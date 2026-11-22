"""Durable per-file keep decisions for low-resolution review.

When the user reviews a low-resolution candidate and chooses Keep, that
decision is stored here (keyed by path, validated by size and mtime) so the
file is not surfaced for low-resolution review again on the next scan. Editing
or replacing the file invalidates its decision.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

from .models import FileRecord

KEEP_DECISIONS_VERSION = 1
MAX_KEEP_DECISIONS_BYTES = 16 * 1024 * 1024


def default_keep_decisions_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    state = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return state / "dedupe" / "keep-decisions.json"


def _resolve_target(path: str | Path | None) -> Path:
    return Path(path).expanduser() if path is not None else default_keep_decisions_path()


@contextmanager
def _locked_store(target: Path):
    """Advisory cross-process lock around read-modify-write on ``target``."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    lock_path = target.with_name(f".{target.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _load_keep_decisions_strict(target: Path) -> dict[str, dict]:
    """Load decisions, returning ``{}`` only if missing; raise OSError on corrupt/oversized."""
    try:
        stat = target.stat()
    except FileNotFoundError:
        return {}
    if stat.st_size > MAX_KEEP_DECISIONS_BYTES:
        raise OSError(
            f"keep-decisions store exceeds {MAX_KEEP_DECISIONS_BYTES} bytes: {target}"
        )
    try:
        envelope = json.loads(target.read_bytes())
    except (OSError, ValueError) as exc:
        raise OSError(f"could not read keep-decisions store {target}: {exc}") from exc
    if not isinstance(envelope, dict) or envelope.get("version") != KEEP_DECISIONS_VERSION:
        raise OSError(
            f"unsupported keep-decisions format or version in {target}"
        )
    decisions = envelope.get("decisions")
    if not isinstance(decisions, dict):
        raise OSError(f"invalid keep-decisions payload in {target}")
    return {
        str(file_path): identity
        for file_path, identity in decisions.items()
        if isinstance(identity, dict)
    }


def load_keep_decisions(path: str | Path | None = None) -> dict[str, dict]:
    """Return {file path: identity} for every stored keep decision."""
    target = _resolve_target(path)
    try:
        return _load_keep_decisions_strict(target)
    except OSError:
        return {}


def _write_keep_decisions_unlocked(decisions: dict[str, dict], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    envelope = {"version": KEEP_DECISIONS_VERSION, "decisions": decisions}
    payload = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)
        raise


def save_keep_decisions(decisions: dict[str, dict], path: str | Path | None = None) -> None:
    """Atomically save decisions with private directory/file permissions."""
    target = _resolve_target(path)
    with _locked_store(target):
        _write_keep_decisions_unlocked(decisions, target)


def _identity(record: FileRecord) -> dict:
    entry: dict[str, int | str] = {
        "size": int(record.size),
        "mtime_ns": record.mtime_sort_stamp,
        "decided_at": datetime.now(UTC).isoformat(),
    }
    if record.device is not None and record.inode is not None:
        entry["device"] = int(record.device)
        entry["inode"] = int(record.inode)
    return entry


def _identity_matches(record: FileRecord, identity: dict) -> bool:
    try:
        return (
            int(identity["size"]) == int(record.size)
            and int(identity["mtime_ns"]) == record.mtime_sort_stamp
        )
    except (KeyError, TypeError, ValueError):
        return False


def _dev_inode_matches(record: FileRecord, identity: dict) -> bool:
    if record.device is None or record.inode is None:
        return False
    raw_dev = identity.get("device")
    raw_ino = identity.get("inode")
    if raw_dev is None or raw_ino is None:
        return False
    try:
        return (
            int(raw_dev) == int(record.device)
            and int(raw_ino) == int(record.inode)
            and _identity_matches(record, identity)
        )
    except (TypeError, ValueError):
        return False


def matches_keep_decision(record: FileRecord, decisions: dict[str, dict]) -> bool:
    """True when the record still matches its stored keep decision (or renamed inode)."""
    identity = decisions.get(record.path)
    if identity is not None:
        return _identity_matches(record, identity)
    if record.device is None or record.inode is None:
        return False
    return any(_dev_inode_matches(record, entry) for entry in decisions.values())


def kept_paths(records: Iterable[FileRecord], path: str | Path | None = None) -> set[str]:
    """Paths among ``records`` with a still-valid stored keep decision."""
    decisions = load_keep_decisions(path)
    if not decisions:
        return set()
    by_inode: dict[tuple[int, int], list[dict]] = {}
    for entry in decisions.values():
        raw_dev = entry.get("device")
        raw_ino = entry.get("inode")
        if raw_dev is not None and raw_ino is not None:
            try:
                by_inode.setdefault((int(raw_dev), int(raw_ino)), []).append(entry)
            except (TypeError, ValueError):
                continue
    kept: set[str] = set()
    for record in records:
        identity = decisions.get(record.path)
        if identity is not None:
            if _identity_matches(record, identity):
                kept.add(record.path)
            continue
        if record.device is not None and record.inode is not None:
            entries = by_inode.get((int(record.device), int(record.inode)))
            if entries and any(_identity_matches(record, entry) for entry in entries):
                kept.add(record.path)
    return kept


def update_keep_decisions(
    keep: Iterable[FileRecord] = (),
    clear: Iterable[str] = (),
    path: str | Path | None = None,
) -> None:
    """Record keeps and/or drop cleared paths; no-op when nothing changes.

    Refuses to overwrite an existing unreadable, oversized, or corrupt store
    (raises OSError cleanly instead of wiping prior keep decisions).
    """
    keep = list(keep)
    clear = [str(item) for item in clear]
    if not keep and not clear:
        return
    target = _resolve_target(path)
    with _locked_store(target):
        decisions = _load_keep_decisions_strict(target)
        changed = False
        for cleared in clear:
            if decisions.pop(cleared, None) is not None:
                changed = True
        for record in keep:
            existing = decisions.get(record.path)
            if existing is None or not _identity_matches(record, existing):
                decisions[record.path] = _identity(record)
                changed = True
        if changed:
            _write_keep_decisions_unlocked(decisions, target)
