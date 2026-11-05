"""Durable per-file move intent, outcomes, and conservative crash reconciliation.

Journals are ordinary action receipts. Recovery inspects files and updates the
receipt; it never moves a file. A move without a recorded result is accepted
only when its destination still matches the content saved before the move.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from os import replace as atomic_replace
from pathlib import Path
from stat import S_ISREG

from .exact import file_sha256
from .receipts import iter_receipt_paths, receipt_filename, resolve_log_dir

_active: set[str] = set()
_active_lock = threading.Lock()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _identity(path: Path, verified_hash=None) -> dict:
    before = path.lstat()
    if not S_ISREG(before.st_mode):
        raise OSError("move journal requires a regular file")
    if verified_hash is not None:
        digest, verified_stat = verified_hash
        if any(getattr(before, key) != getattr(verified_stat, key)
               for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
            raise OSError("file changed after its immediate content check")
    else:
        digest = file_sha256(path)
    after = path.lstat()
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in keys):
        raise OSError("file changed while preparing its move journal")
    return {"size": after.st_size, "sha256": digest,
            "device": after.st_dev, "inode": after.st_ino}


class ActionJournal:
    def __init__(self, result: dict, plans: list[dict], log_dir=None):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        self.path = resolve_log_dir(log_dir) / receipt_filename(
            dry_run=False, stamp=stamp, session_id=result["session_id"],
        )
        self.lock = threading.Lock()
        self.failed = False
        self.events_path = self.path.with_suffix(".journal")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        self.data = {
            **result, "log_path": str(self.path), "status": "running", "pid": os.getpid(),
            "journal_version": 1,
            "journal_events": self.events_path.name,
            "journal": [{**plan, "state": "pending"} for plan in plans],
        }
        try:
            self._save()
        except BaseException:
            self.close()
            raise
        with _active_lock:
            _active.add(str(self.path))

    def _save(self):
        try:
            atomic_json(self.path, self.data)
        except OSError:
            self.failed = True
            raise

    def _append(self, index: int, entry: dict):
        try:
            with self.events_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"index": index, "entry": entry}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            self.failed = True
            raise

    def begin(self, index: int, *, search_dirs=(), verified_hash=None):
        entry = self.data["journal"][index]
        identity = _identity(Path(entry["path"]), verified_hash)
        with self.lock:
            if self.failed:
                raise OSError("move journal unavailable; remaining moves stopped")
            entry.update(identity=identity, state="moving", search_dirs=list(map(str, search_dirs)))
            self._append(index, entry)

    def record(self, index: int, item: dict):
        with self.lock:
            entry = self.data["journal"][index]
            uncertain = (
                entry["state"] == "moving" and not item["ok"]
                and not Path(entry["path"]).exists()
            )
            entry.update(state="moving" if uncertain else "complete", result=item)
            self._append(index, entry)

    def finalize(self, result: dict):
        with self.lock:
            unfinished = any(entry["state"] == "moving" for entry in self.data["journal"])
            self.data.update(result, status="running" if unfinished else "complete",
                             log_path=str(self.path))
            self._save()
            self.events_path.unlink(missing_ok=True)
        self.close()

    def close(self):
        with _active_lock:
            _active.discard(str(self.path))
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None


def _matches(path: Path, identity: dict) -> bool:
    try:
        stat = path.lstat()
        return (
            S_ISREG(stat.st_mode)
            and stat.st_size == identity.get("size")
            and file_sha256(path) == identity.get("sha256")
        )
    except OSError:
        return False


def _recover_entry(entry: dict, action: str) -> dict:
    if entry.get("state") == "complete":
        return entry["result"]
    source = Path(entry["path"])
    item = {"path": str(source), "action": action, "size": entry.get("size"), "ok": False}
    if source.exists() or source.is_symlink():
        identity = entry.get("identity", {})
        destination = entry.get("destination")
        conflicting_destination = (
            destination and destination != "Trash" and Path(destination).exists()
        )
        try:
            stat = source.lstat()
            original_identity = (stat.st_dev, stat.st_ino) == (
                identity.get("device"), identity.get("inode"),
            )
        except OSError:
            original_identity = False
        untouched = entry.get("state") == "pending" or (
            original_identity and _matches(source, identity) and not conflicting_destination
        )
        item["error"] = (
            "interrupted before removal; original is still present" if untouched else
            "original path is occupied; interrupted move needs inspection"
        )
        entry["recovery"] = "original_present" if untouched else "uncertain"
        return item
    if entry.get("state") == "pending":
        item["error"] = "original is missing; no move was recorded"
        entry["recovery"] = "uncertain"
        return item
    candidates = []
    if entry.get("destination") and entry["destination"] != "Trash":
        candidates = [Path(entry["destination"])]
    else:
        for directory in entry.get("search_dirs", []):
            try:
                # Content equality by itself cannot identify an arbitrary file
                # in Trash. Limit candidates to the original name/collision form.
                candidates.extend(
                    path for path in Path(directory).iterdir()
                    if path.name == source.name or path.name.startswith(f"{source.stem} ")
                    or path.name.startswith(f"{source.stem}_")
                )
            except OSError:
                continue
    identity = entry.get("identity", {})
    if not entry.get("destination") or entry["destination"] == "Trash":
        # A pre-existing duplicate with identical bytes in Trash is not proof
        # that this move completed. Require the original filesystem identity.
        same_files = []
        for candidate in candidates:
            try:
                stat = candidate.lstat()
                if (stat.st_dev, stat.st_ino) == (identity.get("device"), identity.get("inode")):
                    same_files.append(candidate)
            except OSError:
                pass
        candidates = same_files
    matches = [p for p in candidates if _matches(p, identity)]
    if len(matches) == 1:
        item.update(ok=True, destination=str(matches[0]))
        entry["recovery"] = "confirmed"
    else:
        item["error"] = "move interrupted; destination could not be identified safely"
        entry["recovery"] = "uncertain"
    return item


def recover_interrupted_actions(log_dir=None) -> list[dict]:
    """Reconcile abandoned receipts without moving files; leave live workers alone."""
    recovered = []
    for path in iter_receipt_paths(log_dir):
        lock_stream = None
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                continue
            data = json.loads(path.read_text())
            if data.get("status") != "running" or data.get("journal_version") != 1:
                continue
            with _active_lock:
                if str(path) in _active:
                    continue
            events_path = path.with_suffix(".journal")
            if data.get("journal_events") and events_path.exists():
                lock_stream = events_path.open("rb")
                try:
                    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
            pid = data.get("pid")
            if (not data.get("journal_events") and isinstance(pid, int)
                    and pid != os.getpid()):
                try:
                    os.kill(pid, 0)
                    continue
                except ProcessLookupError:
                    pass
                except (PermissionError, ValueError):
                    continue
            if events_path.exists():
                with events_path.open(encoding="utf-8") as events:
                    for line in events:
                        try:
                            event = json.loads(line)
                            index = event["index"]
                            entry = event["entry"]
                            if (isinstance(index, int) and 0 <= index < len(data["journal"])
                                    and isinstance(entry, dict)
                                    and entry.get("path") == data["journal"][index]["path"]):
                                data["journal"][index] = entry
                        except (ValueError, TypeError, KeyError):
                            # A final partial write is expected after interruption.
                            # Without a complete intent, no move is inferred.
                            continue
            items = [_recover_entry(entry, data["action"]) for entry in data["journal"]]
            data.update(
                items=items, status="interrupted", reconciled_at=_stamp(),
                success_count=sum(item["ok"] for item in items),
                fail_count=sum(not item["ok"] for item in items),
                uncertain_count=sum(e.get("recovery") == "uncertain" for e in data["journal"]),
            )
            atomic_json(path, data)
            events_path.unlink(missing_ok=True)
            recovered.append(data)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        finally:
            if lock_stream is not None:
                lock_stream.close()
    return recovered
