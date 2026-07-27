"""Durable low-resolution keep decisions."""

from pathlib import Path

from dedupe.keep_decisions import (
    default_keep_decisions_path,
    kept_paths,
    load_keep_decisions,
    matches_keep_decision,
    update_keep_decisions,
)
from dedupe.models import FileRecord, MediaType


def _rec(path: str, size: int = 100, mtime_ns: int = 1_000_000_000) -> FileRecord:
    return FileRecord(
        path=path,
        size=size,
        mtime=mtime_ns / 1_000_000_000,
        media_type=MediaType.IMAGE,
        extension=Path(path).suffix.lower(),
        mtime_ns=mtime_ns,
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
