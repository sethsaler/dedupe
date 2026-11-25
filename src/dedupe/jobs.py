"""Named, local cleanup jobs with independently resumable reviews."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .models import FileRecord, GroupKind, ReviewPolicy, ScanResult
from .review_session import default_review_session_path, save_review_session

_JOB_ID_RE = re.compile(r"[a-f0-9]{32}")
_MAX_JOB_META_BYTES = 64 * 1024 * 1024
_MAX_BASELINE_BYTES = 1024 * 1024 * 1024


def file_identity(record: FileRecord) -> list:
    return [record.size, record.mtime_sort_stamp, record.ctime_ns,
            record.device, record.inode]


def _failed_scan_paths(result: ScanResult) -> set[str]:
    """Return file paths that failed analysis so they are not stamped into baseline."""
    failed: set[str] = set()
    record_paths = {record.path for record in result.files}
    for record in result.files:
        if record.error or record.human_detection_status == "analysis_failed":
            failed.add(record.path)
    for err in result.errors:
        for prefix in (
            "border check failed for ",
            "resolution probe failed for ",
            "resolution probe failed: ",
            "video version analysis failed: ",
        ):
            if err.startswith(prefix):
                rest = err[len(prefix) :]
                if rest in record_paths:
                    failed.add(rest)
                else:
                    candidate = rest.rsplit(": ", 1)[0]
                    if candidate in record_paths:
                        failed.add(candidate)
                break
    return failed


class JobStore:
    def __init__(self, review_path: str | Path | None = None):
        review = Path(review_path) if review_path else default_review_session_path()
        self.root = review.parent / "cleanup-jobs"

    def _path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
            raise ValueError("invalid cleanup job")
        return self.root / f"{job_id}.json"

    def session_path(self, job_id: str) -> Path:
        return self._path(job_id).with_suffix(".review.json")

    def baseline_path(self, job_id: str) -> Path:
        return self._path(job_id).with_suffix(".baseline.json")

    def _read_metadata(self, job_id: str) -> dict:
        path = self._path(job_id)
        if path.stat().st_size > _MAX_JOB_META_BYTES:
            raise ValueError("cleanup job is too large")
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("id") != job_id:
            raise ValueError("invalid cleanup job file")
        return data

    def _read_baseline(self, job_id: str) -> dict | None:
        path = self.baseline_path(job_id)
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        if st.st_size > _MAX_BASELINE_BYTES:
            raise ValueError("cleanup job baseline is too large")
        baseline = json.loads(path.read_text())
        if not isinstance(baseline, dict):
            raise TypeError("invalid cleanup job baseline")
        return baseline

    def get(self, job_id: str) -> dict:
        data = self._read_metadata(job_id)
        baseline = self._read_baseline(job_id)
        if baseline is not None:
            data["baseline"] = baseline
        return data

    def list(self) -> list[dict]:
        jobs = []
        for path in self.root.glob("*.json"):
            if not _JOB_ID_RE.fullmatch(path.stem):
                continue
            try:
                data = self._read_metadata(path.stem)
                jobs.append({key: value for key, value in data.items() if key != "baseline"})
            except (OSError, ValueError, TypeError):
                continue
        return sorted(jobs, key=lambda job: job.get("updated_at", ""), reverse=True)

    def _write_json(self, path: Path, payload: dict, *, prefix: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.root, prefix=prefix)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _write(self, data: dict) -> None:
        self._write_json(self._path(data["id"]), data, prefix=".job-")

    def save(self, name: str, settings: dict, job_id: str | None = None) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Give the job a name of 1–100 characters")
        if not isinstance(settings, dict) or len(json.dumps(settings)) > 64 * 1024:
            raise ValueError("invalid cleanup settings")
        previous = self._read_metadata(job_id) if job_id is not None else {}
        # These belong to a request, not to a reusable job definition.
        settings = {key: value for key, value in settings.items()
                    if key not in {"job_id", "new_only", "scan_id", "retry"}}
        legacy_baseline = previous.pop("baseline", None) if previous else None
        data = {**previous, "id": job_id or uuid.uuid4().hex, "name": name.strip(),
                "settings": settings, "updated_at": datetime.now(UTC).isoformat()}
        if previous and previous.get("settings") != settings:
            # A changed scope must receive a full pass before using a baseline.
            data.pop("baseline", None)
            data.pop("last_scan_at", None)
            data["monitor"] = False
            self.baseline_path(data["id"]).unlink(missing_ok=True)
        elif (
            isinstance(legacy_baseline, dict)
            and not self.baseline_path(data["id"]).exists()
        ):
            self._write_json(
                self.baseline_path(data["id"]), legacy_baseline, prefix=".baseline-"
            )
        self._write(data)
        return {key: value for key, value in data.items() if key != "baseline"}

    def set_monitor(self, job_id: str, enabled: bool) -> dict:
        data = self._read_metadata(job_id)
        if enabled and self._read_baseline(job_id) is None:
            raise ValueError("Run this saved job once before monitoring it")
        data["monitor"] = bool(enabled)
        data["updated_at"] = datetime.now(UTC).isoformat()
        self._write(data)
        return {key: value for key, value in data.items() if key != "baseline"}

    def save_review(self, result: ScanResult, deleted_files: dict | None = None) -> None:
        if result.job_id:
            self._read_metadata(result.job_id)
            save_review_session(result, self.session_path(result.job_id), deleted_files)

    def finish_scan(self, result: ScanResult) -> None:
        if not result.job_id:
            return
        data = self._read_metadata(result.job_id)
        data.pop("baseline", None)
        failed_paths = _failed_scan_paths(result)
        baseline = {
            record.path: file_identity(record)
            for record in result.files
            if record.path not in failed_paths
        }
        self._write_json(self.baseline_path(result.job_id), baseline, prefix=".baseline-")
        data["last_scan_at"] = datetime.now(UTC).isoformat()
        self._write(data)

    def remove(self, job_id: str) -> None:
        self._path(job_id).unlink()
        self.session_path(job_id).unlink(missing_ok=True)
        self.baseline_path(job_id).unlink(missing_ok=True)


def new_arrival_paths(result: ScanResult, baseline: dict) -> set[str]:
    return {record.path for record in result.files
            if baseline.get(record.path) != file_identity(record)}


def focus_new_arrivals(result: ScanResult, baseline: dict) -> int:
    """Retain complete duplicate/set groups and new unprotected independent candidates."""
    arrivals = new_arrival_paths(result, baseline)
    groups = []
    for group in result.groups:
        if group.kind == GroupKind.ALL_FILES:
            continue
        if group.kind == GroupKind.SETS:
            if len(group.members) >= 2 and any(
                member.path in arrivals for member in group.members
            ):
                groups.append(group)
        elif group.policy == ReviewPolicy.INDEPENDENT_CANDIDATES:
            kept_members = [
                member
                for member in group.members
                if member.path in arrivals and not member.protected
            ]
            kept_paths = {member.path for member in kept_members}
            group.members = kept_members
            group.selected_for_removal = [
                p for p in group.selected_for_removal if p in kept_paths
            ]
            group.reviewed_paths = [p for p in group.reviewed_paths if p in kept_paths]
            if group.members:
                groups.append(group)
        elif any(member.path in arrivals for member in group.members):
            groups.append(group)
    result.groups = groups
    result.recompute_stats()
    return len(arrivals)


def _default_keep_one_selection(group) -> set[str]:
    """The automatic selection a freshly built exact/similar group would have."""
    if group.match_basis in ("semantic", "video_version"):
        return set()
    keep = group.suggested_keep or group.anchor_path
    if keep is None and group.members:
        keep = max((member.path for member in group.members), default=None)
    return {
        member.path
        for member in group.members
        if member.path != keep and not member.protected
    }


def _fuse_saved_analysis(current: FileRecord, previous: FileRecord | None) -> FileRecord:
    """Fresh identity/stat fields with saved analysis filled in when missing."""
    if previous is None:
        return current
    merged = copy.deepcopy(current)
    for field in dataclasses.fields(FileRecord):
        if getattr(merged, field.name) is None:
            setattr(merged, field.name, getattr(previous, field.name))
    return merged


def _merge_cluster_components(entries: list[tuple[object, bool]], records: dict[str, FileRecord],
                              arrivals: set[str]):
    """Union same-kind old/new keep-one groups connected through shared members."""
    parent = list(range(len(entries)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    owner: dict[str, int] = {}
    for index, (group, is_new) in enumerate(entries):
        for member in group.members:
            # A replaced path is not evidence that its old byte-identical group
            # and its new group are the same duplicate set.
            if not is_new and member.path in arrivals:
                continue
            if member.path in owner:
                union(index, owner[member.path])
            else:
                owner[member.path] = index

    components: dict[int, list[tuple[object, bool]]] = {}
    for index, entry in enumerate(entries):
        components.setdefault(find(index), []).append(entry)

    old_members = {
        member.path: member
        for group, is_new in entries
        if not is_new
        for member in group.members
    }
    merged_groups = []
    for component in components.values():
        # New groups come first in ``entries`` and therefore own the id/template.
        new_paths = {
            member.path
            for group, is_new in component
            if is_new
            for member in group.members
        }
        ordered_members = []
        seen = set()
        for group, is_new in component:
            for member in group.members:
                if member.path in seen or member.path not in records:
                    continue
                # A changed file's old membership is evidence only when the fresh
                # scan still connects it to this component.
                if member.path in arrivals and member.path not in new_paths:
                    continue
                seen.add(member.path)
                if member.path in arrivals:
                    # A replacement at the same path must not inherit hashes,
                    # crop measurements, or detector verdicts from different bytes.
                    ordered_members.append(records[member.path])
                elif is_new:
                    ordered_members.append(
                        _fuse_saved_analysis(records[member.path], old_members.get(member.path))
                    )
                else:
                    # Unchanged files keep their saved analysis (crop metadata,
                    # detector verdicts, hashes) instead of inventory-only records.
                    ordered_members.append(member)
        if len(ordered_members) < 2:
            continue
        member_paths = {member.path for member in ordered_members}
        merged = copy.deepcopy(component[0][0])
        merged.members = ordered_members
        saved_deselections = set()
        for group, is_new in component:
            if is_new:
                continue
            saved_deselections.update(
                set(group.reviewed_paths) - set(group.selected_for_removal)
            )
            if set(group.selected_for_removal) != _default_keep_one_selection(group):
                # A manual keep-one edit is not otherwise marked server-side.
                # Once the saved state differs from the automatic suggestion,
                # every unchanged unselected member is a keep decision that a
                # fresh automatic pick must not undo.
                saved_deselections.update(
                    member.path
                    for member in group.members
                    if member.path not in arrivals
                    and member.path not in group.selected_for_removal
                )
        saved_selections = set()
        saved_reviewed = set()
        for group, is_new in component:
            for path in group.selected_for_removal:
                if is_new or path not in arrivals:
                    saved_selections.add(path)
            for path in group.reviewed_paths:
                if is_new or path not in arrivals:
                    saved_reviewed.add(path)
        merged.selected_for_removal = sorted(
            (saved_selections - saved_deselections) & member_paths
        )
        merged.reviewed_paths = sorted(saved_reviewed & member_paths)
        merged.distinct_participants = sorted({
            path
            for group, _is_new in component
            for path in group.distinct_participants
        })
        merged.reviewed = all(group.reviewed for group, _is_new in component)
        merged.suggested_keep = next(
            (
                group.suggested_keep
                for group, _is_new in component
                if group.suggested_keep in member_paths
            ),
            None,
        )
        merged.match_scores = {}
        merged.match_details = {}
        merged.match_explanations = {}
        for group, _is_new in reversed(component):
            merged.match_scores.update(group.match_scores)
            merged.match_details.update(group.match_details)
            merged.match_explanations.update(group.match_explanations)
        merged_groups.append(merged)
    return merged_groups


def _merge_match_metadata(target, source, arrivals: set[str]) -> None:
    """Carry saved per-member analysis metadata that the fresh group lacks."""
    for key, value in source.match_details.items():
        if key == "adjusted_crops" and isinstance(value, dict):
            value = {path: box for path, box in value.items() if path not in arrivals}
        existing = target.match_details.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged = copy.deepcopy(value)
            merged.update(existing)
            target.match_details[key] = merged
        elif key not in target.match_details:
            target.match_details[key] = copy.deepcopy(value)
    for mapping_name in ("match_scores", "match_explanations"):
        target_mapping = getattr(target, mapping_name)
        for key, value in getattr(source, mapping_name).items():
            target_mapping.setdefault(key, value)


def merge_new_arrival_review(result: ScanResult, previous: ScanResult | None, baseline: dict) -> None:
    """Carry unchanged pending review groups into an incremental arrival result.

    Incremental scans intentionally do not re-verify old-vs-old pairs. The prior
    review is therefore the positive evidence for pairs among unchanged files:
    unchanged non-overlapping groups are carried forward, and same-kind groups
    connected through an arrival are unioned instead of dropping older copies.
    """
    if previous is None or not previous.groups:
        return
    arrivals = new_arrival_paths(result, baseline)
    records = {record.path: record for record in result.files}
    groups = list(result.groups)

    for kind in (GroupKind.EXACT, GroupKind.SIMILAR):
        entries = [(group, True) for group in groups if group.kind == kind]
        entries.extend(
            (group, False) for group in previous.groups if group.kind == kind
        )
        if not entries:
            continue
        merged = _merge_cluster_components(entries, records, arrivals)
        groups = [group for group in groups if group.kind != kind] + merged

    # Independent review tabs are often one group per kind. Preserve unchanged
    # candidates in the matching fresh group when one exists, otherwise carry
    # the old group forward with only the members that still have current records.
    for old in previous.groups:
        if old.kind in (GroupKind.ALL_FILES, GroupKind.EXACT, GroupKind.SIMILAR):
            continue
        target = next((group for group in groups if group.kind == old.kind), None)
        target_paths = {
            member.path for group in groups if group.kind == old.kind
            for member in group.members
        }
        preserved = []
        unchanged_preserved = []
        carried = []
        seen = set()
        target_member_paths = (
            {member.path for member in target.members} if target is not None else set()
        )
        for member in old.members:
            if member.path in seen or member.path not in records:
                continue
            if member.path in arrivals and member.path not in target_paths:
                continue
            seen.add(member.path)
            kept_member = records[member.path] if member.path in arrivals else member
            preserved.append(kept_member)
            if member.path not in arrivals:
                unchanged_preserved.append(kept_member)
            if member.path not in target_member_paths:
                carried.append(kept_member)
        minimum = 2 if old.kind == GroupKind.SETS else 1
        if target is None and len(carried) < minimum:
            continue
        unchanged_preserved_paths = {member.path for member in unchanged_preserved}
        carried_paths = {member.path for member in carried}
        if target is not None:
            target.members.extend(carried)
            saved_deselections = set(old.reviewed_paths) - set(old.selected_for_removal)
            target.selected_for_removal = sorted((
                set(target.selected_for_removal)
                | (set(old.selected_for_removal) & unchanged_preserved_paths)
                - saved_deselections
            ) & {member.path for member in target.members})
            target.reviewed_paths = sorted(
                set(target.reviewed_paths) | (set(old.reviewed_paths) & unchanged_preserved_paths)
            )
            _merge_match_metadata(target, old, arrivals)
        else:
            kept = copy.deepcopy(old)
            kept.members = carried
            kept.selected_for_removal = [
                path for path in old.selected_for_removal if path in carried_paths
            ]
            kept.reviewed_paths = [
                path for path in old.reviewed_paths if path in carried_paths
            ]
            groups.append(kept)

    result.groups = groups
    result.recompute_stats()
