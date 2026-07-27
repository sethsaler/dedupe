"""Durable storage for the last completed review session."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .actions import validate_file_record
from .models import ReviewPolicy, ScanResult

REVIEW_SESSION_VERSION = 1
MAX_REVIEW_SESSION_BYTES = 64 * 1024 * 1024
MAX_PRUNE_SAMPLES = 20

PRUNE_REASON_LABELS = {
    "missing": "no longer on disk",
    "changed": "changed since the scan",
    "outside_roots": "outside the scanned folders",
    "symlink": "became a symbolic link",
    "unreadable": "could not be read",
}


def prune_reason(error: str) -> str:
    """Bucket a validation error into a stable, user-facing pruning reason."""
    lowered = error.lower()
    if "symbolic link" in lowered:
        return "symlink"
    if "no longer exists" in lowered or "no such file" in lowered:
        return "missing"
    if "outside the scanned roots" in lowered:
        return "outside_roots"
    if "changed since scan" in lowered:
        return "changed"
    return "unreadable"


def default_review_session_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    state = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return state / "dedupe" / "review-session.json"


@dataclass
class ReviewSessionLoad:
    result: ScanResult | None = None
    path: Path | None = None
    saved_at: str | None = None
    pruned_files: int = 0
    pruned_reasons: dict[str, int] = field(default_factory=dict)
    pruned_samples: list[dict] = field(default_factory=list)
    error: str | None = None
    corrupt: bool = False

    @property
    def available(self) -> bool:
        return self.result is not None

    def metadata(self) -> dict:
        return {
            "path": str(self.path) if self.path else None,
            "available": self.available,
            "saved_at": self.saved_at,
            "pruned_files": self.pruned_files,
            "pruned_reasons": dict(self.pruned_reasons),
            "pruned_reason_labels": {
                reason: PRUNE_REASON_LABELS[reason] for reason in self.pruned_reasons
            },
            "pruned_samples": [dict(sample) for sample in self.pruned_samples],
            "pruned_sample_limit": MAX_PRUNE_SAMPLES,
            "corrupt": self.corrupt,
            "error": self.error,
        }


def save_review_session(result: ScanResult, path: str | Path | None = None) -> dict:
    """Atomically save a completed result with private directory/file permissions."""
    target = Path(path).expanduser() if path is not None else default_review_session_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    saved_at = datetime.now(timezone.utc).isoformat()
    envelope = {
        "version": REVIEW_SESSION_VERSION,
        "saved_at": saved_at,
        "result": result.to_dict(),
    }
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
    return {"path": str(target), "saved_at": saved_at}


def load_review_session(path: str | Path | None = None) -> ReviewSessionLoad:
    """Load and revalidate a review, pruning files that changed since its scan."""
    target = Path(path).expanduser() if path is not None else default_review_session_path()
    report = ReviewSessionLoad(path=target)
    try:
        size = target.stat().st_size
        if size > MAX_REVIEW_SESSION_BYTES:
            raise ValueError(f"review session exceeds {MAX_REVIEW_SESSION_BYTES} bytes")
        envelope = json.loads(target.read_bytes())
        version = envelope.get("version")
        if version != REVIEW_SESSION_VERSION:
            raise ValueError(f"unsupported review session version: {version!r}")
        result = ScanResult.from_dict(envelope["result"])
    except FileNotFoundError:
        return report
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        report.error = str(exc)
        report.corrupt = True
        return report

    valid_paths: set[str] = set()
    invalid: dict[str, str] = {}
    # Group members can exist outside result.files in older exports; validate them too.
    records = list(result.files) + [
        member for group in result.groups for member in group.members
    ]
    for record in records:
        error = validate_file_record(record, result.roots)
        if error is None:
            valid_paths.add(record.path)
        else:
            invalid.setdefault(record.path, error)
    for path in valid_paths:
        invalid.pop(path, None)
    result.files = [record for record in result.files if record.path in valid_paths]
    groups = []
    for group in result.groups:
        group.members = [member for member in group.members if member.path in valid_paths]
        member_paths = {member.path for member in group.members}
        group.selected_for_removal = [p for p in group.selected_for_removal if p in member_paths]
        group.reviewed_paths = [p for p in group.reviewed_paths if p in member_paths]
        if group.suggested_keep not in member_paths:
            group.suggested_keep = group.members[0].path if group.members else None
        minimum = 1 if group.policy == ReviewPolicy.INDEPENDENT_CANDIDATES else 2
        if len(group.members) >= minimum:
            groups.append(group)
    result.groups = groups
    result.recompute_stats()
    report.result = result
    report.saved_at = envelope.get("saved_at")
    report.pruned_files = len(invalid)
    reasons: dict[str, int] = {}
    for error in invalid.values():
        reason = prune_reason(error)
        reasons[reason] = reasons.get(reason, 0) + 1
    report.pruned_reasons = reasons
    report.pruned_samples = [
        {"path": path, "reason": prune_reason(error), "detail": error}
        for path, error in sorted(invalid.items())[:MAX_PRUNE_SAMPLES]
    ]
    if report.pruned_files:
        try:
            saved = save_review_session(result, target)
            report.saved_at = saved["saved_at"]
        except OSError as exc:
            report.error = f"loaded but could not persist stale-file pruning: {exc}"
    return report


def discard_review_session(path: str | Path | None = None) -> bool:
    target = Path(path).expanduser() if path is not None else default_review_session_path()
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
