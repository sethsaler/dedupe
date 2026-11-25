"""Group construction, ranking, and smart-select rules."""

from __future__ import annotations

import hashlib
import random
import re
import threading
from collections.abc import Callable
from pathlib import Path

from .cache import DistinctReviews
from .human_policy import may_enter_no_person_review
from .models import (
    DuplicateGroup,
    FileRecord,
    GroupKind,
    MediaType,
    ReviewPolicy,
    ScanResult,
    SmartRule,
)

LOW_RESOLUTION_MAX_PIXELS = 1_000_000
DEFAULT_RANDOM_REVIEW_COUNT = 50

# Mirrors face_detection.FACE_MEDIA_TYPES; importing that module here would
# create a grouping -> face_detection -> human_detection -> similar_video ->
# grouping import cycle.
FACE_COUNTED_MEDIA_TYPES = (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO)


_keeper_metadata_lock = threading.Lock()

_RAW_IMAGE_EXTS = frozenset({".raw", ".dng", ".cr2", ".nef", ".arw"})
_LOSSLESS_IMAGE_EXTS = frozenset({".tif", ".tiff", ".png", ".bmp"})
_MODERN_IMAGE_EXTS = frozenset({".heic", ".heif", ".avif", ".webp"})
_JPEG_IMAGE_EXTS = frozenset({".jpg", ".jpeg"})

_EXPORT_NAME_RE = re.compile(
    r"(?:^|[ _-])(?:copy|edited|export|compressed|whatsapp|thumb|thumbnail|resized)(?:[ _.-]|\d|$)"
    r"|(?:^|[ _-])(?:IMG-\d+-WA\d+|IMG_E\d+)(?:[ _.-]|$)"
    r"|(?:^|[ _-])screenshot"
    r"|\(\d+\)",
    re.IGNORECASE,
)


def _has_capture_metadata(rec: FileRecord) -> bool:
    """Read and cache EXIF camera capture metadata without holding the lock on I/O."""
    if rec.capture_metadata is not None:
        return bool(rec.capture_metadata)
    has_meta = False
    if rec.media_type == MediaType.IMAGE:
        from .similar_image import ensure_heif_registered, get_cached_capture_metadata

        cached_meta = get_cached_capture_metadata(rec)
        if cached_meta is not None:
            has_meta = cached_meta
        else:
            try:
                from PIL import Image

                ensure_heif_registered()
                with Image.open(rec.path) as image:
                    exif = image.getexif()
                    capture = exif.get(36867)
                    if not capture and exif.get(34665):
                        capture = exif.get_ifd(34665).get(36867)
                    has_meta = bool(capture and (exif.get(271) or exif.get(272)))
            except Exception:
                pass
    with _keeper_metadata_lock:
        if rec.capture_metadata is None:
            rec.capture_metadata = has_meta
        return bool(rec.capture_metadata)


def _format_quality_tier(rec: FileRecord) -> int:
    if rec.media_type != MediaType.IMAGE:
        return 0
    ext = (rec.extension or Path(rec.path).suffix).lower()
    if ext in _RAW_IMAGE_EXTS:
        return 4
    if ext in _LOSSLESS_IMAGE_EXTS:
        return 3
    if ext in _MODERN_IMAGE_EXTS:
        return 2
    if ext in _JPEG_IMAGE_EXTS:
        return 1
    return 0


def _border_stats(rec: FileRecord) -> tuple[bool, bool, float]:
    """Higher is cleaner: (no_chrome, under_5pct_removed, -removed_fraction)."""
    crop = rec.border_crop
    if not crop or crop == "dismissed":
        return (True, True, 0.0)
    parts = crop.split(",")
    if len(parts) not in (6, 7):
        return (True, True, 0.0)
    has_chrome = len(parts) == 7 and bool(parts[6])
    raw = rec.pixels
    eff = rec.effective_pixels
    if raw <= 0 or eff >= raw:
        return (not has_chrome, True, 0.0)
    removed = 1.0 - (eff / raw)
    return (not has_chrome, removed < 0.05, round(-removed, 4))


