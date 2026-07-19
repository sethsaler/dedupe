"""Grouping and smart-select tests."""

from __future__ import annotations

from dedupe.actions import collect_selected_paths
from dedupe.grouping import (
    apply_smart_select,
    build_groups,
    build_no_human_groups,
    pick_suggested_keep,
)
from dedupe.human_detection import human_detection_signature
from dedupe.models import (
    FileRecord,
    GroupKind,
    MediaType,
    ReviewGroup,
    ScanResult,
    SmartRule,
)


def _rec(path: str, size: int, mtime: float, w: int = 100, h: int = 100) -> FileRecord:
    return FileRecord(
        path=path,
        size=size,
        mtime=mtime,
        media_type=MediaType.IMAGE,
        extension=".jpg",
        width=w,
        height=h,
    )


def _mark_no_person(record: FileRecord) -> FileRecord:
    record.human_detection_status = "no_person_detected"
    record.human_detection_signature = human_detection_signature()
    return record


def test_suggested_keep_prefers_resolution() -> None:
    low = _rec("/a/small.jpg", size=5000, mtime=10, w=100, h=100)
    high = _rec("/b/big.jpg", size=4000, mtime=5, w=4000, h=3000)
    assert pick_suggested_keep([low, high]) == high.path


def test_smart_select_always_keeps_one() -> None:
    a = _rec("/a.jpg", 100, 1)
    b = _rec("/b.jpg", 200, 2)
    groups = build_groups([[a, b]], [])
    assert len(groups) == 1
    g = groups[0]
    apply_smart_select(g, SmartRule.AUTOMATIC)
    assert len(g.selected_for_removal) == 1
    assert g.suggested_keep not in g.selected_for_removal

    apply_smart_select(g, SmartRule.NEWEST)
    assert g.suggested_keep == b.path

    apply_smart_select(g, SmartRule.OLDEST)
    assert g.suggested_keep == a.path

    apply_smart_select(g, SmartRule.DESELECT_ALL)
    assert g.selected_for_removal == []


def test_collect_selected_never_empties_group() -> None:
    a = _rec("/a.jpg", 100, 1)
    b = _rec("/b.jpg", 200, 2)
    groups = build_groups([[a, b]], [])
    g = groups[0]
    # Force bad selection of everything
    g.selected_for_removal = [a.path, b.path]
    selected = collect_selected_paths(groups)
    assert len(selected) == 1
    assert g.suggested_keep not in selected or len(selected) < 2


def test_similar_subset_of_exact_skipped() -> None:
    a = _rec("/a.jpg", 100, 1)
    b = _rec("/b.jpg", 100, 2)
    groups = build_groups([[a, b]], [[a, b]])
    # Should only produce one group (exact), not a redundant similar
    assert len(groups) == 1
    assert groups[0].kind.value == "exact"


def test_no_human_candidate_can_be_selected_for_removal_by_itself() -> None:
    candidate = _mark_no_person(_rec("/landscape.jpg", 300, 1))
    group = build_no_human_groups([candidate])[0]

    assert group.kind.value == "no_humans"
    assert group.suggested_keep is None
    assert group.reclaimable_bytes == 0
    assert collect_selected_paths([group]) == []

    apply_smart_select(group, SmartRule.SELECT_CANDIDATES)
    assert group.reclaimable_bytes == candidate.size
    assert collect_selected_paths([group]) == [candidate.path]

    apply_smart_select(group, SmartRule.DESELECT_ALL)
    assert group.selected_for_removal == []


def test_overlapping_no_human_selection_still_retains_a_duplicate() -> None:
    a = _rec("/landscape.jpg", 300, 1)
    b = _rec("/landscape-copy.jpg", 300, 2)
    duplicate = build_groups([[a, b]], [])[0]
    _mark_no_person(a)
    _mark_no_person(b)
    candidate_group = build_no_human_groups([a, b])[0]
    apply_smart_select(candidate_group, SmartRule.SELECT_CANDIDATES)

    selected = collect_selected_paths([duplicate, candidate_group])
    assert len(selected) == 1
    assert duplicate.suggested_keep not in selected


def test_no_human_groups_reject_positive_and_unverified_records() -> None:
    safe = _mark_no_person(_rec("/landscape.jpg", 300, 1))
    human = _rec("/portrait.jpg", 400, 2)
    human.human_detection_status = "person_detected"
    unverified = _rec("/unknown.jpg", 500, 3)
    stale = _rec("/old-result.jpg", 600, 4)
    stale.human_detection_status = "no_person_detected"
    stale.human_detection_signature = "human-presence-v1|opencv"

    groups = build_no_human_groups([safe, human, unverified, stale])

    assert len(groups) == 1
    assert groups[0].members == [safe]


def test_loaded_no_human_group_drops_positive_records() -> None:
    safe = _mark_no_person(_rec("/landscape.jpg", 300, 1))
    human = _rec("/portrait.jpg", 400, 2)
    human.human_detection_status = "person_detected"
    raw = ReviewGroup(
        id="no-human-test",
        kind=GroupKind.NO_HUMANS,
        media_type=MediaType.IMAGE,
        members=[safe, human],
        selected_for_removal=[safe.path, human.path],
        reviewed_paths=[safe.path, human.path],
    ).to_dict()

    loaded = ReviewGroup.from_dict(raw)

    assert loaded.members == [safe]
    assert loaded.selected_for_removal == [safe.path]
    assert loaded.reviewed_paths == [safe.path]


def test_positive_record_in_manual_no_human_group_cannot_be_selected() -> None:
    human = _rec("/portrait.jpg", 400, 2)
    human.human_detection_status = "person_detected"
    group = ReviewGroup(
        id="unsafe-group",
        kind=GroupKind.NO_HUMANS,
        media_type=MediaType.IMAGE,
        members=[human],
        selected_for_removal=[human.path],
        reviewed_paths=[human.path],
    )

    assert collect_selected_paths([group]) == []
    assert group.reclaimable_bytes == 0


def test_loaded_scan_drops_non_human_group_with_only_stale_decisions() -> None:
    stale = _rec("/old-result.jpg", 600, 4)
    stale.human_detection_status = "no_person_detected"
    stale.human_detection_signature = "human-presence-v1|opencv"
    raw_group = ReviewGroup(
        id="stale-group",
        kind=GroupKind.NO_HUMANS,
        media_type=MediaType.IMAGE,
        members=[stale],
    )
    raw = ScanResult(roots=["/"], files=[stale], groups=[raw_group]).to_dict()

    loaded = ScanResult.from_dict(raw)

    assert loaded.groups == []
    assert loaded.no_human_files == 0
