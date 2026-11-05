"""Persistent history and failure-injected move recovery use disposable media only."""

import json
from pathlib import Path

import pytest

from dedupe import actions
from dedupe.action_journal import ActionJournal, recover_interrupted_actions
from dedupe.cache import HashCache
from dedupe.grouping import build_groups
from dedupe.history import list_history, reconsider_distinct_decision
from dedupe.models import FileRecord, MediaType


def pair(tmp_path):
    records = []
    for name in ("one.jpg", "two.jpg"):
        path = tmp_path / name
        path.write_bytes(b"same photo bytes")
        stat = path.stat()
        records.append(FileRecord(path=str(path), size=stat.st_size, mtime=stat.st_mtime,
                                  extension=".jpg", media_type=MediaType.IMAGE))
    return records, build_groups([records], [])


def history(tmp_path):
    return list_history(tmp_path / "logs", cache_path=tmp_path / "cache.sqlite3")


def test_receipt_is_durable_before_move_and_history_survives_restore(tmp_path, monkeypatch):
    _records, groups = pair(tmp_path)
    real_move = actions._move_file

    def inspect_move(source, destination, **kwargs):
        receipts = list((tmp_path / "logs").glob("action-*.json"))
        assert len(receipts) == 1
        data = json.loads(receipts[0].read_text())
        assert data["status"] == "running"
        event = json.loads(receipts[0].with_suffix(".journal").read_text().splitlines()[-1])
        assert event["entry"]["state"] == "moving"
        assert event["entry"]["identity"]["sha256"]
        real_move(source, destination, **kwargs)

    monkeypatch.setattr(actions, "_move_file", inspect_move)
    result = actions.apply_actions(groups, action="quarantine", quarantine_dir=tmp_path / "q",
                                   dry_run=False, log_dir=tmp_path / "logs", workers=1)
    assert history(tmp_path)["actions"][0]["undoable"]
    assert json.loads(Path(result.log_path).read_text())["status"] == "complete"
    monkeypatch.setattr(actions, "_move_file", real_move)
    restored = actions.undo_action(result.log_path, dry_run=False)
    assert restored.success_count == 1
    original = next(row for row in history(tmp_path)["actions"] if row["action"] == "quarantine")
    assert not original["undoable"]
    assert original["items"][0]["path"] == result.items[0].path


class SimulatedCrash(BaseException):
    pass


def test_crash_after_move_is_reconciled_without_moving_then_can_be_undone(tmp_path, monkeypatch):
    _records, groups = pair(tmp_path)
    source = Path(groups[0].selected_for_removal[0])
    real_move = actions._move_file

    def crash(source, destination, **kwargs):
        real_move(source, destination, **kwargs)
        raise SimulatedCrash()

    monkeypatch.setattr(actions, "_move_file", crash)
    with pytest.raises(SimulatedCrash):
        actions.apply_actions(groups, action="quarantine", quarantine_dir=tmp_path / "q",
                              dry_run=False, log_dir=tmp_path / "logs", workers=1)
    recovered = recover_interrupted_actions(tmp_path / "logs")
    assert len(recovered) == 1
    assert recovered[0]["status"] == "interrupted"
    assert recovered[0]["success_count"] == 1
    assert not source.exists()  # reconciliation never performs a restore
    assert not recover_interrupted_actions(tmp_path / "logs")  # idempotent
    monkeypatch.setattr(actions, "_move_file", real_move)
    undo = actions.undo_action(recovered[0]["log_path"], dry_run=False)
    assert undo.success_count == 1
    assert source.read_bytes() == b"same photo bytes"


def test_crash_before_move_keeps_original_and_no_restorable_item(tmp_path, monkeypatch):
    records, groups = pair(tmp_path)

    def crash(*_args, **_kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(actions, "_move_file", crash)
    with pytest.raises(SimulatedCrash):
        actions.apply_actions(groups, action="quarantine", quarantine_dir=tmp_path / "q",
                              dry_run=False, log_dir=tmp_path / "logs", workers=1)
    row = history(tmp_path)["actions"][0]
    assert not row["undoable"]
    assert row["success_count"] == 0
    assert all(Path(record.path).exists() for record in records)


def test_unrelated_identical_trash_file_is_never_adopted(tmp_path):
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"identical")
    trash = tmp_path / "trash"
    trash.mkdir()
    old = trash / source.name
    old.write_bytes(b"identical")
    journal = ActionJournal(actions.ActionResult(False, "trash").to_dict(),
                            [{"path": str(source), "destination": "Trash", "size": 9}],
                            tmp_path / "logs")
    journal.begin(0, search_dirs=[trash])
    source.unlink()
    journal.close()
    recovered = recover_interrupted_actions(tmp_path / "logs")[0]
    assert recovered["success_count"] == 0
    assert recovered["uncertain_count"] == 1
    assert old.exists()


