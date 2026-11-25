"""Durable review-session persistence and stale-file tests."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path

from dedupe.grouping import build_groups
from dedupe.models import DuplicateGroup, FileRecord, MediaType, ScanResult
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


def test_saved_records_omit_only_unset_optional_values(tmp_path: Path) -> None:
    session_path = tmp_path / "state" / "review.json"
    result = _result(tmp_path)
    record = result.files[0]
    record.border_crop = ""
    record.face_count = 0
    record.capture_metadata = False

    save_review_session(result, session_path)
    envelope = json.loads(session_path.read_bytes())
    saved = envelope["result"]
    table = envelope["file_table"]
    assert isinstance(saved["files"][0], int)
    assert isinstance(saved["groups"][0]["members"][0], int)
    assert saved["files"][0] == saved["groups"][0]["members"][0]
    for idx in (saved["files"][0], saved["groups"][0]["members"][0]):
        row = table[idx]
        assert "phash" not in row
        assert row["border_crop"] == ""
        assert row["face_count"] == 0
        assert row["capture_metadata"] is False
    assert "phash" in result.to_dict()["files"][0]
    assert load_review_session(session_path).result.to_dict() == result.to_dict()


def test_legacy_session_checks_disk_once_per_path(tmp_path: Path, monkeypatch) -> None:
    from dedupe import actions

    session_path = tmp_path / "review.json"
    result = _result(tmp_path)
    # One stale inventory snapshot and one group-only record. Both current
    # group members must survive regardless of their inventory representation.
    result.files = [replace(result.files[0], size=0)]
    session_path.write_text(json.dumps({"version": 1, "result": result.to_dict()}))
    checked = []
    scoped_stat = actions._scoped_file_stat

    def count_stat(path, roots):
        checked.append(str(path))
        return scoped_stat(path, roots)

    monkeypatch.setattr(actions, "_scoped_file_stat", count_stat)
    loaded = load_review_session(session_path)
    assert loaded.error is None
    assert loaded.pruned_files == 0
    paths = [r.path for r in result.groups[0].members]
    assert sorted(checked) == sorted(paths)
    assert [r.path for r in loaded.result.groups[0].members] == paths


def test_distinct_participants_round_trip(tmp_path: Path) -> None:
    """Pair-level distinct decisions survive save/load so a later dissolve
    can still suppress every participant pair — including dismissed members."""
    session_path = tmp_path / "state" / "review.json"
    result = _result(tmp_path)
    dismissed = str(tmp_path / "dismissed.jpg")
    result.groups[0].distinct_participants = [result.files[0].path, dismissed]

    save_review_session(result, session_path)
    loaded = load_review_session(session_path)

    assert loaded.error is None
    assert loaded.result is not None
    # Dismissed members are no longer group members; participants must not be
    # pruned down to current membership.
    assert loaded.result.groups[0].distinct_participants == [
        result.files[0].path,
        dismissed,
    ]


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


def test_deleted_files_round_trip_survives_resume(tmp_path: Path) -> None:
    session_path = tmp_path / "state" / "review.json"
    result = _result(tmp_path)
    trashed = result.groups[0].members[0].path
    trash_dir = tmp_path / "Trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    trash_copy = trash_dir / "a.jpg"
    Path(trashed).replace(trash_copy)
    deleted = {trashed: str(trash_copy)}

    save_review_session(result, session_path, deleted_files=deleted)
    loaded = load_review_session(session_path)

    assert loaded.deleted_files == deleted
    assert loaded.unlocatable_deleted_files == {}
    assert loaded.pruned_files == 0
    member_paths = {member.path for member in loaded.result.groups[0].members}
    assert trashed in member_paths  # kept for the undo control, not pruned


def test_session_without_deleted_files_loads_an_empty_map(tmp_path: Path) -> None:
    session_path = tmp_path / "state" / "review.json"
    save_review_session(_result(tmp_path), session_path)

    loaded = load_review_session(session_path)

    assert loaded.deleted_files == {}


def test_file_table_deduplicates_records_and_supports_legacy_v1(tmp_path: Path) -> None:
    from dedupe.models import GroupKind, MediaType

    session_path = tmp_path / "review.json"
    result = _result(tmp_path)
    result.job_id = "job-42"
    # Mirror the same FileRecord across an additional ALL_FILES/CROP-style group.
    extra = DuplicateGroup(
        id="all-1",
        kind=GroupKind.ALL_FILES,
        media_type=MediaType.IMAGE,
        members=list(result.files),
        suggested_keep=None,
    )
    result.groups.append(extra)
    save_review_session(result, session_path, job_name="Weekly Cleanup")

    raw = json.loads(session_path.read_bytes())
    assert raw["version"] == 2
    assert len(raw["file_table"]) == len(result.files)
    assert raw["roots"] == result.roots
    assert raw["job_id"] == "job-42"
    assert raw["job_name"] == "Weekly Cleanup"
    assert raw["group_count"] == 2
    assert load_review_session(session_path).result.to_dict() == result.to_dict()

    # Legacy v1 envelope without file_table loads cleanly.
    v1_path = tmp_path / "legacy-v1.json"
    v1_path.write_text(
        json.dumps({"version": 1, "saved_at": "2025-01-01T00:00:00Z", "result": result.to_dict()})
    )
    v1_loaded = load_review_session(v1_path)
    assert v1_loaded.error is None
    assert v1_loaded.result.to_dict() == result.to_dict()


def test_peek_review_session_header_fast_path_and_oversized_guard(
    tmp_path: Path, monkeypatch
) -> None:
    import dedupe.review_session as rs_mod
    from dedupe.review_session import peek_review_session

    session_path = tmp_path / "large.json"
    result = _result(tmp_path)
    result.job_id = "job-fast"
    save_review_session(result, session_path, job_name="Fast Peek")

    # Force header-prefix path without needing a 64 MB disk fixture.
    monkeypatch.setattr(rs_mod, "FAST_FULL_PEEK_MAX_BYTES", 64)
    monkeypatch.setattr(rs_mod, "PEEK_HEADER_BYTES", 512)

    def _forbid_full_loads(*_args, **_kwargs):
        raise AssertionError("full json.loads must not run for large header peeks")

    monkeypatch.setattr(rs_mod.json, "loads", _forbid_full_loads)

    peeked = peek_review_session(session_path)
    assert peeked.available is True
    assert peeked.peeked is True
    assert peeked.roots == result.roots
    assert peeked.job_id == "job-fast"
    assert peeked.job_name == "Fast Peek"
    assert peeked.group_count == 1
    meta = peeked.metadata()
    assert meta["job_name"] == "Fast Peek"
    assert meta["group_count"] == 1
    assert meta["roots"] == result.roots

    # Oversized files above MAX_REVIEW_SESSION_BYTES fail cleanly without reading body.
    monkeypatch.setattr(rs_mod, "MAX_REVIEW_SESSION_BYTES", 10)
    oversized = peek_review_session(session_path)
    assert oversized.corrupt is True
    assert "exceeds" in (oversized.error or "")


def test_unlocatable_and_vanished_deleted_files_are_flagged_not_silently_kept(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "review.json"
    result = _result(tmp_path)
    a_path = result.groups[0].members[0].path
    b_path = result.groups[0].members[1].path
    Path(a_path).unlink()
    Path(b_path).unlink()
    deleted = {
        a_path: str(tmp_path / "Trash" / "missing.jpg"),
        b_path: "Trash",
    }
    save_review_session(result, session_path, deleted_files=deleted)

    loaded = load_review_session(session_path)
    assert loaded.deleted_files == {}
    assert loaded.unlocatable_deleted_files == deleted
    meta = loaded.metadata()
    assert meta["unlocatable_deleted_count"] == 2
    # Dead entries are persisted out cleanly on load.
    reloaded = load_review_session(session_path)
    assert reloaded.unlocatable_deleted_files == {}
    assert reloaded.deleted_files == {}


def test_encode_compact_result_memoizes_intern_by_record_identity(
    tmp_path: Path, monkeypatch
) -> None:
    from dedupe.models import DuplicateGroup, FileRecord, GroupKind, MediaType
    from dedupe.review_session import _encode_compact_result

    result = _result(tmp_path)
    # Add another group referencing the exact same FileRecord instances.
    result.groups.append(
        DuplicateGroup(
            id="all-files",
            kind=GroupKind.ALL_FILES,
            media_type=MediaType.IMAGE,
            members=list(result.files),
        )
    )
    calls = 0
    real_to_dict = FileRecord.to_dict

    def counted_to_dict(self, *, compact: bool = False):
        nonlocal calls
        calls += 1
        return real_to_dict(self, compact=compact)

    monkeypatch.setattr(FileRecord, "to_dict", counted_to_dict)
    file_table, compact = _encode_compact_result(result)
    assert len(file_table) == len(result.files)
    assert len(compact["files"]) == len(result.files)
    # Each shared FileRecord object is serialized once despite appearing in files + 2 groups.
    assert calls == len(result.files)
