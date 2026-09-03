"""Grouping and smart-select tests."""

from __future__ import annotations

import random

from dedupe.actions import collect_selected_paths
from dedupe.grouping import (
    apply_smart_select,
    build_all_files_groups,
    build_faces_groups,
    build_groups,
    build_low_resolution_groups,
    build_no_human_groups,
    build_random_review_groups,
    ensure_all_files_groups,
    pick_suggested_keep,
)
from dedupe.human_detection import human_detection_signature
from dedupe.models import (
    FileRecord,
    GroupKind,
    MediaType,
    ReviewGroup,
    ReviewPolicy,
    ScanResult,
    SmartRule,
    effective_selected_paths,
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


def test_similar_group_selects_lower_resolution_for_removal_by_default() -> None:
    low = _rec("/a/small.jpg", size=5000, mtime=10, w=100, h=100)
    high = _rec("/b/big.jpg", size=4000, mtime=5, w=4000, h=3000)

    group = build_groups([], [[low, high]])[0]

    assert group.kind == GroupKind.SIMILAR
    assert group.suggested_keep == high.path
    assert group.selected_for_removal == [low.path]


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
    assert group.reviewed_paths == []
    assert group.selected_for_removal == []

    group.reviewed_paths = [candidate.path]
    apply_smart_select(group, SmartRule.SELECT_CANDIDATES)
    assert group.reclaimable_bytes == candidate.size
    assert collect_selected_paths([group]) == [candidate.path]

    apply_smart_select(group, SmartRule.DESELECT_ALL)
    assert group.selected_for_removal == []


def test_low_resolution_candidates_include_images_gifs_and_videos_below_one_mp() -> None:
    image = _rec("/small.jpg", 300, 1, w=100, h=100)
    gif = _rec("/small.gif", 400, 2, w=200, h=200)
    gif.media_type = MediaType.GIF
    video = _rec("/small.mp4", 500, 3, w=640, h=360)
    video.media_type = MediaType.VIDEO
    boundary = _rec("/one-mp.jpg", 600, 4, w=1000, h=1000)
    unknown = _rec("/unknown.jpg", 700, 5, w=0, h=0)

    (group,) = build_low_resolution_groups([boundary, video, unknown, gif, image])

    assert group.kind == GroupKind.LOW_RESOLUTION
    assert group.media_type == MediaType.MIXED
    assert [member.path for member in group.members] == [image.path, gif.path, video.path]
    assert group.selected_for_removal == []
    assert group.reviewed_paths == []

    group.reviewed_paths = [video.path]
    group.selected_for_removal = [video.path]
    assert collect_selected_paths([group]) == [video.path]
    assert group.reclaimable_bytes == video.size


def test_low_resolution_candidates_can_be_filtered_by_media_type() -> None:
    image = _rec("/small.jpg", 300, 1, w=100, h=100)
    gif = _rec("/small.gif", 400, 2, w=200, h=200)
    gif.media_type = MediaType.GIF
    video = _rec("/small.mp4", 500, 3, w=640, h=360)
    video.media_type = MediaType.VIDEO

    (images,) = build_low_resolution_groups(
        [image, gif, video], media_types={MediaType.IMAGE}
    )
    (gifs,) = build_low_resolution_groups(
        [image, gif, video], media_types={MediaType.GIF}
    )
    (videos,) = build_low_resolution_groups(
        [image, gif, video], media_types={MediaType.VIDEO}
    )

    assert [member.path for member in images.members] == [image.path]
    assert [member.path for member in gifs.members] == [gif.path]
    assert [member.path for member in videos.members] == [video.path]
    assert build_low_resolution_groups([image], media_types=set()) == []


def test_low_resolution_candidates_use_per_type_pixel_bounds() -> None:
    image = _rec("/image.jpg", 300, 1, w=100, h=100)
    gif = _rec("/animation.gif", 400, 2, w=200, h=200)
    gif.media_type = MediaType.GIF
    video = _rec("/video.mp4", 500, 3, w=640, h=360)
    video.media_type = MediaType.VIDEO

    (group,) = build_low_resolution_groups(
        [image, gif, video],
        max_pixels_by_media_type={
            MediaType.IMAGE: 20_000,
            MediaType.GIF: 20_000,
            MediaType.VIDEO: 300_000,
        },
    )

    assert [member.path for member in group.members] == [image.path, video.path]


def test_low_resolution_groups_skip_paths_with_stored_keep_decisions() -> None:
    kept = _rec("/kept.jpg", 300, 1, w=100, h=100)
    fresh = _rec("/fresh.jpg", 400, 2, w=200, h=200)

    (group,) = build_low_resolution_groups([kept, fresh], skip_paths={kept.path})

    assert [member.path for member in group.members] == [fresh.path]
    assert build_low_resolution_groups([kept], skip_paths={kept.path}) == []


def test_random_review_is_unique_bounded_and_reproducible_with_seed() -> None:
    records = [_rec(f"/{index:03d}.jpg", 100 + index, index) for index in range(80)]

    first = build_random_review_groups(records, count=50, rng=random.Random(7))[0]
    second = build_random_review_groups(records, count=50, rng=random.Random(7))[0]

    assert first.kind == GroupKind.RANDOM_REVIEW
    assert len(first.members) == 50
    assert len({member.path for member in first.members}) == 50
    assert [member.path for member in first.members] == [
        member.path for member in second.members
    ]
    assert build_random_review_groups(records, count=0) == []


def test_independent_review_groups_dedupe_overlapping_scan_paths() -> None:
    record = _rec("/nested/photo.jpg", 100, 1, w=320, h=240)

    low_resolution = build_low_resolution_groups([record, record])[0]
    random_review = build_random_review_groups(
        [record, record], count=50, rng=random.Random(1)
    )[0]

    assert low_resolution.members == [record]
    assert random_review.members == [record]


def test_explicit_keep_vetoes_overlapping_duplicate_selection() -> None:
    first = _rec("/first.jpg", 100, 1)
    second = _rec("/second.jpg", 200, 2)
    duplicate = build_groups([[first, second]], [])[0]
    path = duplicate.selected_for_removal[0]
    random_review = build_random_review_groups([first, second], count=2)[0]
    random_review.reviewed_paths = [path]

    assert collect_selected_paths([duplicate, random_review]) == []
    assert effective_selected_paths(
        [duplicate],
        protection_groups=[duplicate, random_review],
    ) == []


def test_overlapping_no_human_selection_still_retains_a_duplicate() -> None:
    a = _rec("/landscape.jpg", 300, 1)
    b = _rec("/landscape-copy.jpg", 300, 2)
    duplicate = build_groups([[a, b]], [])[0]
    _mark_no_person(a)
    _mark_no_person(b)
    candidate_group = build_no_human_groups([a, b])[0]
    candidate_group.reviewed_paths = [a.path, b.path]
    apply_smart_select(candidate_group, SmartRule.SELECT_CANDIDATES)

    selected = collect_selected_paths([duplicate, candidate_group])
    assert len(selected) == 1
    assert duplicate.suggested_keep not in selected


def test_no_human_groups_reject_counted_female_faces() -> None:
    safe = _mark_no_person(_rec("/landscape.jpg", 300, 1))
    woman = _mark_no_person(_rec("/portrait.jpg", 400, 2))
    woman.face_count = 1
    woman.female_face_count = 1
    man = _mark_no_person(_rec("/group.jpg", 500, 3))
    man.face_count = 2
    man.male_face_count = 2
    man.female_face_count = 0

    groups = build_no_human_groups([safe, woman, man])

    assert len(groups) == 1
    assert groups[0].members == [safe]


def test_loaded_no_human_group_drops_female_face_records() -> None:
    safe = _mark_no_person(_rec("/landscape.jpg", 300, 1))
    woman = _mark_no_person(_rec("/portrait.jpg", 400, 2))
    woman.face_count = 1
    woman.female_face_count = 1
    raw = ReviewGroup(
        id="no-human-female",
        kind=GroupKind.NO_HUMANS,
        media_type=MediaType.IMAGE,
        members=[safe, woman],
        selected_for_removal=[safe.path, woman.path],
        reviewed_paths=[safe.path, woman.path],
    ).to_dict()

    loaded = ReviewGroup.from_dict(raw)

    assert loaded.members == [safe]
    assert loaded.selected_for_removal == [safe.path]
    assert woman.path not in loaded.selected_for_removal
    assert collect_selected_paths([loaded]) == [safe.path]


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


def test_no_human_candidates_form_one_mixed_media_collection() -> None:
    candidates = [
        _mark_no_person(_rec(f"/image-{index:03d}.jpg", 300, index))
        for index in range(120)
    ]
    video = _mark_no_person(_rec("/video.mp4", 500, 121))
    video.media_type = MediaType.VIDEO
    candidates.append(video)

    groups = build_no_human_groups(candidates)

    assert len(groups) == 1
    assert groups[0].media_type == MediaType.MIXED
    assert len(groups[0].members) == 121


def test_no_human_members_ordered_newest_mtime_first() -> None:
    older = _mark_no_person(_rec("/older.jpg", 300, mtime=10))
    newer = _mark_no_person(_rec("/newer.jpg", 300, mtime=30))
    mid = _mark_no_person(_rec("/mid.jpg", 300, mtime=20))
    # Same mtime: path is the stable tie-breaker.
    twin_b = _mark_no_person(_rec("/twin-b.jpg", 300, mtime=30))
    twin_a = _mark_no_person(_rec("/twin-a.jpg", 300, mtime=30))
    # Nanosecond precision beats coarser float seconds when available.
    ns_older = _mark_no_person(_rec("/ns-older.jpg", 300, mtime=40))
    ns_older.mtime_ns = 40_000_000_000
    ns_newer = _mark_no_person(_rec("/ns-newer.jpg", 300, mtime=40))
    ns_newer.mtime_ns = 40_000_000_500

    group = build_no_human_groups(
        [older, newer, mid, twin_b, twin_a, ns_older, ns_newer]
    )[0]

    assert [m.path for m in group.members] == [
        "/ns-newer.jpg",
        "/ns-older.jpg",
        "/newer.jpg",
        "/twin-a.jpg",
        "/twin-b.jpg",
        "/mid.jpg",
        "/older.jpg",
    ]

    loaded = ReviewGroup.from_dict(
        {
            "id": "no-human-order",
            "kind": "no_humans",
            "media_type": "image",
            "members": [
                m.to_dict()
                for m in [older, mid, twin_b, twin_a, newer, ns_older, ns_newer]
            ],
            "selected_for_removal": [],
            "reviewed_paths": [],
            "suggested_keep": None,
        }
    )
    assert [m.path for m in loaded.members] == [
        "/ns-newer.jpg",
        "/ns-older.jpg",
        "/newer.jpg",
        "/twin-a.jpg",
        "/twin-b.jpg",
        "/mid.jpg",
        "/older.jpg",
    ]


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


def test_build_faces_groups_orders_by_face_count_and_excludes_unanalyzed() -> None:
    busy = _rec("/busy.jpg", 500, 5)
    busy.face_count = 3
    pair = _rec("/pair.jpg", 500, 9)
    pair.face_count = 2
    solo = _rec("/solo.jpg", 500, 1)
    solo.face_count = 1
    faceless = _rec("/faceless.jpg", 500, 20)
    faceless.face_count = 0
    unanalyzed = _rec("/unanalyzed.jpg", 500, 30)
    video = _rec("/clip.mp4", 500, 40)
    video.media_type = MediaType.VIDEO
    video.extension = ".mp4"
    video.face_count = 4

    groups = build_faces_groups([faceless, solo, video, busy, unanalyzed, pair])

    assert len(groups) == 1
    group = groups[0]
    assert group.kind == GroupKind.FACES
    # Videos with trusted face counts join the Faces review flow too.
    assert [member.path for member in group.members] == [
        "/clip.mp4",
        "/busy.jpg",
        "/pair.jpg",
        "/solo.jpg",
    ]
    assert group.selected_for_removal == []
    assert group.reviewed_paths == []

    assert build_faces_groups([faceless, unanalyzed]) == []


def test_loaded_scan_drops_faces_group_with_no_current_face_counts() -> None:
    with_faces = _rec("/faces.jpg", 600, 4)
    with_faces.face_count = 2
    lost_count = _rec("/lost.jpg", 600, 8)

    raw = ScanResult(
        roots=["/"],
        files=[with_faces, lost_count],
        groups=[
            ReviewGroup(
                id="faces-group",
                kind=GroupKind.FACES,
                media_type=MediaType.IMAGE,
                members=[with_faces, lost_count],
            )
        ],
    ).to_dict()

    loaded = ScanResult.from_dict(raw)

    assert len(loaded.groups) == 1
    assert [member.path for member in loaded.groups[0].members] == ["/faces.jpg"]
    assert loaded.faces_files == 1

    stale_only = dict(raw)
    stale_only["groups"] = [
        ReviewGroup(
            id="stale-faces",
            kind=GroupKind.FACES,
            media_type=MediaType.IMAGE,
            members=[lost_count],
        ).to_dict()
    ]
    assert ScanResult.from_dict(stale_only).groups == []


def test_build_all_files_groups_covers_every_file_once_per_root() -> None:
    a = _rec("/root-a/z.jpg", 100, 1)
    b = _rec("/root-a/a.jpg", 200, 2)
    c = _rec("/root b/q.jpg", 300, 3)
    video = _rec("/root b/clip.mp4", 400, 4)
    video.media_type = MediaType.VIDEO
    video.extension = ".mp4"

    groups = build_all_files_groups([a, b, c, video], ["/root-a", "/root b"])

    assert [group.root for group in groups] == ["/root-a", "/root b"]
    first, second = groups
    assert first.kind == GroupKind.ALL_FILES
    assert first.policy == ReviewPolicy.INDEPENDENT_CANDIDATES
    # Members are path-ordered so sifting follows the folder structure.
    assert [member.path for member in first.members] == [
        "/root-a/a.jpg",
        "/root-a/z.jpg",
    ]
    assert first.media_type == MediaType.IMAGE
    assert [member.path for member in second.members] == [
        "/root b/clip.mp4",
        "/root b/q.jpg",
    ]
    assert second.media_type == MediaType.MIXED
    # Browse groups start without selections or review progress.
    assert first.selected_for_removal == []
    assert first.reviewed_paths == []


def test_build_all_files_groups_assigns_overlapping_roots_to_the_deeper_one() -> None:
    inner = _rec("/lib/wedding/raw/img.jpg", 100, 1)
    outer = _rec("/lib/other.jpg", 100, 1)

    groups = build_all_files_groups([inner, outer], ["/lib", "/lib/wedding"])

    by_root = {group.root: group for group in groups}
    assert [member.path for member in by_root["/lib"].members] == ["/lib/other.jpg"]
    assert [member.path for member in by_root["/lib/wedding"].members] == [
        "/lib/wedding/raw/img.jpg",
    ]


def test_build_all_files_groups_skips_empty_roots() -> None:
    member = _rec("/scanned/a.jpg", 100, 1)

    groups = build_all_files_groups([member], ["/scanned", "/empty"])

    assert [group.root for group in groups] == ["/scanned"]
    assert build_all_files_groups([], ["/scanned"]) == []


def test_ensure_all_files_groups_upgrades_older_results_once() -> None:
    records = [_rec("/root/a.jpg", 100, 1), _rec("/root/b.jpg", 200, 2)]
    result = ScanResult(roots=["/root"], files=records, groups=[])

    ensure_all_files_groups(result)

    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.kind == GroupKind.ALL_FILES
    assert group.root == "/root"
    assert len(group.members) == 2

    # Existing groups (and their review progress) survive; nothing duplicates.
    group.reviewed_paths = ["/root/a.jpg"]
    ensure_all_files_groups(result)
    assert len(result.groups) == 1
    assert result.groups[0].reviewed_paths == ["/root/a.jpg"]


def test_smart_select_rules_never_touch_all_files_groups() -> None:
    records = [_rec("/root/a.jpg", 100, 1), _rec("/root/b.jpg", 200, 2)]
    result = ScanResult(roots=["/root"], files=records, groups=[])
    ensure_all_files_groups(result)
    group = result.groups[0]
    group.reviewed_paths = ["/root/a.jpg"]

    for rule in SmartRule:
        apply_smart_select(group, rule)
        assert group.selected_for_removal == []
        assert group.suggested_keep is None


def test_all_files_group_survives_a_session_roundtrip() -> None:
    records = [_rec("/root/a.jpg", 100, 1)]
    result = ScanResult(roots=["/root"], files=records, groups=[])
    ensure_all_files_groups(result)

    loaded = ScanResult.from_dict(result.to_dict())

    assert [group.kind for group in loaded.groups] == [GroupKind.ALL_FILES]
    assert loaded.groups[0].policy == ReviewPolicy.INDEPENDENT_CANDIDATES
    assert [member.path for member in loaded.groups[0].members] == ["/root/a.jpg"]
