"""Regression tests for merging monitor scans with saved review progress."""

from pathlib import Path

from dedupe.jobs import file_identity, merge_new_arrival_review
from dedupe.models import FileRecord, GroupKind, MediaType, ReviewGroup, ScanResult


def _record(
    path: str,
    inode: int,
    *,
    mtime: float = 1000.0,
    width=None,
    ctime_ns: int | None = None,
) -> FileRecord:
    return FileRecord(
        path=path,
        size=100,
        mtime=mtime,
        media_type=MediaType.IMAGE,
        extension=Path(path).suffix,
        device=1,
        inode=inode,
        mtime_ns=int(mtime * 1_000_000_000),
        ctime_ns=ctime_ns if ctime_ns is not None else int(mtime * 1_000_000_000),
        width=width,
    )


def _group(kind: GroupKind, members: list[FileRecord], **kwargs) -> ReviewGroup:
    return ReviewGroup(
        id=f"{kind.value}-test",
        kind=kind,
        media_type=MediaType.IMAGE,
        members=members,
        **kwargs,
    )


def _baseline(*records: FileRecord) -> dict:
    return {record.path: file_identity(record) for record in records}


def test_merge_preserves_saved_deselections_and_member_analysis() -> None:
    a = _record("/a.jpg", 1)
    b_old = _record("/b.jpg", 2, width=640)
    b_current = _record("/b.jpg", 2)
    c = _record("/c.jpg", 3)
    previous = ScanResult(
        roots=["/"], files=[a, b_old], groups=[
            _group(GroupKind.EXACT, [a, b_old], reviewed_paths=[b_old.path])
        ],
    )
    result = ScanResult(
        roots=["/"], files=[a, b_current, c], groups=[
            _group(GroupKind.EXACT, [a, b_current, c],
                   selected_for_removal=[b_current.path, c.path])
        ],
    )

    merge_new_arrival_review(result, previous, _baseline(a, b_old))

    (merged,) = [group for group in result.groups if group.kind == GroupKind.EXACT]
    assert {member.path for member in merged.members} == {a.path, b_current.path, c.path}
    assert merged.selected_for_removal == [c.path]
    assert next(member for member in merged.members if member.path == b_current.path).width == 640


def test_changed_file_does_not_join_its_old_and_new_exact_groups() -> None:
    x_old = _record("/x.jpg", 1, mtime=1000)
    x_new = _record("/x.jpg", 10, mtime=2000)
    y = _record("/y.jpg", 2)
    z = _record("/z.jpg", 3)
    previous = ScanResult(
        roots=["/"], files=[x_old, y], groups=[_group(GroupKind.EXACT, [x_old, y])]
    )
    result = ScanResult(
        roots=["/"], files=[x_new, y, z], groups=[_group(GroupKind.EXACT, [x_new, z])]
    )

    merge_new_arrival_review(result, previous, _baseline(x_old, y, z))

    exact = [group for group in result.groups if group.kind == GroupKind.EXACT]
    assert len(exact) == 1
    assert {member.path for member in exact[0].members} == {x_new.path, z.path}


def test_independent_merge_preserves_crop_adjustments_and_analysis() -> None:
    old = _record("/old.jpg", 1, width=1200)
    old.human_detection_status = "no_person"
    current_old_inventory = _record("/old.jpg", 1)
    arrival = _record("/arrival.jpg", 2)
    previous = ScanResult(
        roots=["/"], files=[old], groups=[
            _group(
                GroupKind.CROP,
                [old],
                match_details={"adjusted_crops": {old.path: "10,10,100,100,1200,800"}},
            )
        ],
    )
    result = ScanResult(
        roots=["/"], files=[current_old_inventory, arrival], groups=[
            _group(GroupKind.CROP, [arrival], match_details={"adjusted_crops": {}})
        ],
    )

    merge_new_arrival_review(result, previous, _baseline(old))

    (crop,) = [group for group in result.groups if group.kind == GroupKind.CROP]
    assert {member.path for member in crop.members} == {old.path, arrival.path}
    assert crop.match_details["adjusted_crops"][old.path] == "10,10,100,100,1200,800"
    carried = next(member for member in crop.members if member.path == old.path)
    assert carried.width == 1200
    assert carried.human_detection_status == "no_person"


