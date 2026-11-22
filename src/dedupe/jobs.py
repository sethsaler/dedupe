"""Named, local cleanup jobs with independently resumable reviews."""

from __future__ import annotations

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
