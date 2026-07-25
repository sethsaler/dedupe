"""Action dry-run / quarantine tests."""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

import pytest

import dedupe.actions as actions_module
import dedupe.receipts as receipts_module
from dedupe.actions import apply_actions, undo_quarantine
from dedupe.exact import file_sha256
from dedupe.grouping import apply_smart_select, build_groups, build_no_human_groups
from dedupe.human_detection import human_detection_signature
from dedupe.models import DuplicateGroup, FileRecord, GroupKind, MediaType, SmartRule


def _rec(path: Path, data: bytes) -> FileRecord:
    path.write_bytes(data)
    st = path.stat()
    return FileRecord(
        path=str(path.resolve()),
        size=st.st_size,
        mtime=st.st_mtime,
        media_type=MediaType.IMAGE,
        extension=path.suffix.lower(),
    )


def test_quarantine_dry_run(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same")
    b = _rec(tmp_path / "b.jpg", b"same")
    groups = build_groups([[a, b]], [])
    q = tmp_path / "quarantine"
    result = apply_actions(groups, action="quarantine", quarantine_dir=q, dry_run=True)
    assert result.success_count == 1
    assert not q.exists() or not any(q.iterdir()) if q.exists() else True
    # originals untouched
    assert Path(a.path).exists() and Path(b.path).exists()


def test_quarantine_execute(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    q = tmp_path / "quarantine"
    result = apply_actions(groups, action="quarantine", quarantine_dir=q, dry_run=False)
    assert result.success_count == 1
    assert q.exists()
    remaining = [p for p in (Path(a.path), Path(b.path)) if p.exists()]
    assert len(remaining) == 1
    assert len(list(q.iterdir())) == 1


def test_execute_refuses_when_selected_file_changed_after_scan(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    selected = Path(groups[0].selected_for_removal[0])
    selected.write_bytes(b"new unrelated content")

    result = apply_actions(
        groups,
        action="quarantine",
        quarantine_dir=tmp_path / "q",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )

    assert result.success_count == 0
    assert result.fail_count == 1
    assert selected.exists()
    assert "changed since scan" in (result.items[0].error or "")
    assert result.log_path and Path(result.log_path).exists()


def test_quarantine_receipt_can_restore_file(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    action = apply_actions(
        groups,
        action="quarantine",
        quarantine_dir=tmp_path / "q",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )
    moved_source = Path(action.items[0].path)
    moved_destination = Path(action.items[0].destination or "")
    assert not moved_source.exists() and moved_destination.exists()

    restored = undo_quarantine(action.log_path or "", dry_run=False)

    assert restored.success_count == 1
    assert moved_source.exists() and not moved_destination.exists()


def test_apply_actions_can_be_scoped_by_kind(tmp_path: Path) -> None:
    # Exact duplicate pair → one member auto-selected for removal.
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    (exact_group,) = build_groups([[a, b]], [])

    # A reviewed non-human candidate selected for removal.
    c = _rec(tmp_path / "landscape.jpg", b"scenery-bytes")
    c.human_detection_status = "no_person_detected"
    c.human_detection_signature = human_detection_signature()
    no_human_group = build_no_human_groups([c])[0]
    no_human_group.reviewed_paths = [c.path]
    apply_smart_select(no_human_group, SmartRule.SELECT_CANDIDATES)

    groups = [exact_group, no_human_group]

    everything = apply_actions(groups, action="trash", dry_run=True)
    assert everything.success_count == 2

    only_exact = apply_actions(groups, action="trash", dry_run=True, kinds={"exact"})
    assert only_exact.success_count == 1
    assert {item.path for item in only_exact.items} == set(exact_group.selected_for_removal)

    only_no_humans = apply_actions(
        groups, action="trash", dry_run=True, kinds={"no_humans"}
    )
    assert only_no_humans.success_count == 1
    assert {item.path for item in only_no_humans.items} == {c.path}


def test_non_human_scope_still_keeps_member_of_overlapping_exact_group(
    tmp_path: Path,
) -> None:
    a = _rec(tmp_path / "a.jpg", b"same")
    b = _rec(tmp_path / "b.jpg", b"same")
    (exact_group,) = build_groups([[a, b]], [])
    for record in (a, b):
        record.human_detection_status = "no_person_detected"
        record.human_detection_signature = human_detection_signature()
    no_human_group = build_no_human_groups([a, b])[0]
    no_human_group.reviewed_paths = [a.path, b.path]
    no_human_group.selected_for_removal = [a.path, b.path]

    result = apply_actions(
        [exact_group, no_human_group], action="trash", dry_run=True, kinds={"no_humans"}
    )

    assert result.success_count == 1
    assert exact_group.suggested_keep not in {item.path for item in result.items}


def test_selected_suggested_keeper_uses_and_validates_alternate(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same")
    b = _rec(tmp_path / "b.jpg", b"same")
    (group,) = build_groups([[a, b]], [])
    group.selected_for_removal = [group.suggested_keep or ""]
    alternate = next(member for member in group.members if member.path != group.suggested_keep)
    Path(alternate.path).write_bytes(b"changed")

    result = apply_actions(
        [group], action="quarantine", quarantine_dir=tmp_path / "q", dry_run=False
    )

    assert result.success_count == 0
    assert "retained member is stale" in (result.items[0].error or "")


def test_similar_group_refuses_stale_only_retained_member(tmp_path: Path) -> None:
    selected = _rec(tmp_path / "selected.jpg", b"one")
    retained = _rec(tmp_path / "retained.jpg", b"two")
    group = DuplicateGroup(
        id="similar",
        kind=GroupKind.SIMILAR,
        media_type=MediaType.IMAGE,
        members=[selected, retained],
        selected_for_removal=[selected.path],
        suggested_keep=retained.path,
    )
    Path(retained.path).write_bytes(b"stale retained file")

    result = apply_actions(
        [group], action="quarantine", quarantine_dir=tmp_path / "q", dry_run=False
    )

    assert result.success_count == 0
    assert "retained member is stale" in (result.items[0].error or "")


def test_revalidates_immediately_before_destructive_operation(
    tmp_path: Path, monkeypatch
) -> None:
    a = _rec(tmp_path / "a.jpg", b"same")
    b = _rec(tmp_path / "b.jpg", b"same")
    (group,) = build_groups([[a, b]], [])
    selected_path = group.selected_for_removal[0]
    original_validate = actions_module.validate_file_record
    selected_validations = 0

    def mutate_before_second_validation(record, roots=None):
        nonlocal selected_validations
        if record.path == selected_path:
            selected_validations += 1
            if selected_validations == 2:
                Path(record.path).write_bytes(b"mutated after batch preflight")
        return original_validate(record, roots)

    monkeypatch.setattr(actions_module, "validate_file_record", mutate_before_second_validation)
    result = apply_actions(
        [group], action="quarantine", quarantine_dir=tmp_path / "q", dry_run=False
    )

    assert selected_validations == 2
    assert result.success_count == 0
    assert Path(selected_path).exists()
    assert "changed since scan" in (result.items[0].error or "")


def _fake_send_to_trash(dest_dir: Path):
    """Redirect trash into a temp directory so tests don't pollute the real Trash."""

    def _send_to_trash(src: Path, batch=None) -> Path:
        if not src.exists():
            raise FileNotFoundError(str(src))
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / src.name
        n = 1
        stem = src.stem
        suffix = src.suffix
        while target.exists():
            target = dest_dir / f"{stem}_{n}{suffix}"
            n += 1
        shutil.move(str(src), str(target))
        return target

    return _send_to_trash


def test_similar_group_keeper_mtime_drift_still_deletes(tmp_path: Path, monkeypatch) -> None:
    """Keeper metadata drifts (mtime) but sha256 matches; selected similar is still removed."""
    trash_dir = tmp_path / "trash"
    monkeypatch.setattr(actions_module, "_send_to_trash", _fake_send_to_trash(trash_dir))

    keeper = _rec(tmp_path / "keeper.jpg", b"keeper-bytes")
    selected = _rec(tmp_path / "selected.jpg", b"selected-bytes")
    keeper.sha256 = file_sha256(keeper.path)
    selected.sha256 = file_sha256(selected.path)

    group = DuplicateGroup(
        id="similar-drift",
        kind=GroupKind.SIMILAR,
        media_type=MediaType.IMAGE,
        members=[keeper, selected],
        selected_for_removal=[selected.path],
        suggested_keep=keeper.path,
    )
    os.utime(keeper.path, None)

    preview = apply_actions(
        [group],
        action="trash",
        dry_run=True,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )
    assert preview.success_count == 1
    assert preview.fail_count == 0
    assert Path(selected.path).exists()

    executed = apply_actions(
        [group],
        action="trash",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )
    assert executed.success_count == 1
    assert executed.fail_count == 0
    assert not Path(selected.path).exists()
    assert Path(keeper.path).exists()
    assert len(list(trash_dir.iterdir())) == 1


def test_similar_group_keeper_mtime_drift_size_only_fallback(tmp_path: Path, monkeypatch) -> None:
    """Keeper has no sha256/phash; mtime drift with unchanged size is tolerated."""
    trash_dir = tmp_path / "trash"
    monkeypatch.setattr(actions_module, "_send_to_trash", _fake_send_to_trash(trash_dir))

    keeper = _rec(tmp_path / "keeper.jpg", b"keeper-bytes")
    selected = _rec(tmp_path / "selected.jpg", b"selected-bytes")
    group = DuplicateGroup(
        id="similar-size-only",
        kind=GroupKind.SIMILAR,
        media_type=MediaType.IMAGE,
        members=[keeper, selected],
        selected_for_removal=[selected.path],
        suggested_keep=keeper.path,
    )
    os.utime(keeper.path, None)

    preview = apply_actions(
        [group],
        action="trash",
        dry_run=True,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )
    assert preview.success_count == 1
    assert preview.fail_count == 0
    assert Path(selected.path).exists()

    executed = apply_actions(
        [group],
        action="trash",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )
    assert executed.success_count == 1
    assert executed.fail_count == 0
    assert not Path(selected.path).exists()
    assert Path(keeper.path).exists()
    assert len(list(trash_dir.iterdir())) == 1


def test_mixed_batch_isolates_bad_group_failure(tmp_path: Path, monkeypatch) -> None:
    """A missing/stale keeper fails only its group; the healthy group still succeeds."""
    trash_dir = tmp_path / "trash"
    monkeypatch.setattr(actions_module, "_send_to_trash", _fake_send_to_trash(trash_dir))

    a = _rec(tmp_path / "a.jpg", b"same1")
    b = _rec(tmp_path / "b.jpg", b"same1")
    c = _rec(tmp_path / "c.jpg", b"same2")
    d = _rec(tmp_path / "d.jpg", b"same2")

    bad_group = DuplicateGroup(
        id="bad",
        kind=GroupKind.EXACT,
        media_type=MediaType.IMAGE,
        members=[a, b],
        selected_for_removal=[b.path],
        suggested_keep=a.path,
    )
    good_group = DuplicateGroup(
        id="good",
        kind=GroupKind.EXACT,
        media_type=MediaType.IMAGE,
        members=[c, d],
        selected_for_removal=[d.path],
        suggested_keep=c.path,
    )
    Path(a.path).unlink()

    result = apply_actions(
        [bad_group, good_group],
        action="trash",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )

    assert result.success_count == 1
    assert result.fail_count == 1
    bad_item = next(item for item in result.items if item.path == b.path)
    good_item = next(item for item in result.items if item.path == d.path)
    assert not bad_item.ok
    assert good_item.ok
    assert "stale" in (bad_item.error or "").lower()
    assert Path(b.path).exists()
    assert not Path(d.path).exists()
    assert Path(c.path).exists()
    assert not Path(a.path).exists()
    assert len(list(trash_dir.iterdir())) == 1


def test_exact_group_keeper_rehash_on_mtime_drift(tmp_path: Path, monkeypatch) -> None:
    """Exact group: keeper mtime drifts but content/hash matches, duplicate still removed."""
    trash_dir = tmp_path / "trash"
    monkeypatch.setattr(actions_module, "_send_to_trash", _fake_send_to_trash(trash_dir))

    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    group = DuplicateGroup(
        id="exact-drift",
        kind=GroupKind.EXACT,
        media_type=MediaType.IMAGE,
        members=[a, b],
        selected_for_removal=[b.path],
        suggested_keep=a.path,
    )
    for member in group.members:
        member.sha256 = file_sha256(member.path)
    os.utime(a.path, None)

    preview = apply_actions(
        [group],
        action="trash",
        dry_run=True,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )
    assert preview.success_count == 1
    assert preview.fail_count == 0
    assert Path(b.path).exists()

    executed = apply_actions(
        [group],
        action="trash",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )
    assert executed.success_count == 1
    assert executed.fail_count == 0
    assert not Path(b.path).exists()
    assert Path(a.path).exists()
    assert len(list(trash_dir.iterdir())) == 1


def test_dry_run_receipts_are_marked_as_previews(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    logs = tmp_path / "logs"

    preview = apply_actions(
        groups, action="quarantine", quarantine_dir=tmp_path / "q",
        dry_run=True, log_dir=logs,
    )
    executed = apply_actions(
        groups, action="quarantine", quarantine_dir=tmp_path / "q",
        dry_run=False, log_dir=logs,
    )

    assert Path(preview.log_path).name.startswith("preview-")
    assert Path(executed.log_path).name.startswith("action-")


def test_quarantine_records_item_sizes(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])

    result = apply_actions(
        groups, action="quarantine", quarantine_dir=tmp_path / "q", dry_run=False
    )

    assert result.items[0].size == len(b"same-bytes")


def _cross_device(marker: str):
    def _device_of(path: Path):
        return 2 if marker in str(path) else 1

    return _device_of


def test_quarantine_refuses_cross_device_before_touching_anything(
    tmp_path: Path, monkeypatch
) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    monkeypatch.setattr(actions_module, "_device_of", _cross_device("vault"))

    result = apply_actions(
        groups,
        action="quarantine",
        quarantine_dir=tmp_path / "vault",
        dry_run=False,
        log_dir=tmp_path / "logs",
    )

    assert result.success_count == 0 and result.fail_count == 1
    assert "different volume" in (result.items[0].error or "")
    assert Path(a.path).exists() and Path(b.path).exists()


def test_cross_device_preflight_also_fails_the_dry_run(tmp_path: Path, monkeypatch) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    monkeypatch.setattr(actions_module, "_device_of", _cross_device("vault"))

    result = apply_actions(
        groups,
        action="quarantine",
        quarantine_dir=tmp_path / "vault",
        dry_run=True,
        log_dir=tmp_path / "logs",
    )

    assert result.fail_count == 1
    assert "cross-device" in (result.items[0].error or "")


def test_quarantine_cross_device_opt_in_still_restores(tmp_path: Path, monkeypatch) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    monkeypatch.setattr(actions_module, "_device_of", _cross_device("vault"))

    result = apply_actions(
        groups,
        action="quarantine",
        quarantine_dir=tmp_path / "vault",
        dry_run=False,
        log_dir=tmp_path / "logs",
        allow_cross_device=True,
    )

    assert result.success_count == 1
    restored = undo_quarantine(result.log_path, dry_run=False)
    assert restored.success_count == 1
    assert Path(result.items[0].path).exists()


def test_move_file_refuses_cross_device_and_keeps_source(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    dest = tmp_path / "out" / "src.bin"

    def _exdev(source, target):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(actions_module.os, "replace", _exdev)
    with pytest.raises(actions_module.CrossDeviceError):
        actions_module._move_file(src, dest)

    assert src.exists() and not dest.exists()

    actions_module._move_file(src, dest, allow_cross_device=True)
    assert not src.exists()
    assert dest.read_bytes() == b"payload"


def _quarantined(tmp_path: Path):
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    return apply_actions(
        groups,
        action="quarantine",
        quarantine_dir=tmp_path / "q",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )


def test_undo_refuses_when_original_path_is_occupied(tmp_path: Path) -> None:
    action = _quarantined(tmp_path)
    original = Path(action.items[0].path)
    original.write_bytes(b"something new")

    result = undo_quarantine(action.log_path, dry_run=False)

    assert result.success_count == 0 and result.fail_count == 1
    assert "already occupied" in (result.items[0].error or "")
    assert original.read_bytes() == b"something new"
    assert Path(action.items[0].destination).exists()


def test_undo_reports_missing_quarantined_file(tmp_path: Path) -> None:
    action = _quarantined(tmp_path)
    Path(action.items[0].destination).unlink()

    result = undo_quarantine(action.log_path, dry_run=False)

    assert result.fail_count == 1
    assert "no longer exists" in (result.items[0].error or "")


def test_undo_cancels_every_move_when_one_item_fails(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"one")
    b = _rec(tmp_path / "b.jpg", b"one")
    c = _rec(tmp_path / "c.jpg", b"two")
    d = _rec(tmp_path / "d.jpg", b"two")
    groups = build_groups([[a, b], [c, d]], [])
    action = apply_actions(
        groups,
        action="quarantine",
        quarantine_dir=tmp_path / "q",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )
    assert action.success_count == 2
    Path(action.items[0].path).write_bytes(b"blocking file")

    result = undo_quarantine(action.log_path, dry_run=False)

    assert result.success_count == 0
    assert result.fail_count == 2
    assert any("cancelled" in (item.error or "") for item in result.items)
    for item in action.items:
        assert Path(item.destination).exists()


def test_undo_skips_failed_items_in_a_partial_receipt(tmp_path: Path) -> None:
    action = _quarantined(tmp_path)
    receipt = json.loads(Path(action.log_path).read_text(encoding="utf-8"))
    receipt["items"].append(
        {
            "path": str(tmp_path / "never.jpg"),
            "ok": False,
            "action": "quarantine",
            "destination": None,
            "error": "file changed since scan",
            "group_id": None,
            "size": 10,
        }
    )
    receipt["fail_count"] = 1
    Path(action.log_path).write_text(json.dumps(receipt), encoding="utf-8")

    result = undo_quarantine(action.log_path, dry_run=False)

    assert result.success_count == 1 and result.fail_count == 0
    assert Path(action.items[0].path).exists()
    assert not (tmp_path / "never.jpg").exists()


def test_undo_accepts_a_receipt_id(tmp_path: Path) -> None:
    action = _quarantined(tmp_path)
    receipt_id = Path(action.log_path).stem

    result = undo_quarantine(
        receipt_id, dry_run=False, receipt_dir=tmp_path / "logs", log_dir=tmp_path / "logs"
    )

    assert result.success_count == 1
    assert Path(action.items[0].path).exists()


def test_undo_rejects_unknown_receipt_id(tmp_path: Path) -> None:
    with pytest.raises(receipts_module.ReceiptNotFoundError):
        undo_quarantine("nope-1234", receipt_dir=tmp_path / "logs")


def test_receipts_list_show_and_prune(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    action = _quarantined(tmp_path)
    apply_actions(
        build_groups([[_rec(tmp_path / "c.jpg", b"x2"), _rec(tmp_path / "d.jpg", b"x2")]], []),
        action="quarantine",
        quarantine_dir=tmp_path / "q",
        dry_run=True,
        log_dir=logs,
    )

    summaries = receipts_module.list_receipts(logs)
    assert len(summaries) == 2
    assert summaries[0].started_at >= summaries[1].started_at
    executed = [s for s in summaries if s.executed]
    assert len(executed) == 1
    assert executed[0].undoable is True
    assert executed[0].bytes == len(b"same-bytes")
    preview = [s for s in summaries if s.dry_run][0]
    assert preview.undoable is False
    assert "dry-run" in (preview.undo_blocked_reason or "")

    assert receipts_module.list_receipts(logs, include_previews=False) == executed
    assert receipts_module.list_receipts(logs, undoable_only=True) == executed

    loaded = receipts_module.load_receipt(executed[0].id, logs)
    assert loaded["action"] == "quarantine"
    assert loaded["log_path"] == action.log_path

    # Ids resolve from a unique fragment too (e.g. the session prefix).
    assert receipts_module.resolve_receipt_path(
        executed[0].session_id[:8], logs
    ) == Path(action.log_path)

    preview_only = receipts_module.prune_receipts(logs, drop_previews=True, dry_run=True)
    assert preview_only.removed_count == 1
    assert Path(preview.log_path).exists()

    dropped = receipts_module.prune_receipts(logs, drop_previews=True, dry_run=False)
    assert dropped.removed_count == 1 and dropped.kept_count == 1
    assert not Path(preview.log_path).exists()
    assert dropped.freed_bytes > 0

    kept = receipts_module.prune_receipts(logs, keep=0, dry_run=False)
    assert kept.removed_count == 1
    assert receipts_module.list_receipts(logs) == []


def test_receipts_prune_by_age(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    _quarantined(tmp_path)
    summary = receipts_module.list_receipts(logs)[0]
    data = json.loads(Path(summary.log_path).read_text(encoding="utf-8"))
    data["started_at"] = "2001-01-01T00:00:00+00:00"
    Path(summary.log_path).write_text(json.dumps(data), encoding="utf-8")

    assert receipts_module.prune_receipts(logs, older_than_days=100000).removed_count == 0
    aged = receipts_module.prune_receipts(logs, older_than_days=1, dry_run=False)
    assert aged.removed_count == 1
    assert receipts_module.list_receipts(logs) == []


def test_receipts_ignores_unreadable_files(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "action-broken.json").write_text("{not json", encoding="utf-8")
    (logs / "unrelated.txt").write_text("hi", encoding="utf-8")

    assert receipts_module.list_receipts(logs) == []
    assert receipts_module.list_receipts(tmp_path / "missing") == []


def test_large_selection_is_ordered_and_complete(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    pairs = []
    for i in range(200):
        payload = f"payload-{i:04d}".encode()
        pairs.append([_rec(src / f"a{i:04d}.jpg", payload), _rec(src / f"b{i:04d}.jpg", payload)])
    groups = build_groups(pairs, [])
    expected = actions_module.collect_selected_paths(groups)
    assert len(expected) == 200

    result = apply_actions(
        groups,
        action="quarantine",
        quarantine_dir=tmp_path / "q",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )

    assert result.success_count == 200 and result.fail_count == 0
    assert [item.path for item in result.items] == expected
    destinations = [item.destination for item in result.items]
    assert len(set(destinations)) == 200
    assert all(Path(d).is_file() for d in destinations)
    assert all(not Path(p).exists() for p in expected)

    restored = undo_quarantine(result.log_path, dry_run=False)
    assert restored.success_count == 200
    assert all(Path(p).is_file() for p in expected)


def test_concurrent_executes_on_the_same_selection_move_each_file_once(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    quarantine = tmp_path / "q"
    barrier = threading.Barrier(2)
    guard = threading.Lock()
    outcomes = []

    def execute() -> None:
        barrier.wait(5)
        result = apply_actions(
            groups,
            action="quarantine",
            quarantine_dir=quarantine,
            dry_run=False,
            roots=[str(tmp_path)],
            log_dir=tmp_path / "logs",
        )
        with guard:
            outcomes.append(result)

    threads = [threading.Thread(target=execute) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert len(outcomes) == 2
    assert sum(result.success_count for result in outcomes) == 1
    assert sum(result.fail_count for result in outcomes) == 1
    assert len(list(quarantine.iterdir())) == 1
    assert len([p for p in (Path(a.path), Path(b.path)) if p.exists()]) == 1
