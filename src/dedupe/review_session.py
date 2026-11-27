"""Durable storage for the last completed review session."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path

from .actions import validate_saved_records
from .human_policy import may_enter_no_person_review
from .models import (
    DuplicateGroup,
    FileRecord,
    GroupKind,
    MediaType,
    ReviewPolicy,
    ScanDiagnostics,
    ScanResult,
)
from .parallel import map_parallel, resolve_workers

REVIEW_SESSION_VERSION = 2
SUPPORTED_REVIEW_SESSION_VERSIONS = (1, 2)
MAX_REVIEW_SESSION_BYTES = 64 * 1024 * 1024
PEEK_HEADER_BYTES = 64 * 1024
FAST_FULL_PEEK_MAX_BYTES = 1024 * 1024
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
    # Trashed review candidates (path -> Trash destination) from the saved scan.
    deleted_files: dict[str, str] = field(default_factory=dict)
    # Trashed candidates whose Trash target is missing or unlocatable on disk.
    unlocatable_deleted_files: dict[str, str] = field(default_factory=dict)
    # True when only the session's identity was read at startup (peek): the
    # file exists and is readable, but its result was neither built nor
    # revalidated. Such a load is banner metadata, never an active review.
    peeked: bool = False
    roots: list[str] = field(default_factory=list)
    protected_folders: list[str] = field(default_factory=list)
    scan_settings: dict = field(default_factory=dict)
    job_id: str | None = None
    job_name: str | None = None
    group_count: int = 0
    file_count: int = 0

    @property
    def available(self) -> bool:
        return self.result is not None or self.peeked

    def metadata(self) -> dict:
        roots = list(self.result.roots) if self.result else list(self.roots)
        protected = (
            list(self.result.protected_folders)
            if self.result
            else list(self.protected_folders)
        )
        scan_settings = (
            dict(self.result.scan_settings) if self.result else dict(self.scan_settings)
        )
        job_id = self.result.job_id if self.result else self.job_id
        group_count = len(self.result.groups) if self.result else int(self.group_count or 0)
        file_count = len(self.result.files) if self.result else int(self.file_count or 0)
        job_name = self.job_name or (
            str(scan_settings["job_name"]) if scan_settings.get("job_name") else None
        )
        return {
            "path": str(self.path) if self.path else None,
            "available": self.available,
            "peeked": self.peeked,
            "saved_at": self.saved_at,
            "roots": roots,
            "protected_folders": protected,
            "scan_settings": scan_settings,
            "job_id": job_id,
            "job_name": job_name,
            "group_count": group_count,
            "file_count": file_count,
            "pruned_files": self.pruned_files,
            "pruned_reasons": dict(self.pruned_reasons),
            "pruned_reason_labels": {
                reason: PRUNE_REASON_LABELS[reason] for reason in self.pruned_reasons
            },
            "pruned_samples": [dict(sample) for sample in self.pruned_samples],
            "pruned_sample_limit": MAX_PRUNE_SAMPLES,
            "unlocatable_deleted_files": dict(self.unlocatable_deleted_files),
            "unlocatable_deleted_count": len(self.unlocatable_deleted_files),
            "corrupt": self.corrupt,
            "error": self.error,
        }


def _clone_record(record: FileRecord) -> FileRecord:
    """Return an independent shallow copy of a cached FileRecord."""
    return replace(record)


def _encode_compact_result(result: ScanResult) -> tuple[list[dict], dict]:
    """Encode a ScanResult with a deduplicated top-level FileRecord table."""
    result.recompute_stats()
    file_table: list[dict] = []
    by_id: dict[int, int] = {}
    by_path: dict[str, list[tuple[dict, int]]] = {}

    def _intern(record: FileRecord) -> int:
        rid = id(record)
        cached = by_id.get(rid)
        if cached is not None:
            return cached
        rec_dict = record.to_dict(compact=True)
        for existing_dict, idx in by_path.get(record.path, ()):
            if existing_dict == rec_dict:
                by_id[rid] = idx
                return idx
        idx = len(file_table)
        file_table.append(rec_dict)
        by_path.setdefault(record.path, []).append((rec_dict, idx))
        by_id[rid] = idx
        return idx

    compact_groups: list[dict] = []
    for group in result.groups:
        compact_groups.append(
            {
                "id": group.id,
                "kind": group.kind.value,
                "policy": group.policy.value,
                "media_type": group.media_type.value,
                "members": [_intern(member) for member in group.members],
                "selected_for_removal": list(group.selected_for_removal),
                "reviewed_paths": list(group.reviewed_paths),
                "suggested_keep": group.suggested_keep,
                "reclaimable_bytes": group.reclaimable_bytes,
                "member_count": len(group.members),
                "root": group.root,
                "distinct_participants": list(group.distinct_participants),
                "match_basis": group.match_basis,
                "match_scores": dict(group.match_scores),
                "match_details": dict(group.match_details),
                "anchor_path": group.anchor_path,
                "reviewed": group.reviewed,
                "match_explanations": dict(group.match_explanations),
            }
        )

    compact_result = {
        "roots": list(result.roots),
        "files": [_intern(record) for record in result.files],
        "groups": compact_groups,
        "exact_groups": result.exact_groups,
        "similar_groups": result.similar_groups,
        "no_human_files": result.no_human_files,
        "low_resolution_files": result.low_resolution_files,
        "random_review_files": result.random_review_files,
        "faces_files": result.faces_files,
        "crop_files": result.crop_files,
        "reclaimable_bytes": result.reclaimable_bytes,
        "file_count": len(result.files),
        "group_count": len(result.groups),
        "errors": list(result.errors),
        "diagnostics": result.diagnostics.to_dict(),
        "protected_folders": list(result.protected_folders),
        "job_id": result.job_id,
        "scan_settings": dict(result.scan_settings),
    }
    return file_table, compact_result


def _resolve_file_entry(entry: object, records_table: list[FileRecord]) -> FileRecord:
    if isinstance(entry, int) and not isinstance(entry, bool):
        if entry < 0 or entry >= len(records_table):
            raise ValueError(f"invalid file_table index: {entry}")
        return _clone_record(records_table[entry])
    if isinstance(entry, dict):
        return FileRecord.from_dict(entry)
    raise ValueError(f"invalid serialized file record reference: {type(entry).__name__}")


def _decode_group(data: dict, records_table: list[FileRecord]) -> DuplicateGroup:
    kind = GroupKind(data["kind"])
    members = [_resolve_file_entry(m, records_table) for m in data.get("members", [])]
    if kind == GroupKind.NO_HUMANS:
        members = sorted(
            (member for member in members if may_enter_no_person_review(member)),
            key=lambda member: (-member.mtime_sort_stamp, member.path),
        )
    elif kind == GroupKind.FACES:
        members = sorted(
            (
                member
                for member in members
                if member.face_count is not None and member.face_count >= 1
            ),
            key=lambda member: (
                -member.face_count,
                -member.mtime_sort_stamp,
                member.path,
            ),
        )
    member_paths = {member.path for member in members}
    return DuplicateGroup(
        id=data["id"],
        kind=kind,
        media_type=MediaType(data["media_type"]),
        members=members,
        selected_for_removal=[
            path for path in data.get("selected_for_removal", []) if path in member_paths
        ],
        reviewed_paths=[
            path for path in data.get("reviewed_paths", []) if path in member_paths
        ],
        suggested_keep=data.get("suggested_keep"),
        root=data.get("root"),
        distinct_participants=list(data.get("distinct_participants", [])),
        match_basis=data.get("match_basis"),
        match_details=dict(data.get("match_details") or {}),
        match_scores={
            path: float(score)
            for path, score in (data.get("match_scores") or {}).items()
            if path in member_paths
        },
        anchor_path=data.get("anchor_path"),
        reviewed=bool(data.get("reviewed", False)),
        match_explanations={
            str(path): str(text)
            for path, text in (data.get("match_explanations") or {}).items()
            if path in member_paths
        },
    )


def _decode_envelope_result(envelope: dict) -> ScanResult:
    raw_result = envelope["result"]
    if not isinstance(raw_result, dict):
        raise TypeError("review session result must be a JSON object")
    raw_table = envelope.get("file_table")
    if raw_table is None:
        return ScanResult.from_dict(raw_result)
    if not isinstance(raw_table, list):
        raise TypeError("review session file_table must be a list")
    records_table = [FileRecord.from_dict(entry) for entry in raw_table]
    groups = [_decode_group(group_data, records_table) for group_data in raw_result.get("groups", [])]
    groups = [
        group
        for group in groups
        if group.members or group.kind not in (GroupKind.NO_HUMANS, GroupKind.FACES)
    ]
    result = ScanResult(
        roots=list(raw_result.get("roots", [])),
        files=[_resolve_file_entry(entry, records_table) for entry in raw_result.get("files", [])],
        groups=groups,
        errors=list(raw_result.get("errors", [])),
        diagnostics=ScanDiagnostics.from_dict(raw_result.get("diagnostics", {})),
        protected_folders=list(raw_result.get("protected_folders", [])),
        job_id=raw_result.get("job_id"),
        scan_settings=dict(raw_result.get("scan_settings") or {}),
    )
    result.recompute_stats()
    return result


def _validate_version(version: object) -> None:
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in SUPPORTED_REVIEW_SESSION_VERSIONS
    ):
        raise ValueError(f"unsupported review session version: {version!r}")


def _apply_header_metadata(report: ReviewSessionLoad, source: dict) -> None:
    result_meta = source.get("result") if isinstance(source.get("result"), dict) else {}
    report.saved_at = source.get("saved_at")
    roots = source.get("roots")
    if roots is None:
        roots = result_meta.get("roots")
    if isinstance(roots, list):
        report.roots = [str(root) for root in roots]
    protected = source.get("protected_folders")
    if protected is None:
        protected = result_meta.get("protected_folders")
    if isinstance(protected, list):
        report.protected_folders = [str(folder) for folder in protected]
    scan_settings = source.get("scan_settings")
    if scan_settings is None:
        scan_settings = result_meta.get("scan_settings")
    if isinstance(scan_settings, dict):
        report.scan_settings = dict(scan_settings)
    report.job_id = source.get("job_id") if "job_id" in source else result_meta.get("job_id")
    job_name = source.get("job_name") or report.scan_settings.get("job_name")
    report.job_name = str(job_name) if job_name else None
    group_count = source.get("group_count")
    if group_count is None:
        if "group_count" in result_meta:
            group_count = result_meta.get("group_count")
        elif isinstance(result_meta.get("groups"), list):
            group_count = len(result_meta["groups"])
    if isinstance(group_count, int) and not isinstance(group_count, bool):
        report.group_count = group_count
    file_count = source.get("file_count")
    if file_count is None:
        if "file_count" in result_meta:
            file_count = result_meta.get("file_count")
        elif isinstance(result_meta.get("files"), list):
            file_count = len(result_meta["files"])
    if isinstance(file_count, int) and not isinstance(file_count, bool):
        report.file_count = file_count


def _parse_json_header(text: str) -> tuple[dict, bool]:
    """Decode top-level JSON metadata fields before the heavy file_table/result body.

    Returns ``(header_fields, complete)`` where ``complete`` is True when either the
    closing brace or one of the heavy body keys was reached cleanly inside the prefix.
    """
    decoder = json.JSONDecoder()
    length = len(text)
    pos = 0
    while pos < length and text[pos].isspace():
        pos += 1
    if pos >= length:
        return {}, False
    if text[pos] != "{":
        raise ValueError("review session root must be a JSON object")
    pos += 1
    header: dict = {}
    while True:
        while pos < length and text[pos].isspace():
            pos += 1
        if pos >= length:
            return header, False
        if text[pos] == "}":
            return header, True
        key, pos = decoder.raw_decode(text, pos)
        if not isinstance(key, str):
            raise TypeError("review session JSON key must be a string")
        while pos < length and text[pos].isspace():
            pos += 1
        if pos >= length:
            return header, False
        if text[pos] != ":":
            raise ValueError("expected ':' after review session key")
        pos += 1
        while pos < length and text[pos].isspace():
            pos += 1
        if key in ("file_table", "result"):
            return header, True
        value, pos = decoder.raw_decode(text, pos)
        header[key] = value
        while pos < length and text[pos].isspace():
            pos += 1
        if pos >= length:
            return header, False
        if text[pos] == ",":
            pos += 1
            continue
        if text[pos] == "}":
            return header, True
        raise ValueError("expected ',' or '}' in review session header")


def _verify_deleted_files(
    raw_deleted: object,
) -> tuple[dict[str, str], dict[str, str], bool]:
    """Validate trashed candidate mappings so dead/restored entries never linger silently."""
    if not isinstance(raw_deleted, dict) or not raw_deleted:
        return {}, {}, False
    verified: dict[str, str] = {}
    unlocatable: dict[str, str] = {}
    changed = False
    for path_key, destination in raw_deleted.items():
        if not isinstance(destination, str):
            changed = True
            continue
        orig_str = str(path_key)
        dest_str = str(destination)
        # If the original file already returned to disk or was replaced, clear undo state
        # so normal validation handles it instead of leaving a conflicting Trash card.
        if os.path.lexists(orig_str):
            changed = True
            continue
        if not dest_str or dest_str == "Trash" or not os.path.lexists(dest_str):
            unlocatable[orig_str] = dest_str
            changed = True
            continue
        verified[orig_str] = dest_str
    return verified, unlocatable, changed


def save_review_session(
    result: ScanResult,
    path: str | Path | None = None,
    deleted_files: dict[str, str] | None = None,
    *,
    job_name: str | None = None,
) -> dict:
    """Atomically save a completed result with private directory/file permissions."""
    target = Path(path).expanduser() if path is not None else default_review_session_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    saved_at = datetime.now(UTC).isoformat()
    file_table, compact_result = _encode_compact_result(result)
    resolved_job_name = job_name or (
        str(result.scan_settings["job_name"]) if result.scan_settings.get("job_name") else None
    )
    # Keep lightweight metadata first so peek_review_session reads only a small header prefix.
    envelope: dict = {
        "version": REVIEW_SESSION_VERSION,
        "saved_at": saved_at,
        "roots": list(result.roots),
        "job_id": result.job_id,
        "job_name": resolved_job_name,
        "group_count": len(result.groups),
        "file_count": len(result.files),
        "protected_folders": list(result.protected_folders),
        "scan_settings": dict(result.scan_settings),
    }
    if deleted_files:
        envelope["deleted_files"] = dict(deleted_files)
    envelope["file_table"] = file_table
    envelope["result"] = compact_result
    payload = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        Path(temp_name).unlink(missing_ok=True)
        raise
    return {"path": str(target), "saved_at": saved_at}


def load_review_session(
    path: str | Path | None = None, *, persist_pruning: bool = True
) -> ReviewSessionLoad:
    """Load and revalidate a review, pruning files that changed since its scan."""
    target = Path(path).expanduser() if path is not None else default_review_session_path()
    report = ReviewSessionLoad(path=target)
    try:
        size = target.stat().st_size
        if size > MAX_REVIEW_SESSION_BYTES:
            raise ValueError(f"review session exceeds {MAX_REVIEW_SESSION_BYTES} bytes")
        envelope = json.loads(target.read_bytes())
        if not isinstance(envelope, dict):
            raise TypeError("review session must be a JSON object")
        _validate_version(envelope.get("version"))
        result = _decode_envelope_result(envelope)
    except FileNotFoundError:
        return report
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        report.error = str(exc)
        report.corrupt = True
        return report

    verified_deleted, unlocatable_deleted, deleted_changed = _verify_deleted_files(
        envelope.get("deleted_files")
    )
    report.deleted_files = verified_deleted
    report.unlocatable_deleted_files = unlocatable_deleted

    # Trashed review candidates whose Trash copy still exists keep their undo control.
    valid_paths = set(report.deleted_files)
    invalid: dict[str, str] = {}
    # Group members can exist outside result.files in older exports; validate them too.
    snapshots: dict[str, list[FileRecord]] = {}
    for record in chain(result.files, *(group.members for group in result.groups)):
        if record.path not in valid_paths:
            snapshots.setdefault(record.path, []).append(record)

    def _validate(records):
        return records[0].path, validate_saved_records(records, result.roots)

    for validated_path, error in map_parallel(
        _validate, list(snapshots.values()), workers=resolve_workers(None)
    ):
        if error is None:
            valid_paths.add(validated_path)
        else:
            invalid[validated_path] = error
    result.files = [record for record in result.files if record.path in valid_paths]
    groups = []
    for group in result.groups:
        group.members = [member for member in group.members if member.path in valid_paths]
        member_paths = {member.path for member in group.members}
        group.selected_for_removal = [p for p in group.selected_for_removal if p in member_paths]
        group.reviewed_paths = [p for p in group.reviewed_paths if p in member_paths]
        if group.policy == ReviewPolicy.KEEP_ONE:
            if group.suggested_keep not in member_paths:
                group.suggested_keep = group.members[0].path if group.members else None
        elif group.suggested_keep is not None and group.suggested_keep not in member_paths:
            group.suggested_keep = None
        minimum = 1 if group.policy == ReviewPolicy.INDEPENDENT_CANDIDATES else 2
        if len(group.members) >= minimum:
            groups.append(group)
    result.groups = groups
    result.recompute_stats()
    report.result = result
    _apply_header_metadata(report, envelope)
    report.group_count = len(result.groups)
    report.file_count = len(result.files)
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
    if persist_pruning and (report.pruned_files or deleted_changed):
        try:
            saved = save_review_session(
                result,
                target,
                deleted_files=report.deleted_files,
                job_name=report.job_name,
            )
            report.saved_at = saved["saved_at"]
        except OSError as exc:
            report.error = f"loaded but could not persist stale-file pruning: {exc}"
    return report


def peek_review_session(path: str | Path | None = None) -> ReviewSessionLoad:
    """Read lightweight session identity and metadata without loading/parsing huge group bodies."""
    target = Path(path).expanduser() if path is not None else default_review_session_path()
    report = ReviewSessionLoad(path=target, peeked=True)
    try:
        size = target.stat().st_size
        if size > MAX_REVIEW_SESSION_BYTES:
            raise ValueError(f"review session exceeds {MAX_REVIEW_SESSION_BYTES} bytes")
        if size <= FAST_FULL_PEEK_MAX_BYTES:
            envelope = json.loads(target.read_bytes())
            if not isinstance(envelope, dict):
                raise ValueError("review session must be a JSON object")
            _validate_version(envelope.get("version"))
            _apply_header_metadata(report, envelope)
            return report

        header: dict = {}
        complete = False
        read_limit = PEEK_HEADER_BYTES
        max_header_read = min(size, 512 * 1024)
        with target.open("rb") as stream:
            while True:
                stream.seek(0)
                prefix_bytes = stream.read(read_limit)
                text = prefix_bytes.decode("utf-8", errors="replace")
                try:
                    header, complete = _parse_json_header(text)
                except json.JSONDecodeError:
                    complete = False
                if complete or read_limit >= max_header_read:
                    break
                read_limit = min(max_header_read, read_limit * 2)
        if not complete and "version" not in header:
            raise ValueError("could not parse review session header")
        if "version" not in header:
            envelope = json.loads(target.read_bytes())
            if not isinstance(envelope, dict):
                raise ValueError("review session must be a JSON object")
            _validate_version(envelope.get("version"))
            _apply_header_metadata(report, envelope)
            return report
        _validate_version(header.get("version"))
        _apply_header_metadata(report, header)
    except FileNotFoundError:
        return ReviewSessionLoad(path=target)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        report.error = str(exc)
        report.corrupt = True
        report.peeked = False
    return report


def discard_review_session(path: str | Path | None = None) -> bool:
    target = Path(path).expanduser() if path is not None else default_review_session_path()
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
