"""Durable review-session persistence and stale-file tests."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from dedupe.grouping import build_groups
from dedupe.models import FileRecord, MediaType, ScanResult
from dedupe.review_session import (
    MAX_PRUNE_SAMPLES,
    PRUNE_REASON_LABELS,
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


def test_pruning_reports_reasons_and_a_bounded_path_sample(tmp_path: Path) -> None:
    session_path = tmp_path / "state" / "review.json"
    result = _result(tmp_path)
    changed = Path(result.files[0].path)
    vanished = Path(result.files[1].path)
    save_review_session(result, session_path)
    changed.write_bytes(b"changed content with another size")
    vanished.unlink()

    loaded = load_review_session(session_path)
    metadata = loaded.metadata()

    assert loaded.pruned_files == 2
    assert loaded.pruned_reasons == {"changed": 1, "missing": 1}
    assert {sample["reason"] for sample in loaded.pruned_samples} == {"changed", "missing"}
    assert {sample["path"] for sample in loaded.pruned_samples} == {
        str(changed),
        str(vanished),
    }
    assert all(sample["detail"] for sample in loaded.pruned_samples)
    assert len(loaded.pruned_samples) <= MAX_PRUNE_SAMPLES
    assert metadata["pruned_reasons"] == loaded.pruned_reasons
    assert metadata["pruned_reason_labels"]["missing"] == PRUNE_REASON_LABELS["missing"]
    assert metadata["pruned_sample_limit"] == MAX_PRUNE_SAMPLES
    assert metadata["corrupt"] is False


def test_pruning_detail_is_empty_when_nothing_changed(tmp_path: Path) -> None:
    session_path = tmp_path / "state" / "review.json"
    save_review_session(_result(tmp_path), session_path)

    loaded = load_review_session(session_path)

    assert loaded.pruned_files == 0
    assert loaded.pruned_reasons == {}
    assert loaded.pruned_samples == []
    assert loaded.metadata()["pruned_reason_labels"] == {}


def test_corrupt_session_metadata_flags_the_unreadable_file(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not-json", encoding="utf-8")

    metadata = load_review_session(corrupt).metadata()

    assert metadata["corrupt"] is True
    assert metadata["available"] is False
    assert metadata["error"]
    # A missing file is simply "no saved review", never a corruption warning.
    assert load_review_session(tmp_path / "absent.json").metadata()["corrupt"] is False


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
