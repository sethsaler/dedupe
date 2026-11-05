"""Named, local cleanup jobs with independently resumable reviews."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .models import FileRecord, ScanResult
from .review_session import default_review_session_path, save_review_session


def file_identity(record: FileRecord) -> list:
    return [record.size, record.mtime_sort_stamp, record.ctime_ns,
            record.device, record.inode]


class JobStore:
    def __init__(self, review_path: str | Path | None = None):
        review = Path(review_path) if review_path else default_review_session_path()
        self.root = review.parent / "cleanup-jobs"

    def _path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("invalid cleanup job")
        return self.root / f"{job_id}.json"

    def session_path(self, job_id: str) -> Path:
        return self._path(job_id).with_suffix(".review.json")

    def get(self, job_id: str) -> dict:
        path = self._path(job_id)
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("cleanup job is too large")
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("id") != job_id:
            raise ValueError("invalid cleanup job file")
        return data

    def list(self) -> list[dict]:
        jobs = []
        for path in self.root.glob("*.json"):
            if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                continue
            try:
                data = self.get(path.stem)
                jobs.append({key: value for key, value in data.items() if key != "baseline"})
            except (OSError, ValueError, TypeError):
                continue
        return sorted(jobs, key=lambda job: job.get("updated_at", ""), reverse=True)

    def _write(self, data: dict) -> None:
        path = self._path(data["id"])
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".job-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def save(self, name: str, settings: dict, job_id: str | None = None) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Give the job a name of 1–100 characters")
        if not isinstance(settings, dict) or len(json.dumps(settings)) > 64 * 1024:
            raise ValueError("invalid cleanup settings")
        previous = self.get(job_id) if job_id is not None else {}
        # These belong to a request, not to a reusable job definition.
        settings = {key: value for key, value in settings.items()
                    if key not in {"job_id", "new_only", "scan_id", "retry"}}
        data = {**previous, "id": job_id or uuid.uuid4().hex, "name": name.strip(),
                "settings": settings, "updated_at": datetime.now(UTC).isoformat()}
        if previous and previous.get("settings") != settings:
            # A changed scope must receive a full pass before using a baseline.
            data.pop("baseline", None)
            data.pop("last_scan_at", None)
        self._write(data)
        return {key: value for key, value in data.items() if key != "baseline"}

    def save_review(self, result: ScanResult, deleted_files: dict | None = None) -> None:
        if result.job_id:
            self.get(result.job_id)
            save_review_session(result, self.session_path(result.job_id), deleted_files)

    def finish_scan(self, result: ScanResult) -> None:
        if not result.job_id:
            return
        data = self.get(result.job_id)
        data["baseline"] = {record.path: file_identity(record) for record in result.files}
        data["last_scan_at"] = datetime.now(UTC).isoformat()
        self._write(data)

    def remove(self, job_id: str) -> None:
        self._path(job_id).unlink()
        self.session_path(job_id).unlink(missing_ok=True)


def new_arrival_paths(result: ScanResult, baseline: dict) -> set[str]:
    return {record.path for record in result.files
            if baseline.get(record.path) != file_identity(record)}


def focus_new_arrivals(result: ScanResult, baseline: dict) -> int:
    """Retain complete duplicate groups but only new independent candidates."""
    from .models import ReviewPolicy

    arrivals = new_arrival_paths(result, baseline)
    groups = []
    for group in result.groups:
        if group.policy == ReviewPolicy.INDEPENDENT_CANDIDATES:
            group.members = [member for member in group.members if member.path in arrivals]
            group.selected_for_removal = [p for p in group.selected_for_removal if p in arrivals]
            group.reviewed_paths = [p for p in group.reviewed_paths if p in arrivals]
            if group.members:
                groups.append(group)
        elif any(member.path in arrivals for member in group.members):
            groups.append(group)
    result.groups = groups
    result.recompute_stats()
    return len(arrivals)