def rank_keep_candidate(rec: FileRecord) -> tuple:
    """
    Higher is better for 'automatic' keep.
    Protection, folder preferences, effective resolution (with bounded EXIF/export
    weighting), format tier, sharpness, video audio/duration/bitrate, and clean
    borders precede size, date, and path tie-breakers.
    These hints never establish whether files are duplicates.
    """
    capture_meta = _has_capture_metadata(rec)
    ext = (rec.extension or Path(rec.path).suffix).lower()
    is_raw = rec.media_type == MediaType.IMAGE and ext in _RAW_IMAGE_EXTS
    camera_origin = capture_meta or is_raw
    export_name = bool(_EXPORT_NAME_RE.search(Path(rec.path).name))
    eff_px = rec.effective_pixels
    resolution_score = eff_px * (2.0 if camera_origin else 1.0) * (0.25 if export_name else 1.0)
    format_tier = _format_quality_tier(rec)
    has_sharpness = rec.sharpness is not None
    sharpness_score = round(float(rec.sharpness), 2) if has_sharpness else 0.0
    if rec.media_type == MediaType.VIDEO:
        audio_tier = 2 if rec.has_audio is True else (1 if rec.has_audio is None else 0)
        video_duration = round(float(rec.duration or 0.0), 1)
        video_bitrate = int(rec.video_bitrate or 0)
    else:
        audio_tier = 0
        video_duration = 0.0
        video_bitrate = 0
    path_depth = len(Path(rec.path).parts)
    name_len = len(Path(rec.path).name)
    return (
        rec.protected,
        rec.keeper_preference,
        resolution_score,
        camera_origin,
        format_tier,
        not export_name,
        _border_stats(rec),
        (has_sharpness, sharpness_score),
        (audio_tier, video_bitrate),
        rec.size,
        video_duration,
        rec.mtime,
        -path_depth,
        -name_len,
    )


def _keep_sort_key(rec: FileRecord) -> tuple:
    """Total order shared by clustering and suggested-keeper selection."""
    return (rank_keep_candidate(rec), rec.path)


