"""Action dry-run / quarantine tests."""

from __future__ import annotations

from pathlib import Path

import dedupe.actions as actions_module
from dedupe.actions import apply_actions, undo_quarantine
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
