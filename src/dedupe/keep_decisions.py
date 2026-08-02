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
from datetime import UTC, datetime
from pathlib import Path

from .models import FileRecord

KEEP_DECISIONS_VERSION = 1
MAX_KEEP_DECISIONS_BYTES = 16 * 1024 * 1024


def default_keep_decisions_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    state = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return state / "dedupe" / "keep-decisions.json"


def load_keep_decisions(path: str | Path | None = None) -> dict[str, dict]:
    """Return {file path: identity} for every stored keep decision."""
    target = Path(path).expanduser() if path is not None else default_keep_decisions_path()
    try:
        if target.stat().st_size > MAX_KEEP_DECISIONS_BYTES:
            return {}
        envelope = json.loads(target.read_bytes())
    except (OSError, ValueError):
        return {}
    if not isinstance(envelope, dict) or envelope.get("version") != KEEP_DECISIONS_VERSION:
        return {}
    decisions = envelope.get("decisions")
    if not isinstance(decisions, dict):
        return {}
    return {
        str(file_path): identity
        for file_path, identity in decisions.items()
        if isinstance(identity, dict)
    }


def save_keep_decisions(decisions: dict[str, dict], path: str | Path | None = None) -> None:
    """Atomically save decisions with private directory/file permissions."""
    target = Path(path).expanduser() if path is not None else default_keep_decisions_path()
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


def _identity(record: FileRecord) -> dict:
    return {
        "size": int(record.size),
        "mtime_ns": record.mtime_sort_stamp,
        "decided_at": datetime.now(UTC).isoformat(),
    }


def matches_keep_decision(record: FileRecord, decisions: dict[str, dict]) -> bool:
    """True when the record still matches its stored keep decision."""
    identity = decisions.get(record.path)
    if not identity:
        return False
    try:
        return (
            int(identity["size"]) == int(record.size)
            and int(identity["mtime_ns"]) == record.mtime_sort_stamp
        )
    except (KeyError, TypeError, ValueError):
        return False


def kept_paths(records: Iterable[FileRecord], path: str | Path | None = None) -> set[str]:
    """Paths among ``records`` with a still-valid stored keep decision."""
    decisions = load_keep_decisions(path)
    if not decisions:
        return set()
    return {record.path for record in records if matches_keep_decision(record, decisions)}


def update_keep_decisions(
    keep: Iterable[FileRecord] = (),
    clear: Iterable[str] = (),
    path: str | Path | None = None,
) -> None:
    """Record keeps and/or drop cleared paths; no-op when nothing changes."""
    keep = list(keep)
    clear = [str(item) for item in clear]
    if not keep and not clear:
        return
    decisions = load_keep_decisions(path)
    changed = False
    for cleared in clear:
        if decisions.pop(cleared, None) is not None:
            changed = True
    for record in keep:
        if not matches_keep_decision(record, decisions):
            decisions[record.path] = _identity(record)
            changed = True
    if changed:
        save_keep_decisions(decisions, path)