def cluster_around_best(
    records: list[FileRecord],
    adjacency: dict[str, set[str]],
    distinct: DistinctReviews | None = None,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> list[list[FileRecord]]:
    """Build disjoint groups whose members all directly match the best-ranked member.

    Connected-component clustering is unsafe for fuzzy matches: A can match B and B
    can match C even when A does not match C. By choosing the likely keeper first and
    only attaching its verified neighbors, every automatically removable member has
    been compared directly with the file the UI will recommend keeping.
    """
    # Ranking reads capture metadata from the file, so only rank records that
    # can take part in a group: those with a match, or named as one. In a
    # library that is mostly unique files this skips nearly every file open.
    linked = {path for path, matches in adjacency.items() if matches}
    linked.update(path for matches in adjacency.values() for path in matches)
    sort_keys: dict[str, tuple] = {}
    linked_records: list[FileRecord] = []
    for record in records:
        if record.path not in linked:
            continue
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        sort_keys[record.path] = _keep_sort_key(record)
        linked_records.append(record)
    ordered = sorted(
        linked_records,
        key=lambda record: sort_keys[record.path],
        reverse=True,
    )
    by_path = {record.path: record for record in records}
    remaining = set(by_path)

    groups: list[list[FileRecord]] = []
    for keeper in ordered:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        if keeper.path not in remaining:
            continue
        matches = [
            by_path[path]
            for path in adjacency.get(keeper.path, set())
            if path in remaining
        ]
        if not matches:
            remaining.remove(keeper.path)
            continue
        matches.sort(
            key=lambda record: sort_keys[record.path],
            reverse=True,
        )
        members = [keeper]
        if distinct:
            for match in matches:
                if not any(distinct.is_distinct(match, by_path[member.path]) for member in members):
                    members.append(match)
        else:
            members.extend(matches)
        if len(members) < 2:
            remaining.remove(keeper.path)
            continue
        groups.append(members)
        remaining.difference_update(member.path for member in members)
    return groups


def pick_suggested_keep(members: list[FileRecord]) -> str:
    best = max(members, key=_keep_sort_key)
    return best.path


def make_group_id(kind: GroupKind, members: list[FileRecord]) -> str:
    paths = sorted(m.path for m in members)
    raw = f"{kind.value}|" + "|".join(paths)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def dominant_media_type(members: list[FileRecord]) -> MediaType:
    if not members:
        return MediaType.OTHER
    media_types = {m.media_type for m in members}
    if len(media_types) == 1:
        return next(iter(media_types))
    return MediaType.MIXED


def build_one_group(
    kind: GroupKind,
    members: list[FileRecord],
    *,
    exact_path_sets: list[set[str]] | None = None,
    match_basis: str | None = None,
    match_scores: dict[str, float] | None = None,
) -> DuplicateGroup | None:
    """Build a single DuplicateGroup, or None if members don't form a valid group."""
    by_path = {m.path: m for m in members}
    members = list(by_path.values())
    if len(members) < 2:
        return None
    if kind == GroupKind.SIMILAR and exact_path_sets:
        paths = {m.path for m in members}
        # Skip if this set is wholly contained in some exact group
        if any(paths <= eps for eps in exact_path_sets):
            return None
    keep = pick_suggested_keep(members)
    g = DuplicateGroup(
        id=make_group_id(kind, members),
        kind=kind,
        media_type=dominant_media_type(members),
        members=sorted(members, key=lambda m: m.path),
        suggested_keep=keep,
        anchor_path=keep,
        match_basis=match_basis,
        match_scores=dict(match_scores or {}),
    )
    apply_smart_select(g, SmartRule.AUTOMATIC)
    if match_basis in ("semantic", "video_version"):
        # Meaning matches include distinct shots of one subject (burst
        # frames, slides from one deck), so nothing is preselected for
        # removal: the reviewer opts each file in.
        apply_smart_select(g, SmartRule.DESELECT_ALL)
    return g


def _dedupe_exact_copies_in_similar(
    members: list[FileRecord],
    exact_path_to_group: dict[str, int],
) -> list[FileRecord]:
    """Keep at most one best-ranked representative per exact-identical cluster."""
    by_path = {m.path: m for m in members}
    buckets: dict[tuple, list[FileRecord]] = {}
    for member in by_path.values():
        exact_idx = exact_path_to_group.get(member.path)
        if exact_idx is not None:
            key: tuple = ("exact", exact_idx)
        elif member.sha256:
            key = ("sha256", member.sha256)
        else:
            key = ("path", member.path)
        buckets.setdefault(key, []).append(member)
    return [max(bucket, key=_keep_sort_key) for bucket in buckets.values()]


def build_groups(
    exact_member_lists: list[list[FileRecord]],
    similar_member_lists: list[list[FileRecord]],
) -> list[DuplicateGroup]:
    """Build DuplicateGroups. Similar groups deduplicate redundant exact copies."""
    groups: list[DuplicateGroup] = []
    exact_path_sets: list[set[str]] = []
    exact_path_to_group: dict[str, int] = {}

    for members in exact_member_lists:
        g = build_one_group(GroupKind.EXACT, members)
        if g is None:
            continue
        group_idx = len(exact_path_sets)
        groups.append(g)
        path_set = {m.path for m in g.members}
        exact_path_sets.append(path_set)
        for path in path_set:
            exact_path_to_group[path] = group_idx

    for members in similar_member_lists:
        # Drop redundant exact-duplicate copies from the similar group while
        # keeping cross-quality matches (one representative per exact set).
        deduped = _dedupe_exact_copies_in_similar(members, exact_path_to_group)
        g = build_one_group(
            GroupKind.SIMILAR, deduped, exact_path_sets=exact_path_sets
        )
        if g is None:
            continue
        groups.append(g)

    # Sort: most reclaimable first
    groups.sort(key=lambda g: g.reclaimable_bytes, reverse=True)
    return groups


def build_no_human_groups(members: list[FileRecord]) -> list[DuplicateGroup]:
    """Build one review collection from explicit no-person detector decisions.

    Members are ordered newest-first by mtime so the Non-Human review pages
    surface the most recently modified files first.
    """
    matching = sorted(
        (
            member
            for member in members
            if member.media_type in (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO)
            and may_enter_no_person_review(member)
        ),
        key=lambda member: (-member.mtime_sort_stamp, member.path),
    )
    if not matching:
        return []

    return [
        DuplicateGroup(
            id=make_group_id(GroupKind.NO_HUMANS, matching),
            kind=GroupKind.NO_HUMANS,
            media_type=dominant_media_type(matching),
            members=matching,
            selected_for_removal=[],
            reviewed_paths=[],
            suggested_keep=None,
        )
    ]


def build_low_resolution_groups(
    members: list[FileRecord],
    *,
    max_pixels: int = LOW_RESOLUTION_MAX_PIXELS,
    skip_paths: set[str] | None = None,
    media_types: set[MediaType] | None = None,
    max_pixels_by_media_type: dict[MediaType, int] | None = None,
) -> list[DuplicateGroup]:
    """Build one review collection for media below the configured pixel count.

    ``skip_paths`` holds files with a durable keep decision from an earlier
    review; they are never surfaced for low-resolution review again.
    """
    skip = skip_paths or set()
    allowed_types = media_types if media_types is not None else {
        MediaType.IMAGE,
        MediaType.GIF,
        MediaType.VIDEO,
    }
    bounds = max_pixels_by_media_type or {}
    matching = sorted(
        (
            member
            for member in members
            if member.media_type in allowed_types
            and 0 < member.pixels < bounds.get(member.media_type, max_pixels)
            and member.path not in skip
        ),
        key=lambda member: (member.pixels, member.path),
    )
    return _build_independent_group(GroupKind.LOW_RESOLUTION, matching)


def build_random_review_groups(
    members: list[FileRecord],
    *,
    count: int = DEFAULT_RANDOM_REVIEW_COUNT,
    rng: random.Random | None = None,
) -> list[DuplicateGroup]:
    """Build one freshly shuffled review collection of up to ``count`` media files."""
    if count <= 0:
        return []
    eligible = [
        member
        for member in members
        if member.media_type in (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO)
    ]
    eligible = list({member.path: member for member in eligible}.values())
    if not eligible:
        return []
    sampler = rng or random.SystemRandom()
    matching = sampler.sample(eligible, k=min(count, len(eligible)))
    return _build_independent_group(GroupKind.RANDOM_REVIEW, matching)


def build_faces_groups(members: list[FileRecord]) -> list[DuplicateGroup]:
    """Build one review collection of media with at least one detected face.

    Members are ordered busiest-shot first (highest face count, then newest),
    so group photos surface at the top of the Faces review pages.
    """
    matching = sorted(
        (
            member
            for member in members
            if member.media_type in FACE_COUNTED_MEDIA_TYPES
            and member.face_count is not None
            and member.face_count >= 1
        ),
        key=lambda member: (
            -member.face_count,
            -member.mtime_sort_stamp,
            member.path,
        ),
    )
    return _build_independent_group(GroupKind.FACES, matching)


def crop_review_order(member: FileRecord, box) -> tuple:
    """Sort key for the Crop review.

    Screenshots whose interface (status bar, navigation bar, menu bar) would
    be cropped lead the review, since those are the crops to deal with
    first; then the rest, most border first (the largest share of the frame
    the crop would remove), so the clearest cases come before the subtle ones.
    """
    if box is None:
        return (2, 0.0, member.path)
    return (0 if box.chrome else 1, -box.removed_fraction, member.path)


def build_crop_groups(
    members: list[FileRecord], level: str = "balanced"
) -> list[DuplicateGroup]:
    """Build one review collection of files with a crop suggestion at
    ``level`` (the scan's aggressiveness), ordered by
    :func:`crop_review_order`. The group remembers the level as the
    review's default; each file can be switched in the review."""
    from .borders import suggested_crop, validate_crop_level

    level = validate_crop_level(level)
    suggestions = [
        (member, box)
        for member in members
        if (box := suggested_crop(member, level)) is not None
    ]
    suggestions.sort(key=lambda item: crop_review_order(*item))
    groups = _build_independent_group(GroupKind.CROP, [member for member, _ in suggestions])
    for group in groups:
        group.match_details["crop_level"] = level
    return groups


def build_all_files_groups(
    members: list[FileRecord],
    roots: list[str | Path],
) -> list[DuplicateGroup]:
    """Build one browse collection per scanned root holding every scanned file.

    The All-Files review view is not detector-driven: each group is the whole
    media inventory of one scanned folder, ordered by path, so the user can
    sift a folder for deletion candidates that matched no review category.
    When roots overlap (scanning both ``a`` and ``a/b``), a file joins the
    most specific root's group.
    """
    normalized: list[tuple[str, Path]] = []
    for root in roots:
        text = str(root)
        key = str(Path(text))
        normalized.append((key, Path(key)))
    buckets: dict[str, list[FileRecord]] = {key: [] for key, _ in normalized}
    for member in members:
        path = Path(member.path)
        best: str | None = None
        best_depth = -1
        for key, root in normalized:
            if path.is_relative_to(root) and len(root.parts) > best_depth:
                best = key
                best_depth = len(root.parts)
        if best is not None:
            buckets[best].append(member)

    groups: list[DuplicateGroup] = []
    for key, _root in normalized:
        bucket = sorted(buckets[key], key=lambda member: member.path)
        if not bucket:
            continue
        groups.append(
            DuplicateGroup(
                id=make_group_id(GroupKind.ALL_FILES, bucket),
                kind=GroupKind.ALL_FILES,
                media_type=dominant_media_type(bucket),
                members=bucket,
                selected_for_removal=[],
                reviewed_paths=[],
                suggested_keep=None,
                root=key,
            )
        )
    return groups


def build_set_group(
    members: list[FileRecord], scores: dict[str, float]
) -> DuplicateGroup | None:
    """A Sets browse group: images of one subject or shoot, anchor first.

    ``scores`` holds each other member's cosine with the anchor. No member is
    a suggested keeper and nothing is selected; the Sets review trashes one
    click at a time.
    """
    members = list({member.path: member for member in members}.values())
    if len(members) < 2:
        return None
    anchor, rest = members[0], members[1:]
    rest.sort(key=lambda member: (-scores.get(member.path, 0.0), member.path))
    ordered = [anchor, *rest]
    return DuplicateGroup(
        id=make_group_id(GroupKind.SETS, ordered),
        kind=GroupKind.SETS,
        media_type=dominant_media_type(ordered),
        members=ordered,
        selected_for_removal=[],
        reviewed_paths=[],
        suggested_keep=None,
        match_basis="semantic",
        match_scores=dict(scores),
    )


def refresh_crop_groups(result: ScanResult, *, workers: int = 4) -> list[FileRecord]:
    """Bring a saved Crop review up to the current border detector.

    Suggestions saved by an older detector are measured again; files it no
    longer suggests cropping leave the review, cropped files stay (their
    crop is on disk and can still be undone), and the review is put back in
    :func:`crop_review_order`. Returns the records measured again, so the
    caller can remember the new verdicts.
    """
    from .borders import CropBox, _needs_border_measure, analyze_borders, suggested_crop

    measured: list[FileRecord] = []
    kept_groups = []
    for group in result.groups:
        if group.kind != GroupKind.CROP:
            kept_groups.append(group)
            continue
        level = group.match_details.get("crop_level") or "balanced"
        cropped = group.match_details.get("cropped") or {}
        stale = [
            member
            for member in group.members
            if member.path not in cropped and _needs_border_measure(member, level)
        ]
        if stale:
            analyze_borders(stale, level=level, workers=workers)
            measured.extend(stale)

        boxes = {
            member.path: (
                CropBox.decode(cropped[member.path]["box"])
                if member.path in cropped
                else suggested_crop(member, level)
            )
            for member in group.members
        }
        ordered = sorted(
            (
                (member, boxes[member.path])
                for member in group.members
                if boxes[member.path] is not None
            ),
            key=lambda item: crop_review_order(*item),
        )
        group.members = [member for member, _ in ordered]
        kept = {member.path for member in group.members}
        group.reviewed_paths = [path for path in group.reviewed_paths if path in kept]
        if group.members:
            kept_groups.append(group)
    result.groups = kept_groups
    return measured


def ensure_all_files_groups(result: ScanResult) -> None:
    """Append per-root All-Files browse groups when the result lacks them.

    Fresh engine results get these at scan completion in the web app; sessions
    saved before the view existed are upgraded here on load. Existing groups
    are left untouched so review progress (reviewed_paths) survives.
    """
    if result.scan_settings.get("new_only"):
        return
    if any(group.kind == GroupKind.ALL_FILES for group in result.groups):
        return
    built = build_all_files_groups(result.files, result.roots)
    if built:
        result.groups.extend(built)
        result.recompute_stats()


def _build_independent_group(
    kind: GroupKind,
    members: list[FileRecord],
) -> list[DuplicateGroup]:
    members = list({member.path: member for member in members}.values())
    if not members:
        return []
    return [
        DuplicateGroup(
            id=make_group_id(kind, members),
            kind=kind,
            media_type=dominant_media_type(members),
            members=members,
            selected_for_removal=[],
            reviewed_paths=[],
            suggested_keep=None,
        )
    ]


def apply_smart_select(group: DuplicateGroup, rule: SmartRule) -> None:
    """Mutate selected_for_removal. Always keeps at least one file."""
    if group.kind in (GroupKind.ALL_FILES, GroupKind.SETS, GroupKind.CROP):
        # All-Files and Sets browse groups have no selection semantics: those
        # reviews trash one click at a time, so rules must not touch them.
        # Crop suggestions are applied one click at a time and never delete.
        return
    members = group.members
    if not members:
        group.selected_for_removal = []
        return

    if rule == SmartRule.DESELECT_ALL:
        group.selected_for_removal = []
        return

    if group.policy == ReviewPolicy.INDEPENDENT_CANDIDATES:
        group.suggested_keep = None
        if rule == SmartRule.SELECT_CANDIDATES:
            reviewed = set(group.reviewed_paths)
            if group.kind == GroupKind.NO_HUMANS:
                group.selected_for_removal = [
                    m.path
                    for m in members
                    if m.path in reviewed
                    and not m.protected
                    and may_enter_no_person_review(m)
                ]
            else:
                # In Low-Res, Faces, and Random Review, reviewed & unselected
                # means an explicit Keep decision — never flip Kept files
                # into deletion selections.
                existing_selected = set(group.selected_for_removal)
                group.selected_for_removal = [
                    m.path
                    for m in members
                    if m.path in reviewed
                    and m.path in existing_selected
                    and not m.protected
                ]
        return

    if rule == SmartRule.SELECT_CANDIDATES:
        return

    if group.anchor_path is None:
        group.anchor_path = group.suggested_keep or pick_suggested_keep(members)

    if rule == SmartRule.AUTOMATIC:
        keep = pick_suggested_keep(members)
        group.suggested_keep = keep
        group.selected_for_removal = [
            m.path for m in members if m.path != keep and not m.protected
        ]
        return

    # Protected reference-library copies can never be removed, so prefer them
    # before applying user rules.
    pool = [m for m in members if m.protected] or members

    if rule == SmartRule.NEWEST:
        keep = max(pool, key=lambda m: (m.mtime_sort_stamp, m.path)).path
    elif rule == SmartRule.OLDEST:
        keep = min(pool, key=lambda m: (m.mtime_sort_stamp, m.path)).path
    elif rule == SmartRule.LARGEST:
        keep = max(pool, key=lambda m: (m.effective_pixels, m.size, m.path)).path
    elif rule == SmartRule.SMALLEST:
        keep = min(
            pool,
            key=lambda m: (m.effective_pixels == 0, m.effective_pixels, m.size, m.path),
        ).path
    elif rule == SmartRule.SHORTEST_PATH:
        keep = min(
            pool,
            key=lambda m: (len(Path(m.path).parts), len(m.path), m.path),
        ).path
    else:
        keep = pick_suggested_keep(members)

    group.suggested_keep = keep
    group.selected_for_removal = [
        m.path for m in members if m.path != keep and not m.protected
    ]


def apply_smart_select_all(groups: list[DuplicateGroup], rule: SmartRule) -> None:
    for g in groups:
        if g.match_basis in ("semantic", "video_version") and rule != SmartRule.DESELECT_ALL:
            # Bulk rules never select look-alikes; see build_one_group.
            continue
        apply_smart_select(g, rule)
