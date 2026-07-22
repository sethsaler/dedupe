"""Durable review-session persistence and stale-file tests."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from dedupe.grouping import build_groups
from dedupe.models import FileRecord, MediaType, ScanResult
from dedupe.review_session import (
    REVIEW_SESSION_VERSION,
    discard_review_session,
    load_review_session,
    save_review_session,
)


def _result(root: Path) -> ScanResult:
    records = []
    for name in ("a.jpg", "b.jpg"):
        path = root / name
        path.write_bytes(b"same duplicate")
        metadata = path.stat()
        records.append(
            FileRecord(
                path=str(path),
                size=metadata.st_size,
                mtime=metadata.st_mtime,
                media_type=MediaType.IMAGE,
                extension=".jpg",
                device=metadata.st_dev,
                inode=metadata.st_ino,
                mtime_ns=metadata.st_mtime_ns,
            )
        )
    return ScanResult(
        roots=[str(root)],
        files=records,
        groups=build_groups([records], []),
    )


def test_review_session_round_trip_is_private(tmp_path: Path) -> None:
    session_path = tmp_path / "state" / "review.json"
    result = _result(tmp_path)
    result.groups[0].selected_for_removal = [result.groups[0].members[0].path]

    saved = save_review_session(result, session_path)
    loaded = load_review_session(session_path)

    assert saved["path"] == str(session_path)
    assert loaded.error is None
    assert loaded.result is not None
    assert loaded.result.groups[0].selected_for_removal == result.groups[0].selected_for_removal
    assert stat.S_IMODE(session_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(session_path.parent.stat().st_mode) == 0o700


def test_review_session_prunes_changed_files_and_dissolves_group(tmp_path: Path) -> None:
    session_path = tmp_path / "state" / "review.json"
    result = _result(tmp_path)
    changed = Path(result.files[0].path)
    save_review_session(result, session_path)
    changed.write_bytes(b"changed content with another size")

    loaded = load_review_session(session_path)

    assert loaded.error is None
    assert loaded.pruned_files == 1
    assert loaded.result is not None
    assert [record.path for record in loaded.result.files] == [result.files[1].path]
    assert loaded.result.groups == []


def test_corrupt_and_future_review_sessions_are_preserved(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    before = corrupt.read_bytes()

    corrupt_result = load_review_session(corrupt)

    assert corrupt_result.result is None
    assert corrupt_result.error
    assert corrupt.read_bytes() == before

    future = tmp_path / "future.json"
    future.write_text(
        json.dumps({"version": REVIEW_SESSION_VERSION + 1, "result": {}}),
        encoding="utf-8",
    )
    before = future.read_bytes()

    future_result = load_review_session(future)

    assert future_result.result is None
    assert "unsupported" in (future_result.error or "")
    assert future.read_bytes() == before


def test_discard_review_session_is_idempotent(tmp_path: Path) -> None:
    session_path = tmp_path / "review.json"
    save_review_session(_result(tmp_path), session_path)

    assert discard_review_session(session_path) is True
    assert discard_review_session(session_path) is False