def test_changed_destination_is_not_recovered(tmp_path):
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"original")
    destination = tmp_path / "moved.jpg"
    journal = ActionJournal(actions.ActionResult(False, "quarantine").to_dict(),
                            [{"path": str(source), "destination": str(destination), "size": 8}],
                            tmp_path / "logs")
    journal.begin(0)
    source.rename(destination)
    destination.write_bytes(b"modified")
    journal.close()
    recovered = recover_interrupted_actions(tmp_path / "logs")[0]
    assert recovered["success_count"] == 0
    assert recovered["uncertain_count"] == 1
    assert destination.read_bytes() == b"modified"


def test_active_journal_is_not_recovered(tmp_path):
    journal = ActionJournal(actions.ActionResult(False, "quarantine").to_dict(), [],
                            tmp_path / "logs")
    try:
        assert not recover_interrupted_actions(tmp_path / "logs")
        with pytest.raises(ValueError, match="still running"):
            actions.undo_action(journal.path)
    finally:
        journal.close()


def test_interrupted_copy_with_both_paths_present_needs_inspection(tmp_path):
    source = tmp_path / "original.jpg"
    destination = tmp_path / "copy.jpg"
    source.write_bytes(b"photo")
    journal = ActionJournal(actions.ActionResult(False, "quarantine").to_dict(),
                            [{"path": str(source), "destination": str(destination), "size": 5}],
                            tmp_path / "logs")
    journal.begin(0)
    destination.write_bytes(b"photo")
    journal.close()
    recovered = recover_interrupted_actions(tmp_path / "logs")[0]
    assert recovered["success_count"] == 0
    assert recovered["uncertain_count"] == 1
    assert source.exists() and destination.exists()


def test_unwritable_journal_refuses_moves(tmp_path, monkeypatch):
    records, groups = pair(tmp_path)

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr("dedupe.action_journal.atomic_json", fail)
    with pytest.raises(OSError, match="disk full"):
        actions.apply_actions(groups, action="quarantine", quarantine_dir=tmp_path / "q",
                              dry_run=False, log_dir=tmp_path / "logs")
    assert all(Path(record.path).exists() for record in records)


def test_exact_move_reuses_immediate_verified_hash_for_journal(tmp_path, monkeypatch):
    _records, groups = pair(tmp_path)

    def unexpected_reread(_path):
        raise AssertionError("journal should reuse the just-verified exact hash")

    monkeypatch.setattr("dedupe.action_journal.file_sha256", unexpected_reread)
    result = actions.apply_actions(groups, action="quarantine", quarantine_dir=tmp_path / "q",
                                   dry_run=False, log_dir=tmp_path / "logs")
    assert result.success_count == 1


def test_partial_last_journal_event_keeps_prior_move_intent(tmp_path):
    source = tmp_path / "original.jpg"
    destination = tmp_path / "moved.jpg"
    source.write_bytes(b"photo")
    journal = ActionJournal(actions.ActionResult(False, "quarantine").to_dict(),
                            [{"path": str(source), "destination": str(destination), "size": 5}],
                            tmp_path / "logs")
    journal.begin(0)
    source.rename(destination)
    with journal.events_path.open("a") as stream:
        stream.write('{"index":0,"entry":')
    journal.close()
    recovered = recover_interrupted_actions(tmp_path / "logs")[0]
    assert recovered["success_count"] == 1
    assert recovered["uncertain_count"] == 0


def test_distinct_decision_can_be_listed_and_reconsidered_after_restart(tmp_path):
    records, _groups = pair(tmp_path)
    cache_path = tmp_path / "cache.sqlite3"
    cache = HashCache(cache_path)
    cache.mark_distinct(records)
    cache.close()
    row = history(tmp_path)["decisions"][0]
    assert {row["path_a"], row["path_b"]} == {record.path for record in records}
    result = reconsider_distinct_decision(row["path_a"], row["path_b"], cache_path=cache_path)
    assert result["removed_pairs"] == 1
    assert history(tmp_path)["decision_count"] == 0


def test_undo_refuses_modified_moved_file(tmp_path):
    _records, groups = pair(tmp_path)
    result = actions.apply_actions(groups, action="quarantine", quarantine_dir=tmp_path / "q",
                                   dry_run=False, log_dir=tmp_path / "logs")
    moved = Path(result.items[0].destination)
    moved.write_bytes(b"new different file")
    undo = actions.undo_action(result.log_path, dry_run=False)
    assert undo.success_count == 0
    assert "changed since" in undo.items[0].error
    assert moved.exists()


def test_isolate_move_refuses_protected_library(tmp_path):
    records, groups = pair(tmp_path)
    records[0].protected = True
    result = actions.isolate_groups(groups, tmp_path / "review", mode="move", dry_run=False,
                                    log_dir=tmp_path / "logs")
    assert result.success_count == 0
    assert any("protected" in item.error for item in result.items)
    assert all(Path(record.path).exists() for record in records)
