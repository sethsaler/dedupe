"""Orchestrates full scan: inventory → exact → similar → groups."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .cache import HashCache
from .exact import find_exact_groups
from .grouping import build_one_group
from .models import DuplicateGroup, GroupKind, ScanProgress, ScanResult
from .parallel import resolve_workers
from .scanner import inventory
from .similar_image import DEFAULT_THRESHOLD as IMG_THRESHOLD
from .similar_image import find_similar_image_groups
from .similar_video import DEFAULT_THRESHOLD as VID_THRESHOLD
from .similar_video import find_similar_video_groups

ProgressCb = Callable[[ScanProgress], None]
GroupCb = Callable[[DuplicateGroup], None]


def run_scan(
    roots: list[str | Path],
    *,
    exact: bool = True,
    similar: bool = True,
    include_images: bool = True,
    include_gifs: bool = True,
    include_videos: bool = True,
    include_hidden: bool = False,
    image_threshold: int = IMG_THRESHOLD,
    video_threshold: int = VID_THRESHOLD,
    use_cache: bool = True,
    cache_path: str | Path | None = None,
    workers: int | None = None,
    progress: ProgressCb | None = None,
    on_group: GroupCb | None = None,
) -> ScanResult:
    """Run a full scan.

    ``on_group`` is called as soon as each duplicate group is finalized (exact
    first, then similar-image, then similar-video) so UIs can stream results
    instead of waiting for the whole scan.
    """
    n_workers = resolve_workers(workers)
    prog = ScanProgress(phase="starting", message="Starting scan…")
    groups: list[DuplicateGroup] = []
    exact_path_sets: list[set[str]] = []

    def emit(phase: str, processed: int = 0, total: int = 0, message: str = "") -> None:
        prog.phase = phase
        prog.files_processed = processed
        prog.groups_found = len(groups)
        if total:
            prog.files_found = max(prog.files_found, total)
        if message:
            prog.message = message
        if progress:
            progress(prog)

    def publish(kind: GroupKind, member_lists: list[list]) -> int:
        """Build groups for one phase and stream them via on_group. Returns count added."""
        added = 0
        for members in member_lists:
            g = build_one_group(kind, members, exact_path_sets=exact_path_sets or None)
            if g is None:
                continue
            groups.append(g)
            if kind == GroupKind.EXACT:
                exact_path_sets.append({m.path for m in g.members})
            added += 1
            if on_group:
                on_group(g)
        # Keep most-reclaimable first for partial UI views
        groups.sort(key=lambda x: x.reclaimable_bytes, reverse=True)
        return added

    emit("inventory", message=f"Walking folders… ({n_workers} workers)")

    def inv_progress(phase: str, processed: int, total: int) -> None:
        prog.files_found = processed
        emit(phase, processed, total, f"Found {processed} media files…")

    records = inventory(
        roots,
        include_images=include_images,
        include_gifs=include_gifs,
        include_videos=include_videos,
        include_hidden=include_hidden,
        progress=inv_progress,
    )
    prog.files_found = len(records)
    prog.bytes_scanned = sum(r.size for r in records)
    emit("inventory", len(records), len(records), f"Found {len(records)} media files")

    cache: HashCache | None = None
    if use_cache:
        try:
            cache = HashCache(cache_path)
            hits = cache.hydrate(records)
            emit("cache", hits, len(records), f"Cache hits: {hits}/{len(records)}")
        except Exception as exc:
            emit("cache", 0, 0, f"Cache unavailable: {exc}")
            cache = None

    if exact and records:
        emit("exact", 0, len(records), "Finding exact duplicates…")

        def exact_progress(phase: str, processed: int, total: int) -> None:
            emit(phase, processed, total, f"Exact hash {processed}/{total}")

        exact_member_lists = find_exact_groups(
            records, progress=exact_progress, workers=n_workers
        )
        n_exact = publish(GroupKind.EXACT, exact_member_lists)
        emit(
            "exact",
            len(records),
            len(records),
            f"Found {n_exact} exact group{'s' if n_exact != 1 else ''}",
        )

    if similar:
        emit("similar-image", 0, 0, "Hashing images for similarity…")

        def img_progress(phase: str, processed: int, total: int) -> None:
            emit(phase, processed, total, f"Images {phase}: {processed}/{total}")

        img_groups = find_similar_image_groups(
            records,
            threshold=image_threshold,
            progress=img_progress,
            workers=n_workers,
        )
        n_img = publish(GroupKind.SIMILAR, img_groups)
        emit(
            "similar-image",
            0,
            0,
            f"Found {n_img} similar image group{'s' if n_img != 1 else ''}",
        )

        emit("similar-video", 0, 0, "Fingerprinting videos…")

        def vid_progress(phase: str, processed: int, total: int) -> None:
            emit(phase, processed, total, f"Videos {phase}: {processed}/{total}")

        vid_groups = find_similar_video_groups(
            records,
            threshold=video_threshold,
            progress=vid_progress,
            workers=n_workers,
        )
        n_vid = publish(GroupKind.SIMILAR, vid_groups)
        emit(
            "similar-video",
            0,
            0,
            f"Found {n_vid} similar video group{'s' if n_vid != 1 else ''}",
        )

    if cache is not None:
        try:
            cache.store_all(records)
            cache.close()
        except Exception:
            pass

    result = ScanResult(
        roots=[str(Path(r).expanduser().resolve()) for r in roots],
        files=records,
        groups=groups,
        errors=[r.error for r in records if r.error],
    )
    result.recompute_stats()

    prog.done = True
    prog.phase = "done"
    prog.groups_found = len(groups)
    prog.message = (
        f"Done — {result.exact_groups} exact, {result.similar_groups} similar groups "
        f"({len(records)} files)"
    )
    if progress:
        progress(prog)

    return result