def test_merge_preserves_unmarked_manual_deselection() -> None:
    keeper = _record("/z.jpg", 1)
    copy_old = _record("/a.jpg", 2)
    copy_current = _record("/a.jpg", 2)
    arrival = _record("/0.jpg", 3)
    previous = ScanResult(
        roots=["/"], files=[keeper, copy_old], groups=[
            # Automatic would select copy_old because keeper ranks first; the empty
            # saved selection is therefore itself the user's keep decision.
            _group(GroupKind.EXACT, [keeper, copy_old], selected_for_removal=[])
        ],
    )
    result = ScanResult(
        roots=["/"], files=[keeper, copy_current, arrival], groups=[
            _group(GroupKind.EXACT, [keeper, copy_current, arrival],
                   selected_for_removal=[copy_current.path, arrival.path])
        ],
    )

    merge_new_arrival_review(result, previous, _baseline(keeper, copy_old))

    (merged,) = [group for group in result.groups if group.kind == GroupKind.EXACT]
    assert merged.selected_for_removal == [arrival.path]


def test_replacement_does_not_inherit_crop_decision_or_adjustment() -> None:
    old = _record("/photo.jpg", 1, mtime=1000, width=1200)
    replacement = _record("/photo.jpg", 9, mtime=2000)
    previous = ScanResult(
        roots=["/"], files=[old], groups=[
            _group(
                GroupKind.CROP,
                [old],
                selected_for_removal=[old.path],
                reviewed_paths=[old.path],
                match_details={"adjusted_crops": {old.path: "10,10,100,100,1200,800"}},
            )
        ],
    )
    result = ScanResult(
        roots=["/"], files=[replacement], groups=[
            _group(GroupKind.CROP, [replacement], match_details={"adjusted_crops": {}})
        ],
    )

    merge_new_arrival_review(result, previous, _baseline(old))

    (crop,) = [group for group in result.groups if group.kind == GroupKind.CROP]
    assert crop.selected_for_removal == []
    assert crop.reviewed_paths == []
    assert crop.match_details["adjusted_crops"] == {}
    assert crop.members[0].width is None


def test_undo_restored_crop_keeps_custom_adjustment() -> None:
    original = _record("/photo.jpg", 1, mtime=1000, width=1200)
    restored = _record("/photo.jpg", 22, mtime=2000, ctime_ns=2001, width=1200)
    identity = [
        restored.size,
        restored.mtime_sort_stamp,
        restored.ctime_ns,
        restored.device,
        restored.inode,
    ]
    previous = ScanResult(
        roots=["/"], files=[restored], groups=[
            _group(
                GroupKind.CROP,
                [restored],
                match_details={
                    "adjusted_crops": {restored.path: "10,10,100,100,1200,800"},
                    "restored_originals": {restored.path: identity},
                },
            )
        ],
    )
    fresh = _record("/photo.jpg", 22, mtime=2000, ctime_ns=2001)
    result = ScanResult(
        roots=["/"], files=[fresh], groups=[
            _group(GroupKind.CROP, [fresh], match_details={"adjusted_crops": {}})
        ],
    )

    merge_new_arrival_review(result, previous, _baseline(original))

    (crop,) = [group for group in result.groups if group.kind == GroupKind.CROP]
    assert crop.match_details["adjusted_crops"] == {
        restored.path: "10,10,100,100,1200,800"
    }


def test_independent_merge_keeps_a_saved_keep_decision_unselected() -> None:
    kept = _record("/kept.jpg", 1)
    pending = _record("/pending.jpg", 2)
    arrival = _record("/arrival.jpg", 3)
    previous = ScanResult(
        roots=["/"], files=[kept, pending], groups=[
            _group(GroupKind.LOW_RESOLUTION, [kept, pending], reviewed_paths=[kept.path])
        ],
    )
    result = ScanResult(
        roots=["/"], files=[kept, pending, arrival], groups=[
            _group(GroupKind.LOW_RESOLUTION, [kept, arrival],
                   selected_for_removal=[kept.path, arrival.path])
        ],
    )

    merge_new_arrival_review(result, previous, _baseline(kept, pending))

    (group,) = result.groups
    assert group.selected_for_removal == [arrival.path]
    assert kept.path in group.reviewed_paths
