"""Durable low-resolution keep decisions."""

from pathlib import Path

import pytest

from dedupe.keep_decisions import (
    MAX_KEEP_DECISIONS_BYTES,
    default_keep_decisions_path,
    kept_paths,
    load_keep_decisions,
    matches_keep_decision,
    update_keep_decisions,
)
from dedupe.models import FileRecord, MediaType


def _rec(
    path: str,
    size: int = 100,
    mtime_ns: int = 1_000_000_000,
    *,
    device: int | None = None,
    inode: int | None = None,
) -> FileRecord:
    return FileRecord(
        path=path,
        size=size,
        mtime=mtime_ns / 1_000_000_000,
        media_type=MediaType.IMAGE,
        extension=Path(path).suffix.lower(),
        mtime_ns=mtime_ns,
        device=device,
        inode=inode,
    )


def test_keep_decisions_round_trip_and_default_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    record = _rec("/photos/small.jpg")

    update_keep_decisions(keep=[record])

    assert default_keep_decisions_path() == tmp_path / "dedupe" / "keep-decisions.json"
    decisions = load_keep_decisions()
    assert matches_keep_decision(record, decisions)
    assert kept_paths([record]) == {record.path}


def test_keep_decision_is_invalidated_when_the_file_changes(tmp_path: Path) -> None:
    store = tmp_path / "keep-decisions.json"
    record = _rec("/photos/small.jpg")
    update_keep_decisions(keep=[record], path=store)

    edited = _rec("/photos/small.jpg", size=101)
    touched = _rec("/photos/small.jpg", mtime_ns=2_000_000_000)

    decisions = load_keep_decisions(store)
    assert matches_keep_decision(record, decisions)
    assert not matches_keep_decision(edited, decisions)
    assert not matches_keep_decision(touched, decisions)
    assert kept_paths([edited, touched], path=store) == set()


def test_clearing_a_keep_decision_removes_it(tmp_path: Path) -> None:
    store = tmp_path / "keep-decisions.json"
    record = _rec("/photos/small.jpg")
    update_keep_decisions(keep=[record], path=store)

    update_keep_decisions(clear=[record.path], path=store)

    assert load_keep_decisions(store) == {}
    assert kept_paths([record], path=store) == set()


def test_corrupt_or_missing_store_loads_as_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere.json"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    wrong_version = tmp_path / "wrong-version.json"
    wrong_version.write_text('{"version": 999, "decisions": {"/a.jpg": {}}}')

    assert load_keep_decisions(missing) == {}
    assert load_keep_decisions(corrupt) == {}
    assert load_keep_decisions(wrong_version) == {}


def test_update_refuses_to_overwrite_corrupt_or_oversized_store(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{corrupt-existing-data")
    record = _rec("/photos/small.jpg")

    with pytest.raises(OSError):
        update_keep_decisions(keep=[record], path=corrupt)
    assert corrupt.read_text() == "{corrupt-existing-data"

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (MAX_KEEP_DECISIONS_BYTES + 1))
    with pytest.raises(OSError):
        update_keep_decisions(keep=[record], path=oversized)
    assert oversized.stat().st_size == MAX_KEEP_DECISIONS_BYTES + 1


def test_keep_decision_falls_back_to_device_inode_after_rename(tmp_path: Path) -> None:
    store = tmp_path / "keep-decisions.json"
    original = _rec("/photos/old_name.jpg", size=250, mtime_ns=5_000_000_000, device=7, inode=99)
    update_keep_decisions(keep=[original], path=store)

    renamed = _rec("/photos/archive/new_name.jpg", size=250, mtime_ns=5_000_000_000, device=7, inode=99)
    edited_renamed = _rec(
        "/photos/archive/new_name.jpg", size=251, mtime_ns=5_000_000_000, device=7, inode=99
    )
    different_inode = _rec(
        "/photos/archive/other.jpg", size=250, mtime_ns=5_000_000_000, device=7, inode=100
    )

    decisions = load_keep_decisions(store)
    assert matches_keep_decision(renamed, decisions) is True
    assert matches_keep_decision(edited_renamed, decisions) is False
    assert matches_keep_decision(different_inode, decisions) is False
    assert kept_paths([renamed, edited_renamed, different_inode], path=store) == {renamed.path}


def test_concurrent_updates_preserve_all_decisions_with_flock(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = tmp_path / "keep-decisions.json"
    records = [_rec(f"/photos/{i}.jpg", size=100 + i, device=1, inode=1000 + i) for i in range(12)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda rec: update_keep_decisions(keep=[rec], path=store), records))

    decisions = load_keep_decisions(store)
    assert set(decisions) == {rec.path for rec in records}
