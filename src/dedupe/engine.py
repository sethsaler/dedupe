"""Orchestrates full scan: inventory → exact → similar → groups."""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path

from .borders import analyze_borders, is_croppable, validate_crop_level
from .cache import DistinctReviews, HashCache, share_exact_sibling_analysis
from .decider import DeciderJudge
from .exact import (
    ERROR_PARTIAL_HASH_FAILED,
    ERROR_SHA256_FAILED,
    classify_pixel_exact_group,
    find_exact_groups,
)
from .face_detection import FACE_MEDIA_TYPES, count_faces_in_files
from .grouping import (
    DEFAULT_RANDOM_REVIEW_COUNT,
    LOW_RESOLUTION_MAX_PIXELS,
    build_crop_groups,
    build_faces_groups,
    build_low_resolution_groups,
    build_no_human_groups,
    build_one_group,
    build_random_review_groups,
    build_set_group,
    pick_suggested_keep,
)
from .human_detection import (
    DEFAULT_BACKEND as DEFAULT_HUMAN_BACKEND,
)
from .human_detection import (
    DEFAULT_PHOTON_MODEL,
    find_no_human_files,
)
from .jobs import file_identity
from .keep_decisions import kept_paths
from .models import (
    DuplicateGroup,
    GroupKind,
    MediaType,
    ScanDiagnostics,
    ScanProgress,
    ScanResult,
    StageDiagnostics,
)
from .parallel import (
    DEFAULT_EXACT_WORKERS_CAP,
    DEFAULT_IMAGE_WORKERS_CAP,
    DEFAULT_VIDEO_WORKERS_CAP,
    map_parallel,
    resolve_workers,
    split_cpu_budget,
)
from .scanner import RAW_IMAGE_EXTS, detect_live_photo_pairs, inventory, is_in_photos_library
from .semantic import DEFAULT_DUPLICATE_THRESHOLD as SEMANTIC_DUPLICATE_THRESHOLD
from .semantic import DEFAULT_THRESHOLD as SEMANTIC_THRESHOLD
from .semantic import PREPARE_WORKERS as SEMANTIC_PREPARE_WORKERS
from .semantic import (
    SEMANTIC_MEDIA_TYPES,
    embed_records,
    group_semantic_vectors,
    semantic_available,
)
from .semantic import VERIFY_WORKERS as SEMANTIC_VERIFY_WORKERS
from .similar_image import DEFAULT_THRESHOLD as IMG_THRESHOLD
from .similar_image import (
    ERROR_IMAGE_HASH_FAILED,
    ensure_heif_registered,
    find_similar_image_groups,
    probe_image_dimensions,
)
from .similar_video import DEFAULT_THRESHOLD as VID_THRESHOLD
from .similar_video import (
    ERROR_VIDEO_FINGERPRINT_FAILED,
    _verify_video_offset,
    compare_video_audio,
    ffmpeg_available,
    find_similar_video_groups,
    find_video_version_matches,
    probe_video,
)

ProgressCb = Callable[[ScanProgress], None]
GroupCb = Callable[[DuplicateGroup], None]
GroupsCb = Callable[[list[DuplicateGroup]], None]
StreamProgressCb = Callable[[ScanProgress], None]

# Minimum seconds between mid-stage progress callback invocations.
PROGRESS_CALLBACK_MIN_INTERVAL = 0.1


def _accepts_kwarg(fn: Callable, name: str) -> bool:
    """Return True when ``fn`` accepts ``name`` as a keyword argument."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return name in sig.parameters or any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in sig.parameters.values()
    )


def _concurrent_cpu_stage_count(
    *,
    exact: bool,
    borders: bool,
    image: bool,
    video: bool,
    semantic: bool,
    human: bool,
    faces: bool,
) -> int:
    """Peak number of CPU-bound stages that can run at the same time.

    ``borders`` runs strictly before ``image`` and ``video`` (both wait on
    ``borders_done``), so they never compete for CPU cores simultaneously.
    """
    border_or_media = max(int(borders), int(image) + int(video))
    return int(exact) + border_or_media + int(semantic) + int(human) + int(faces)


def _configure_media_runtime() -> None:
    """Register HEIF decoding once and cap OpenCV internal thread oversubscription."""
    ensure_heif_registered()
    try:
        import cv2

        cv2.setNumThreads(1)
    except Exception:
        pass


class _ArrivalDistinctFilter:
    """Skip verifying old-vs-old pairs during incremental discovery while keeping clustering intact."""

    def __init__(self, base: DistinctReviews, arrival_paths: set[str] | None) -> None:
        self.base = base
        self.arrivals = arrival_paths
        self.filter_active = arrival_paths is not None

    def __bool__(self) -> bool:
        return bool(self.base) or self.filter_active

    def is_distinct(self, a, b) -> bool:
        if self.base.is_distinct(a, b):
            return True
        if self.filter_active and self.arrivals is not None:
            return a.path not in self.arrivals and b.path not in self.arrivals
        return False


def _in_same_exact_group(members: list, exact_path_to_group: dict[str, int]) -> bool:
    """Return True if all members already share the same exact-duplicate group ID."""
    if len(members) < 2 or not exact_path_to_group:
        return False
    first_id = exact_path_to_group.get(members[0].path)
    if first_id is None:
        return False
    return all(exact_path_to_group.get(m.path) == first_id for m in members[1:])


def _aligned_audio_ranges(
    keeper,
    member,
    cancelled: Callable[[], bool] | None,
    *,
    frame_cache=None,
) -> dict[str, list[float]]:
    """Compute aligned ``{path: [start, end]}`` windows before audio comparison."""
    da = float(keeper.duration or 0.0)
    db = float(member.duration or 0.0)
    fallback_end = min(da, db)
    fallback = {keeper.path: [0.0, fallback_end], member.path: [0.0, fallback_end]}
    if min(da, db) <= 0.0:
        return fallback
    offsets = [0.0]
    if abs(da - db) > 0.5:
        diff = da - db
        offsets.extend((diff, diff / 2.0))
    verify_kwargs: dict[str, object] = {"min_overlap": min(5.0, min(da, db))}
    if frame_cache is not None and _accepts_kwarg(_verify_video_offset, "frame_cache"):
        verify_kwargs["frame_cache"] = frame_cache
    for offset in offsets:
        try:
            evidence = _verify_video_offset(
                keeper,
                member,
                offset,
                cancelled,
                **verify_kwargs,
            )
        except InterruptedError:
            raise
        except Exception:
            evidence = None
        if evidence and isinstance(evidence.get("ranges"), dict):
            return evidence["ranges"]
    return fallback


def _annotate_live_photo_groups(groups: list[DuplicateGroup], live_pairs: list[tuple]) -> list[str]:
    """Attach Live Photo companion hints to groups and return diagnostic warnings."""
    if not live_pairs:
        return []
    companion_hint: dict[str, str] = {}
    warnings: list[str] = []
    for still, mov, _cid in live_pairs:
        still_name = Path(still.path).name
        mov_name = Path(mov.path).name
        companion_hint[still.path] = f"Live Photo still companion (paired with {mov_name})"
        companion_hint[mov.path] = f"Live Photo motion clip (paired with {still_name})"
        warnings.append(
            f"Live Photo pair detected ({still_name} + {mov_name}): keep both companions together"
        )
    for group in groups:
        for member in group.members:
            hint = companion_hint.get(member.path)
            if hint and member.path not in group.match_explanations:
                group.match_explanations[member.path] = hint
    return warnings


def _low_resolution_bounds(
    *,
    default_max_pixels: int,
    images: bool,
    gifs: bool,
    videos: bool,
    image_max_pixels: int | None,
    gif_max_pixels: int | None,
    video_max_pixels: int | None,
) -> dict[MediaType, int]:
    return {
        media_type: max(1, int(max_pixels or default_max_pixels))
        for media_type, enabled, max_pixels in (
            (MediaType.IMAGE, images, image_max_pixels),
            (MediaType.GIF, gifs, gif_max_pixels),
            (MediaType.VIDEO, videos, video_max_pixels),
        )
        if enabled
    }


def _populate_missing_dimensions(
    records: list,
    *,
    media_types: set[MediaType],
    workers: int,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[str]:
    """Populate dimensions needed by low-resolution review without full hashing."""
    missing = [
        record
        for record in records
        if record.media_type in media_types
        and record.extension not in RAW_IMAGE_EXTS
        and not (record.width and record.height)
    ]
    if not missing:
        return []

    probe_accepts_cancelled = _accepts_kwarg(probe_video, "cancelled")

    def probe(record):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        try:
            if record.media_type == MediaType.VIDEO:
                if cancelled is not None and probe_accepts_cancelled:
                    duration, width, height = probe_video(record.path, cancelled=cancelled)
                else:
                    duration, width, height = probe_video(record.path)
                if duration is not None:
                    record.duration = duration
            else:
                width, height = probe_image_dimensions(record.path)
            if not (width and height):
                return f"resolution probe failed: {record.path}"
            record.width = width
            record.height = height
            return None
        except InterruptedError:
            raise
        except Exception as exc:
            return f"resolution probe failed for {record.path}: {exc}"

    # map_parallel keeps only ~2x workers futures in flight; a raw
    # ThreadPoolExecutor.map would allocate one future per missing record.
    results = map_parallel(
        probe,
        missing,
        workers=max(1, min(workers, len(missing))),
        progress=progress,
        cancelled=cancelled,
    )
    return [error for error in results if error]


def run_scan(
    roots: list[str | Path],
    *,
    preferred_folders: list[str] | None = None,
    protected_folders: list[str] | None = None,
    video_versions: bool = False,
    exact: bool = True,
    similar: bool = True,
    semantic: bool = False,
    semantic_threshold: float = SEMANTIC_THRESHOLD,
    semantic_duplicate_threshold: float = SEMANTIC_DUPLICATE_THRESHOLD,
    semantic_full_speed: bool = False,
    decider: bool = False,
    find_no_humans: bool = False,
    count_faces: bool = False,
    find_black_borders: bool = False,
    crop_aggressiveness: str = "balanced",
    find_low_resolution: bool = True,
    low_resolution_images: bool = True,
    low_resolution_gifs: bool = True,
    low_resolution_videos: bool = True,
    low_resolution_max_pixels: int = LOW_RESOLUTION_MAX_PIXELS,
    low_resolution_image_max_pixels: int | None = None,
    low_resolution_gif_max_pixels: int | None = None,
    low_resolution_video_max_pixels: int | None = None,
    random_review_count: int = DEFAULT_RANDOM_REVIEW_COUNT,
    human_backend: str = DEFAULT_HUMAN_BACKEND,
    photon_model: str = DEFAULT_PHOTON_MODEL,
    include_images: bool = True,
    include_gifs: bool = True,
    include_videos: bool = True,
    include_hidden: bool = False,
    image_threshold: int = IMG_THRESHOLD,
    video_threshold: int = VID_THRESHOLD,
    use_cache: bool = True,
    cache_path: str | Path | None = None,
    workers: int | None = None,
    exclusions: list[str] | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: ProgressCb | None = None,
    on_group: GroupCb | None = None,
    on_groups: GroupsCb | None = None,
    emit_done: bool = True,
    baseline: dict[str, list] | None = None,
    new_only: bool | None = None,
    _build_review_groups: bool = True,
    _workers_chosen: bool | None = None,
) -> ScanResult:
    """Run a full or incremental arrival-focused scan.

    Every enabled check — exact hashing, image/GIF similarity, video
    similarity, dimension probing, person detection, and face counting — runs
    concurrently on one stage pool. ``on_group`` is called as soon as each
    duplicate group is finalized so UIs can stream results instead of waiting
    for the whole scan. Result-affecting order is kept with events: exact
    groups are always published before similar groups because similar grouping
    consults the exact-group membership, crop borders finish before image/video
    similarity hashing when enabled, and Non-Human groups wait for face counts
    when faces are also being counted (a counted face vetoes membership).
    """
    _configure_media_runtime()
    crop_aggressiveness = validate_crop_level(crop_aggressiveness)
    n_workers = resolve_workers(workers)
    # Whether the user chose the count. A per-folder stream receives a share
    # of the budget as a number, so its caller says where the number came from.
    workers_chosen = (
        bool(workers and workers > 0) if _workers_chosen is None else _workers_chosen
    )
    low_resolution_bounds = _low_resolution_bounds(
        default_max_pixels=low_resolution_max_pixels,
        images=low_resolution_images,
        gifs=low_resolution_gifs,
        videos=low_resolution_videos,
        image_max_pixels=low_resolution_image_max_pixels,
        gif_max_pixels=low_resolution_gif_max_pixels,
        video_max_pixels=low_resolution_video_max_pixels,
    )
    cancel_internal = threading.Event()

    def is_cancelled() -> bool:
        return cancel_internal.is_set() or bool(cancelled and cancelled())

    def check_cancelled() -> None:
        if is_cancelled():
            raise InterruptedError("scan cancelled")

    prog = ScanProgress(phase="starting", message="Starting scan…")
    started = time.monotonic()
    phase_started = started
    previous_phase = prog.phase
    groups: list[DuplicateGroup] = []
    exact_path_sets: list[set[str]] = []
    exact_path_to_group: dict[str, int] = {}
    stage_durations: dict[str, float] = {}
    stage_errors: dict[str, list[str]] = {
        "exact": [],
        "similar_image": [],
        "similar_video": [],
        "semantic": [],
        "decider": [],
        "human_detection": [],
        "face_detection": [],
        "black_borders": [],
        "low_resolution": [],
    }
    cache_hits = 0
    first_duplicate_seconds = None

    # Per-file progress arrives from every concurrent stage; invoking the
    # caller's callback for each one means hundreds of thousands of lock
    # acquisitions on a big library. Throttle mid-stage callbacks, guard shared
    # ScanProgress state, and always deliver immutable replace(prog) snapshots.
    prog_lock = threading.Lock()
    last_callback = {"t": 0.0}

    def emit(
        phase: str,
        processed: int = 0,
        total: int = 0,
        message: str = "",
        *,
        files_found: int | None = None,
        bytes_scanned: int | None = None,
        done: bool = False,
        eta_zero: bool = False,
    ) -> None:
        nonlocal phase_started, previous_phase
        with prog_lock:
            now = time.monotonic()
            phase_changed = phase != previous_phase
            if phase_changed:
                phase_started = now
                previous_phase = phase
            prog.phase = phase
            prog.files_processed = processed
            prog.groups_found = len(groups)
            if files_found is not None:
                prog.files_found = files_found
            elif total:
                prog.files_found = max(prog.files_found, total)
            if bytes_scanned is not None:
                prog.bytes_scanned = bytes_scanned
            if message:
                prog.message = message
            if done:
                prog.done = True
            prog.elapsed_seconds = max(0.0, now - started)
            if eta_zero:
                prog.eta_seconds = 0.0
            else:
                phase_elapsed = max(0.0, now - phase_started)
                if total > 0 and 0 < processed < total and phase_elapsed > 0:
                    rate = processed / phase_elapsed
                    prog.eta_seconds = (total - processed) / rate if rate > 0 else None
                else:
                    prog.eta_seconds = None
            if progress:
                milestone = done or (bool(message) and (not total or processed >= total))
                stage_finished = bool(total) and processed >= total
                if (
                    phase_changed
                    or milestone
                    or stage_finished
                    or now - last_callback["t"] >= PROGRESS_CALLBACK_MIN_INTERVAL
                ):
                    last_callback["t"] = now
                    progress(replace(prog))

    publish_lock = threading.Lock()
    callback_lock = threading.Lock()
    arrival_paths: set[str] | None = None

    def _stream_groups(built: list[DuplicateGroup]) -> None:
        if not built:
            return
        if on_groups is not None:
            with callback_lock:
                on_groups(list(built))
        elif on_group is not None:
            with callback_lock:
                for group in built:
                    on_group(group)

    def publish(kind: GroupKind, member_lists: list[list]) -> int:
        """Build groups for one phase and stream them via on_group. Returns count added."""
        nonlocal first_duplicate_seconds
        built: list[DuplicateGroup] = []
        with publish_lock:
            exact_lookup = dict(exact_path_to_group) if kind == GroupKind.SIMILAR else {}
        for members in member_lists:
            if all(member.protected for member in members):
                continue
            if arrival_paths is not None and not any(m.path in arrival_paths for m in members):
                continue
            if kind == GroupKind.SIMILAR and _in_same_exact_group(members, exact_lookup):
                continue
            g = build_one_group(kind, members)
            if g is None:
                continue
            if kind == GroupKind.SIMILAR:
                basis, explanations = classify_pixel_exact_group(
                    g.members, keeper_path=g.suggested_keep, cancelled=is_cancelled
                )
                if basis:
                    g.match_basis = basis
                if explanations:
                    g.match_explanations.update(explanations)
            built.append(g)

        if not built:
            return 0
        with publish_lock:
            for g in built:
                groups.append(g)
                if first_duplicate_seconds is None:
                    first_duplicate_seconds = max(0.0, time.monotonic() - started)
                if kind == GroupKind.EXACT:
                    group_idx = len(exact_path_sets)
                    path_set = {m.path for m in g.members}
                    exact_path_sets.append(path_set)
                    for path in path_set:
                        exact_path_to_group[path] = group_idx
        _stream_groups(built)
        return len(built)

    emit("inventory", message=f"Walking folders… ({n_workers} workers)")

    def inv_progress(phase: str, processed: int, total: int) -> None:
        emit(phase, processed, total, f"Found {processed} media files…", files_found=processed)

    protected_roots: list[Path] = []
    protected_identities: set[tuple[int, int]] = set()
    walk_roots: list[Path] = []
    seen_walk_paths: set[Path] = set()
    failed_root_paths: set[Path] = set()
    root_errors: list[str] = []

    # Walk protected folders BEFORE unprotected roots so shared subtrees or
    # overlapping folder inodes resolve under the protected library first.
    for raw_folder in dict.fromkeys(protected_folders or []):
        resolved = Path(raw_folder).expanduser().resolve(strict=False)
        if not resolved.exists():
            failed_root_paths.add(resolved)
            root_errors.append(f"protected folder does not exist: {resolved}")
            continue
        if is_in_photos_library(resolved):
            failed_root_paths.add(resolved)
            root_errors.append(
                "Photos libraries cannot be scanned directly; export media from "
                f"Photos.app to a normal folder first: {resolved}"
            )
            continue
        try:
            st = resolved.stat()
        except OSError as exc:
            failed_root_paths.add(resolved)
            root_errors.append(f"protected folder unreadable: {resolved}: {exc}")
            continue
        protected_roots.append(resolved)
        protected_identities.add((st.st_dev, st.st_ino))
        if resolved not in seen_walk_paths:
            seen_walk_paths.add(resolved)
            walk_roots.append(resolved)

    for root in dict.fromkeys(roots):
        resolved = Path(root).expanduser().resolve(strict=False)
        if resolved in seen_walk_paths or resolved in failed_root_paths:
            continue
        if not resolved.exists():
            failed_root_paths.add(resolved)
            root_errors.append(f"scan root does not exist: {resolved}")
            continue
        if is_in_photos_library(resolved):
            failed_root_paths.add(resolved)
            root_errors.append(
                "Photos libraries cannot be scanned directly; export media from "
                f"Photos.app to a normal folder first: {resolved}"
            )
            continue
        seen_walk_paths.add(resolved)
        walk_roots.append(resolved)

    attempted_roots_count = len(seen_walk_paths | failed_root_paths)
    resolved_roots = walk_roots

    check_cancelled()
    inventory_started = time.monotonic()
    records = inventory(
        resolved_roots,
        include_images=include_images,
        include_gifs=include_gifs,
        include_videos=include_videos,
        include_hidden=include_hidden,
        exclusions=exclusions,
        progress=inv_progress,
        cancelled=is_cancelled,
    )
    stage_durations["inventory"] = time.monotonic() - inventory_started
    emit(
        "inventory",
        len(records),
        len(records),
        f"Found {len(records)} media files",
        files_found=len(records),
        bytes_scanned=sum(r.size for r in records),
    )

    preferred_roots = [Path(p).expanduser().resolve(strict=False) for p in (preferred_folders or [])]
    protected_ancestors: dict[Path, bool] = {}

    def is_protected(path: Path) -> bool:
        if not protected_roots:
            return False
        if path not in protected_ancestors:
            try:
                stat = path.stat()
            except OSError:
                protected_ancestors[path] = False
                return False
            protected_ancestors[path] = (
                (stat.st_dev, stat.st_ino) in protected_identities
                or (path.parent != path and is_protected(path.parent))
            )
        return protected_ancestors[path]

    for record in records:
        record.protected = (
            (record.device, record.inode) in protected_identities
            or is_protected(Path(record.path).parent)
        )
        record.keeper_preference = next(
            (
                len(preferred_roots) - index
                for index, folder in enumerate(preferred_roots)
                if Path(record.path).is_relative_to(folder)
            ),
            0,
        )

    # Propagate protection across hardlinks sharing the same (device, inode)
    # even when the unprotected link lives outside protected folder trees.
    protected_file_inodes = {
        (record.device, record.inode)
        for record in records
        if record.protected and record.inode
    }
    if protected_file_inodes:
        for record in records:
            if not record.protected and (record.device, record.inode) in protected_file_inodes:
                record.protected = True

    incremental = baseline is not None and new_only is not False
    if incremental and baseline is not None:
        arrival_paths = {
            record.path
            for record in records
            if baseline.get(record.path) != file_identity(record)
        }
    incremental_empty = arrival_paths is not None and not arrival_paths

    cache: HashCache | None = None
    if use_cache and not incremental_empty:
        try:
            cache = HashCache(cache_path)
            cache_hits = cache.hydrate(records)
            emit(
                "cache",
                cache_hits,
                len(records),
                f"Cache hits: {cache_hits}/{len(records)}",
            )
        except Exception as exc:
            emit("cache", 0, 0, f"Cache unavailable: {exc}")
            cache = None

    if not incremental_empty:
        share_exact_sibling_analysis(records)

    independent_records = (
        [r for r in records if r.path in arrival_paths and not r.protected]
        if arrival_paths is not None
        else records
    )

    run_exact = exact and bool(records) and not incremental_empty
    run_similar_images = similar and bool(records) and not incremental_empty
    run_similar_videos = (similar or video_versions) and bool(records) and not incremental_empty
    run_similar = run_similar_images or run_similar_videos
    run_semantic = semantic and bool(records) and not incremental_empty
    run_dimensions = find_low_resolution and bool(independent_records) and not incremental_empty
    run_human = find_no_humans and bool(independent_records) and not incremental_empty
    run_faces = (
        count_faces
        and (
            bool([r for r in independent_records if r.media_type in FACE_MEDIA_TYPES])
            if arrival_paths is not None
            else bool(records)
        )
        and not incremental_empty
    )
    border_records = (
        [
            record
            for record in records
            if is_croppable(record)
            and (
                arrival_paths is None
                or (
                    record.path in arrival_paths
                    and (run_similar_images or run_similar_videos or not record.protected)
                )
            )
        ]
        if find_black_borders and not incremental_empty
        else []
    )
    run_borders = bool(border_records)
    _build_review_active = _build_review_groups and not incremental_empty and (
        arrival_paths is None or bool(independent_records)
    )
    run_any_stage = (
        run_exact
        or run_similar
        or run_semantic
        or run_dimensions
        or run_human
        or run_faces
        or run_borders
        or _build_review_active
    )

    if run_any_stage:
        check_cancelled()
        distinct = (
            cache.distinct_reviews(records)
            if ((run_similar or run_semantic) and cache is not None)
            else DistinctReviews.empty()
        )
        # Release the SQLite connection while multi-minute scan stages run so
        # WAL checkpointing and concurrent streams are never pinned open.
        if cache is not None:
            cache.suspend()
        image_count = len(
            [r for r in records if r.media_type in (MediaType.IMAGE, MediaType.GIF)]
        )
        video_count = len([r for r in records if r.media_type == MediaType.VIDEO])
        human_records = independent_records
        face_candidates = [
            record for record in independent_records if record.media_type in FACE_MEDIA_TYPES
        ]
        dims_candidates_count = (
            len(
                [
                    record
                    for record in independent_records
                    if record.media_type in set(low_resolution_bounds)
                    and record.extension not in RAW_IMAGE_EXTS
                ]
            )
            if run_dimensions
            else 0
        )
        normalized_human_backend = human_backend.strip().lower()

        active_exact = 1 if run_exact else 0
        active_image = 1 if run_similar_images and image_count else 0
        active_video = 1 if run_similar_videos and video_count else 0
        semantic_count = len(
            [r for r in records if r.media_type in SEMANTIC_MEDIA_TYPES]
        )
        active_semantic = 1 if run_semantic and semantic_count else 0
        active_human = 1 if run_human and normalized_human_backend == "opencv" else 0
        active_faces = 1 if run_faces else 0
        active_borders = 1 if run_borders else 0
        cpu_stage_count = _concurrent_cpu_stage_count(
            exact=bool(active_exact),
            borders=bool(active_borders),
            image=bool(active_image),
            video=bool(active_video),
            semantic=bool(active_semantic),
            human=bool(active_human),
            faces=bool(active_faces),
        )
        cpu_workers = split_cpu_budget(n_workers, cpu_stage_count)
        exact_workers = (
            cpu_workers if workers_chosen else min(cpu_workers, DEFAULT_EXACT_WORKERS_CAP)
        )
        # The image cap keeps an automatic scan light on memory; a count the
        # user chose is their budget and is passed on as given.
        image_workers = (
            cpu_workers if workers_chosen else min(cpu_workers, DEFAULT_IMAGE_WORKERS_CAP)
        )
        video_workers = (
            cpu_workers if workers_chosen else min(cpu_workers, DEFAULT_VIDEO_WORKERS_CAP)
        )
        human_workers = (
            cpu_workers if normalized_human_backend == "opencv" else n_workers
        )
        semantic_prepare_workers = min(cpu_workers, SEMANTIC_PREPARE_WORKERS)
        semantic_verify_workers = min(cpu_workers, SEMANTIC_VERIFY_WORKERS)

        stage_lock = threading.Lock()
        stage_text: dict[str, str] = {}
        stage_counts: dict[str, tuple[int, int]] = {}
        exact_done = threading.Event()
        borders_done = threading.Event()
        image_done = threading.Event()
        video_done = threading.Event()
        semantic_done = threading.Event()
        dims_done = threading.Event()
        human_done = threading.Event()
        face_done = threading.Event()

        def _wait_event(event: threading.Event) -> None:
            while not event.wait(timeout=0.1):
                check_cancelled()

        def _late_video_versions_workers() -> int:
            """Reclaim CPU budget from already-finished stages for video excerpts."""
            other_running = sum(
                1
                for active, done_ev in (
                    (active_image, image_done),
                    (active_semantic, semantic_done),
                    (active_human, human_done),
                    (active_faces, face_done),
                )
                if active and not done_ev.is_set()
            )
            if other_running == 0:
                return n_workers
            return split_cpu_budget(n_workers, 1 + other_running)

        def _emit_stages_locked() -> None:
            processed = sum(done for done, _total in stage_counts.values())
            total = sum(total for _done, total in stage_counts.values())
            message = " · ".join(
                text
                for text in (
                    stage_text.get("exact"),
                    stage_text.get("borders"),
                    stage_text.get("image"),
                    stage_text.get("video"),
                    stage_text.get("video_versions"),
                    stage_text.get("semantic"),
                    stage_text.get("dims"),
                    stage_text.get("human"),
                    stage_text.get("face"),
                )
                if text
            )
            emit("processing", processed, total, message)

        def _stage_progress(stage: str, text: str, processed: int, total: int) -> None:
            with stage_lock:
                stage_text[stage] = text
                if total:
                    previous = stage_counts.get(stage)
                    if previous:
                        stage_total = previous[1]
                        if total != stage_total:
                            # A stage counts only the files it has to work
                            # on. The rest were served from the cache and are
                            # done, so the stage keeps the total it began with.
                            remaining = max(0, total - processed)
                            processed = max(0, stage_total - remaining)
                            total = stage_total
                        # Reports from worker threads can arrive out of order.
                        processed = max(processed, previous[0])
                    stage_counts[stage] = (processed, total)
                _emit_stages_locked()

        def _stage_finished(stage: str, text: str) -> None:
            """A stage's closing note. Whatever it last counted, it is complete."""
            with stage_lock:
                stage_text[stage] = text
                if stage in stage_counts:
                    total = stage_counts[stage][1]
                    stage_counts[stage] = (total, total)
                _emit_stages_locked()

        def _two_pass_progress(
            stage: str,
            text: str,
            stage_total: int,
            second_pass: bool,
            processed: int,
            total: int,
        ) -> None:
            """Progress of a stage that passes over its files twice."""
            if total <= 0 or stage_total <= 0:
                _stage_progress(stage, text, 0, 0)
                return
            half = stage_total / 2
            done = int(half * min(1.0, processed / total) + (half if second_pass else 0))
            _stage_progress(stage, text, min(stage_total, done), stage_total)

        def publish_prebuilt(new_groups: list[DuplicateGroup]) -> None:
            """Append already-built review groups and stream them (thread-safe)."""
            nonlocal first_duplicate_seconds
            if not new_groups:
                return
            with publish_lock:
                for group in new_groups:
                    groups.append(group)
                    if group.kind == GroupKind.SIMILAR and first_duplicate_seconds is None:
                        first_duplicate_seconds = max(0.0, time.monotonic() - started)
            _stream_groups(new_groups)

        run_video_similar = similar and run_similar_videos and bool(video_count)
        run_video_versions = video_versions and run_similar_videos and bool(video_count)

        with stage_lock:
            if run_exact:
                stage_text["exact"] = "Finding exact duplicates…"
                stage_counts["exact"] = (0, len(records))
            if run_borders:
                stage_text["borders"] = "Looking for crops…"
                stage_counts["borders"] = (0, len(border_records))
            if run_similar_images and image_count:
                stage_text["image"] = f"Hashing {image_count} images…"
                stage_counts["image"] = (0, image_count)
            if run_video_similar:
                stage_text["video"] = f"Fingerprinting {video_count} videos…"
                stage_counts["video"] = (0, video_count)
            if run_video_versions:
                stage_text["video_versions"] = f"Finding video excerpts ({video_count} videos)…"
                stage_counts["video_versions"] = (0, video_count)
            if run_semantic and semantic_count:
                stage_text["semantic"] = (
                    f"Embedding {semantic_count} images (EmbeddingGemma)…"
                )
                stage_counts["semantic"] = (0, semantic_count)
            if run_dimensions:
                stage_text["dims"] = "Reading media dimensions…"
                if dims_candidates_count:
                    stage_counts["dims"] = (0, dims_candidates_count)
            if run_human:
                stage_text["human"] = (
                    f"Looking for media without people ({human_backend})…"
                )
                stage_counts["human"] = (0, len(human_records))
            if run_faces:
                stage_text["face"] = "Counting faces (OpenCV)…"
                stage_counts["face"] = (0, len(face_candidates))
            _emit_stages_locked()

        def exact_progress(phase: str, processed: int, total: int) -> None:
            _two_pass_progress(
                "exact",
                f"Exact hash {processed}/{total}",
                len(records),
                "full" in phase,
                processed,
                total,
            )

        def img_progress(phase: str, processed: int, total: int) -> None:
            if "hash" in phase:
                label = "hashing"
            elif "cluster" in phase:
                label = "clustering"
            else:
                label = phase.replace("-", " ")
            _two_pass_progress(
                "image",
                f"Images {label}: {processed}/{total}",
                image_count,
                "hash" not in phase,
                processed,
                total,
            )

        def vid_progress(
            phase: str, processed: int, total: int, message: str = ""
        ) -> None:
            if "hash" in phase:
                label = "hashing"
            elif "cluster" in phase:
                label = "clustering"
            else:
                label = phase.replace("-", " ")
            text = message or f"Videos {label}: {processed}/{total}"
            _two_pass_progress(
                "video", text, video_count, "hash" not in phase, processed, total
            )

        def vid_versions_progress(
            phase: str, processed: int, total: int, message: str = ""
        ) -> None:
            """Map video-excerpt sampling, verification, and audio onto their own stage budget."""
            lowered = phase.lower()
            if "audio" in lowered:
                lo, hi = 0.90, 0.99
                default_label = "Comparing video audio"
            elif "index" in lowered:
                lo, hi = 0.55, 0.65
                default_label = "Indexing video excerpts"
            elif any(
                token in lowered
                for token in ("verify", "match", "cluster", "candidate")
            ):
                lo, hi = 0.65, 0.90
                default_label = "Verifying video excerpts"
            else:
                lo, hi = 0.0, 0.55
                default_label = "Finding video excerpts"
            if message:
                text = (
                    message
                    if (total <= 0 or f"{processed}/{total}" in message)
                    else f"{message} ({processed}/{total})"
                )
            elif total > 0:
                text = f"{default_label}: {processed}/{total}"
            else:
                text = default_label
            if total <= 0 or video_count <= 0:
                _stage_progress("video_versions", text, 0, 0)
                return
            frac = min(1.0, max(0.0, processed / total))
            scaled = int(video_count * (lo + (hi - lo) * frac))
            _stage_progress(
                "video_versions",
                text,
                min(max(0, video_count - 1), scaled),
                video_count,
            )

        def _exact_stage() -> float:
            started_at = time.monotonic()
            exact_errs: list[str] = []
            try:
                exact_member_lists = find_exact_groups(
                    records,
                    progress=exact_progress,
                    workers=exact_workers,
                    cancelled=is_cancelled,
                    arrival_paths=arrival_paths,
                    errors=exact_errs,
                )
                count = publish(GroupKind.EXACT, exact_member_lists)
                if not exact_errs:
                    exact_errs.extend(
                        r.error
                        for r in records
                        if r.error
                        and r.error.startswith(
                            (ERROR_PARTIAL_HASH_FAILED, ERROR_SHA256_FAILED)
                        )
                    )
                stage_errors["exact"] = list(dict.fromkeys(exact_errs))
                _stage_finished(
                    "exact",
                    f"Found {count} exact group{'s' if count != 1 else ''}",
                )
                return time.monotonic() - started_at
            finally:
                # Always unblock similar publishing, even on cancel/error.
                exact_done.set()

        def _border_stage() -> float:
            started_at = time.monotonic()
            try:
                def border_progress(processed: int, total: int) -> None:
                    _stage_progress(
                        "borders",
                        f"Crops {processed}/{total}",
                        processed,
                        total,
                    )

                stage_errors["black_borders"] = analyze_borders(
                    border_records,
                    level=crop_aggressiveness,
                    workers=cpu_workers,
                    progress=border_progress,
                    cancelled=is_cancelled,
                )
                crop_pool = (
                    [r for r in border_records if not r.protected]
                    if arrival_paths is not None
                    else border_records
                )
                crop_groups = build_crop_groups(crop_pool, level=crop_aggressiveness)
                crop_count = sum(len(group.members) for group in crop_groups)
                _stage_finished(
                    "borders",
                    f"Found {crop_count} file{'' if crop_count == 1 else 's'} to crop",
                )
                duration = time.monotonic() - started_at
                publish_prebuilt(crop_groups)
                return duration
            finally:
                # Always unblock image/video hashing waiting for border_crop ROIs.
                borders_done.set()

        def _image_stage() -> float:
            started_at = time.monotonic()
            try:
                if run_borders:
                    _wait_event(borders_done)
                    check_cancelled()
                    started_at = time.monotonic()
                img_distinct = _ArrivalDistinctFilter(distinct, arrival_paths)

                def _img_progress(phase: str, processed: int, total: int) -> None:
                    if phase == "image-cluster" and total > 0 and processed >= total:
                        img_distinct.filter_active = False
                    img_progress(phase, processed, total)

                img_groups = find_similar_image_groups(
                    records,
                    threshold=image_threshold,
                    distinct=img_distinct if arrival_paths is not None else distinct,
                    progress=_img_progress if arrival_paths is not None else img_progress,
                    workers=image_workers,
                    cancelled=is_cancelled,
                )
                _wait_event(exact_done)
                check_cancelled()
                count = publish(GroupKind.SIMILAR, img_groups)
                stage_errors["similar_image"] = list(
                    dict.fromkeys(
                        record.error
                        for record in records
                        if record.error
                        and record.media_type in (MediaType.IMAGE, MediaType.GIF)
                        and record.error.startswith(ERROR_IMAGE_HASH_FAILED)
                    )
                )
                _stage_finished(
                    "image",
                    f"Found {count} similar image group{'s' if count != 1 else ''}",
                )
                return time.monotonic() - started_at
            finally:
                # Always unblock dimension probing and semantic grouping, even on cancel/error.
                image_done.set()

        def _video_stage() -> float:
            started_at = time.monotonic()
            version_errors: list[str] = []
            try:
                if run_borders:
                    _wait_event(borders_done)
                    check_cancelled()
                    started_at = time.monotonic()
                if similar:
                    vid_distinct = _ArrivalDistinctFilter(distinct, arrival_paths)

                    def _vid_progress(
                        phase: str, processed: int, total: int, message: str = ""
                    ) -> None:
                        if (
                            phase == "video-cluster"
                            and message == ""
                            and total > 0
                            and processed >= total
                        ):
                            vid_distinct.filter_active = False
                        vid_progress(phase, processed, total, message)

                    vid_groups = find_similar_video_groups(
                        records,
                        threshold=video_threshold,
                        distinct=vid_distinct if arrival_paths is not None else distinct,
                        progress=_vid_progress if arrival_paths is not None else vid_progress,
                        workers=video_workers,
                        cancelled=is_cancelled,
                        exact_done=exact_done,
                    )
                    if video_versions:
                        _stage_finished(
                            "video",
                            f"Fingerprinted {video_count} "
                            f"video{'s' if video_count != 1 else ''}",
                        )
                else:
                    vid_groups = []
                _wait_event(exact_done)
                check_cancelled()
                if video_versions:
                    version_workers = _late_video_versions_workers()
                    version_distinct = (
                        _ArrivalDistinctFilter(distinct, arrival_paths)
                        if arrival_paths is not None
                        else distinct
                    )
                    versions = find_video_version_matches(
                        records,
                        distinct=version_distinct,
                        progress=vid_versions_progress,
                        workers=version_workers,
                        cancelled=is_cancelled,
                        use_cache=use_cache,
                        cache_path=(
                            Path(cache_path).with_suffix(".video-versions.sqlite3")
                            if cache_path
                            else None
                        ),
                        errors=version_errors,
                    )
                    by_pair = {
                        tuple(sorted(m.path for m in members)): (members, details)
                        for members, details in versions
                        if arrival_paths is None or any(m.path in arrival_paths for m in members)
                    }
                    with publish_lock:
                        exact_lookup = dict(exact_path_to_group)
                    audio_pairs = []
                    for members in vid_groups:
                        keeper_path = pick_suggested_keep(members)
                        keeper = next(m for m in members if m.path == keeper_path)
                        for member in members:
                            if member is keeper:
                                continue
                            if (
                                arrival_paths is not None
                                and keeper.path not in arrival_paths
                                and member.path not in arrival_paths
                            ):
                                continue
                            key = tuple(sorted((keeper.path, member.path)))
                            if key in by_pair:
                                continue
                            if _in_same_exact_group([keeper, member], exact_lookup):
                                continue
                            audio_pairs.append((key, keeper, member))
                    if audio_pairs:
                        from . import similar_video as _sv_mod

                        frame_cache_cls = getattr(_sv_mod, "_FrameBatchCache", None)
                        audio_frame_cache = frame_cache_cls() if frame_cache_cls else None
                        audio_presence_cache: dict[str, bool] = {}
                        audio_presence_lock = threading.Lock()

                        def _audio_progress(done: int, total_pairs: int) -> None:
                            vid_versions_progress(
                                "video-versions-audio",
                                done,
                                total_pairs,
                                f"Comparing video audio {done}/{total_pairs}",
                            )

                        def _audio_job(item):
                            _, keeper_rec, member_rec = item
                            ranges = _aligned_audio_ranges(
                                keeper_rec,
                                member_rec,
                                is_cancelled,
                                frame_cache=audio_frame_cache,
                            )
                            audio = compare_video_audio(
                                keeper_rec.path,
                                member_rec.path,
                                ranges,
                                cancelled=is_cancelled,
                                presence_cache=audio_presence_cache,
                                presence_lock=audio_presence_lock,
                            )
                            return ranges, audio

                        audio_results = map_parallel(
                            _audio_job,
                            audio_pairs,
                            workers=max(1, min(_late_video_versions_workers(), len(audio_pairs))),
                            backend="thread",
                            progress=_audio_progress,
                            cancelled=is_cancelled,
                        )
                        for (key, keeper, member), (ranges, audio) in zip(
                            audio_pairs, audio_results, strict=True
                        ):
                            by_pair[key] = (
                                [keeper, member],
                                {
                                    "ranges": ranges,
                                    "relation": "same_footage",
                                    "audio": audio,
                                    "audio_scope": "sampled overlap",
                                    "manual_review": True,
                                },
                            )
                    built_versions: list[DuplicateGroup] = []
                    for members, details in by_pair.values():
                        if all(m.protected for m in members):
                            continue
                        if _in_same_exact_group(members, exact_lookup):
                            continue
                        group = build_one_group(
                            GroupKind.SIMILAR,
                            members,
                            match_basis="video_version",
                        )
                        if group is None:
                            continue
                        group.match_details = details
                        if details.get("relation") == "excerpt":
                            group.suggested_keep = max(
                                members, key=lambda m: (m.protected, m.duration or 0)
                            ).path
                        built_versions.append(group)
                    publish_prebuilt(built_versions)
                    count = len(built_versions)
                    _stage_finished(
                        "video_versions",
                        f"Found {count} video version group{'s' if count != 1 else ''}",
                    )
                else:
                    count = publish(GroupKind.SIMILAR, vid_groups)
                    _stage_finished(
                        "video",
                        f"Found {count} similar video group{'s' if count != 1 else ''}",
                    )
                video_fp_errors = [
                    record.error
                    for record in records
                    if record.error
                    and record.media_type == MediaType.VIDEO
                    and record.error.startswith(ERROR_VIDEO_FINGERPRINT_FAILED)
                ]
                stage_errors["similar_video"] = list(
                    dict.fromkeys([*version_errors, *video_fp_errors])
                )
                return time.monotonic() - started_at
            finally:
                # Always unblock dimension probing, even on cancel/error.
                video_done.set()

        def semantic_progress(phase: str, processed: int, total: int) -> None:
            if "judge" in phase:
                label = "judging"
                lo, hi = 0.92, 0.99
            elif "verify" in phase:
                label = "verifying"
                lo, hi = 0.85, 0.92
            elif "cluster" in phase:
                label = "matching"
                lo, hi = 0.70, 0.85
            elif "cooling" in phase:
                label = "paused while the Mac cools"
                lo, hi = 0.0, 0.70
            else:
                label = "embedding"
                lo, hi = 0.0, 0.70
            if total <= 0 or semantic_count <= 0:
                _stage_progress(
                    "semantic", f"Look-alikes {label}: {processed}/{total}", 0, 0
                )
                return
            frac = min(1.0, max(0.0, processed / total))
            done = int(semantic_count * (lo + (hi - lo) * frac))
            _stage_progress(
                "semantic",
                f"Look-alikes {label}: {processed}/{total}",
                min(max(0, semantic_count - 1), done),
                semantic_count,
            )

        def _semantic_stage() -> float:
            started_at = time.monotonic()
            try:
                try:
                    vectors, errors = embed_records(
                        records,
                        use_cache=use_cache,
                        cache_path=cache_path,
                        workers=semantic_prepare_workers,
                        progress=semantic_progress,
                        cancelled=is_cancelled,
                        background=not semantic_full_speed,
                    )
                except InterruptedError:
                    raise
                except Exception as exc:
                    stage_errors["semantic"].append(f"semantic look-alikes skipped: {exc}")
                    _stage_finished("semantic", "Look-alikes unavailable")
                    return time.monotonic() - started_at
                waiting_since = time.monotonic()
                _wait_event(exact_done)
                _wait_event(image_done)
                check_cancelled()
                # The recorded duration leaves out the wait for the hash stages.
                started_at += time.monotonic() - waiting_since
                with publish_lock:
                    excluded_edges = {
                        (a.path, b.path) if a.path < b.path else (b.path, a.path)
                        for group in groups
                        if group.kind == GroupKind.SIMILAR and group.match_basis != "video_version"
                        for idx, a in enumerate(group.members)
                        for b in group.members[idx + 1 :]
                    }
                    exact_sets = [set(paths) for paths in exact_path_sets]
                    exact_lookup = dict(exact_path_to_group)
                judge: DeciderJudge | None = None
                if decider:
                    try:
                        judge = DeciderJudge(
                            use_cache=use_cache,
                            cache_path=cache_path,
                            cancelled=is_cancelled,
                        )
                    except Exception as exc:
                        stage_errors["decider"].append(f"Decider verification skipped: {exc}")
                group_kwargs: dict[str, object] = {
                    "set_threshold": semantic_threshold,
                    "duplicate_threshold": semantic_duplicate_threshold,
                    "distinct": distinct,
                    "excluded_pairs": excluded_edges,
                    "exact_path_sets": exact_sets,
                    "use_cache": use_cache,
                    "cache_path": cache_path,
                    "progress": semantic_progress,
                    "cancelled": is_cancelled,
                    "judge": judge,
                    "workers": semantic_verify_workers,
                }
                if arrival_paths is not None and _accepts_kwarg(
                    group_semantic_vectors, "arrival_paths"
                ):
                    group_kwargs["arrival_paths"] = arrival_paths
                try:
                    set_lists, duplicate_lists = group_semantic_vectors(
                        records,
                        vectors,
                        **group_kwargs,
                    )
                except InterruptedError:
                    raise
                except Exception as exc:
                    stage_errors["semantic"].append(f"semantic look-alikes skipped: {exc}")
                    _stage_finished("semantic", "Look-alikes unavailable")
                    return time.monotonic() - started_at
                finally:
                    if judge is not None:
                        judge.close()
                        if judge.failure:
                            stage_errors["decider"].append(judge.failure)
                stage_errors["semantic"].extend(errors)

                built_semantic: list[DuplicateGroup] = []
                duplicate_count = 0
                set_count = 0
                for members, scores in duplicate_lists:
                    if all(m.protected for m in members):
                        continue
                    if arrival_paths is not None and not any(m.path in arrival_paths for m in members):
                        continue
                    if _in_same_exact_group(members, exact_lookup):
                        continue
                    group = build_one_group(
                        GroupKind.SIMILAR,
                        members,
                        match_basis="semantic",
                        match_scores=scores,
                    )
                    if group is None:
                        continue
                    built_semantic.append(group)
                    duplicate_count += 1
                for members, scores in set_lists:
                    if (
                        arrival_paths is not None
                        and (len(members) < 2 or not any(m.path in arrival_paths for m in members))
                    ):
                        continue
                    group = build_set_group(members, scores)
                    if group is None:
                        continue
                    built_semantic.append(group)
                    set_count += 1
                publish_prebuilt(built_semantic)
                _stage_finished(
                    "semantic",
                    f"Found {set_count} set{'s' if set_count != 1 else ''} and "
                    f"{duplicate_count} look-alike duplicate group"
                    f"{'s' if duplicate_count != 1 else ''}",
                )
                return time.monotonic() - started_at
            finally:
                semantic_done.set()

        def _dimensions_stage() -> float:
            try:
                _wait_event(image_done)
                _wait_event(video_done)
                check_cancelled()
                started_at = time.monotonic()

                def resolution_progress(processed: int, total: int) -> None:
                    _stage_progress(
                        "dims",
                        f"Dimensions {processed}/{total}",
                        processed,
                        total,
                    )

                stage_errors["low_resolution"] = _populate_missing_dimensions(
                    independent_records,
                    media_types=set(low_resolution_bounds),
                    workers=n_workers,
                    cancelled=is_cancelled,
                    progress=resolution_progress,
                )
                _stage_finished("dims", "Dimensions ready")
                return time.monotonic() - started_at
            finally:
                # Always unblock review-group publishing, even on cancel/error.
                dims_done.set()

        def _review_stage() -> float:
            _wait_event(dims_done)
            check_cancelled()
            started_at = time.monotonic()
            review_groups: list[DuplicateGroup] = []
            if find_low_resolution:
                raw_excluded = {
                    record.path
                    for record in independent_records
                    if record.extension in RAW_IMAGE_EXTS
                }
                review_groups.extend(
                    build_low_resolution_groups(
                        independent_records,
                        max_pixels=max(1, int(low_resolution_max_pixels)),
                        skip_paths=kept_paths(independent_records) | raw_excluded,
                        media_types=set(low_resolution_bounds),
                        max_pixels_by_media_type=low_resolution_bounds,
                    )
                )
            review_groups.extend(
                build_random_review_groups(
                    independent_records, count=max(0, int(random_review_count))
                )
            )
            publish_prebuilt(review_groups)
            return time.monotonic() - started_at

        def _human_stage() -> float:
            started_at = time.monotonic()
            try:

                def human_progress(
                    phase: str,
                    processed: int,
                    total: int,
                    message: str | None = None,
                ) -> None:
                    _stage_progress(
                        "human",
                        message or f"Person detection {processed}/{total}",
                        processed,
                        total,
                    )

                no_human_files = find_no_human_files(
                    human_records,
                    backend=human_backend,
                    photon_model=photon_model,
                    workers=human_workers,
                    progress=human_progress,
                    cancelled=is_cancelled,
                    confirm_with_faces=not run_faces,
                )
                stage_errors["human_detection"] = [
                    record.error or "person analysis failed"
                    for record in human_records
                    if record.human_detection_status == "analysis_failed"
                ]
                if run_faces:
                    _stage_finished(
                        "human",
                        "Person detection finished; face counts will confirm "
                        "Non-Human candidates",
                    )
                else:
                    publish_prebuilt(build_no_human_groups(no_human_files))
                    _stage_finished(
                        "human",
                        f"Found {len(no_human_files)} file"
                        f"{'' if len(no_human_files) == 1 else 's'} "
                        "without detected people",
                    )
                return time.monotonic() - started_at
            finally:
                # Always unblock Non-Human publishing in the face stage.
                human_done.set()

        def _face_stage() -> float:
            started_at = time.monotonic()
            try:

                def face_progress(phase: str, processed: int, total: int) -> None:
                    _stage_progress(
                        "face",
                        f"Face counting {processed}/{total}",
                        processed,
                        total,
                    )

                count_faces_in_files(
                    independent_records if arrival_paths is not None else records,
                    workers=cpu_workers,
                    progress=face_progress,
                    cancelled=is_cancelled,
                )
                stage_errors["face_detection"] = [
                    record.error or "face counting failed"
                    for record in face_candidates
                    if record.face_count is None
                ]
                files_with_faces = sum(
                    1 for record in face_candidates if (record.face_count or 0) > 0
                )
                _stage_finished(
                    "face",
                    f"Found faces in {files_with_faces} "
                    f"file{'s' if files_with_faces != 1 else ''}",
                )
                duration = time.monotonic() - started_at
                publish_prebuilt(build_faces_groups(face_candidates))
                if find_no_humans:
                    _wait_event(human_done)
                    check_cancelled()
                    confirmed = build_no_human_groups(human_records)
                    confirmed_count = sum(len(group.members) for group in confirmed)
                    publish_prebuilt(confirmed)
                    _stage_finished(
                        "human",
                        f"Found {confirmed_count} file"
                        f"{'' if confirmed_count == 1 else 's'} "
                        "without detected people",
                    )
                return duration
            finally:
                face_done.set()

        stage_jobs: dict[str, Callable[[], float]] = {}
        if run_exact:
            stage_jobs["exact"] = _exact_stage
        else:
            exact_done.set()
        if run_borders:
            stage_jobs["borders"] = _border_stage
        else:
            borders_done.set()
        if run_similar_images:
            stage_jobs["image"] = _image_stage
        else:
            image_done.set()
        if run_similar_videos:
            stage_jobs["video"] = _video_stage
        else:
            video_done.set()
        if run_semantic:
            stage_jobs["semantic"] = _semantic_stage
        else:
            semantic_done.set()
        if run_dimensions:
            stage_jobs["dims"] = _dimensions_stage
        else:
            dims_done.set()
        if run_human:
            stage_jobs["human"] = _human_stage
        else:
            human_done.set()
        if run_faces:
            stage_jobs["face"] = _face_stage
        else:
            face_done.set()
        if _build_review_active:
            stage_jobs["review"] = _review_stage

        stage_results: dict[str, float] = {}
        stage_exc: BaseException | None = None
        if stage_jobs:
            with ThreadPoolExecutor(
                max_workers=len(stage_jobs), thread_name_prefix="scan-stage"
            ) as stage_pool:
                futures = {
                    stage_pool.submit(fn): name for name, fn in stage_jobs.items()
                }
                pending = set(futures)
                while pending:
                    finished, pending = wait(
                        pending, timeout=0.1, return_when=FIRST_COMPLETED
                    )
                    if is_cancelled() and not cancel_internal.is_set():
                        cancel_internal.set()
                        for remaining in pending:
                            remaining.cancel()
                    for fut in finished:
                        name = futures[fut]
                        try:
                            stage_results[name] = fut.result()
                        except BaseException as exc:
                            cancel_internal.set()
                            if stage_exc is None or (
                                isinstance(exc, InterruptedError)
                                and not isinstance(stage_exc, InterruptedError)
                            ):
                                stage_exc = exc
                            for remaining in pending:
                                remaining.cancel()
        if stage_exc is not None:
            # Pool has completely exited above, so records are no longer mutated by
            # stage threads when storing partial analysis on cancellation/failure.
            if cache is not None:
                try:
                    cache.reopen()
                    share_exact_sibling_analysis(records)
                    cache.reuse_content_analysis(records)
                    cache.store_all(records)
                    cache.close()
                except Exception:
                    pass
                cache = None
            raise stage_exc

        if run_exact:
            stage_durations["exact"] = stage_results["exact"]
        if run_similar_images:
            stage_durations["similar_image"] = stage_results["image"]
        if run_similar_videos:
            stage_durations["similar_video"] = stage_results["video"]
        if run_semantic:
            stage_durations["semantic"] = stage_results["semantic"]
        if run_dimensions:
            stage_durations["low_resolution"] = stage_results["dims"]
        if run_human:
            stage_durations["human_detection"] = stage_results["human"]
        if run_faces:
            stage_durations["face_detection"] = stage_results["face"]
        if run_borders:
            stage_durations["black_borders"] = stage_results["borders"]

    live_warnings: list[str] = []
    if not incremental_empty:
        emit(
            "finalizing",
            len(records),
            len(records),
            "Checking Live Photos…",
            eta_zero=True,
        )
        share_exact_sibling_analysis(records)
        live_pairs = detect_live_photo_pairs(records)
        live_warnings = _annotate_live_photo_groups(groups, live_pairs)

    groups.sort(key=lambda x: x.reclaimable_bytes, reverse=True)

    cache_errors: list[str] = []
    if cache is not None:
        emit(
            "finalizing",
            len(records),
            len(records),
            "Saving analysis cache…",
            eta_zero=True,
        )
        try:
            cache.reopen()
            if run_similar:
                # Distinct rows recorded before a file was hashed are
                # stat-only; now that the scan re-hashed everything, give
                # them the content identity that survives metadata drift.
                cache.backfill_distinct_content(records)
            cache.reuse_content_analysis(records)
            cache.store_all(records)
        except Exception as exc:
            cache_errors.append(f"cache store failed: {exc}")
        try:
            cache.close()
        except Exception as exc:
            cache_errors.append(f"cache close failed: {exc}")

    image_records = [
        record
        for record in records
        if record.media_type in (MediaType.IMAGE, MediaType.GIF)
    ]
    video_records = [record for record in records if record.media_type == MediaType.VIDEO]
    size_counts: dict[int, int] = {}
    for record in records:
        if record.size > 0:
            size_counts[record.size] = size_counts.get(record.size, 0) + 1
    exact_candidates = [
        record for record in records if record.size > 0 and size_counts[record.size] > 1
    ]

    exact_failures = stage_errors["exact"]
    image_failures = stage_errors["similar_image"]
    video_cache_warnings = [
        error
        for error in stage_errors["similar_video"]
        if error.startswith("video version cache unavailable")
    ]
    video_failures = [
        error
        for error in stage_errors["similar_video"]
        if error not in video_cache_warnings
    ]
    image_attempted = (
        len(image_records) if run_similar_images and len(image_records) >= 2 else 0
    )
    video_dependency = ffmpeg_available()
    video_attempted = (
        len(video_records)
        if run_similar_videos and len(video_records) >= 2 and video_dependency
        else 0
    )
    semantic_failures = stage_errors["semantic"]
    semantic_ready = (
        run_semantic
        and semantic_available()
        and not any(
            error.startswith("semantic look-alikes skipped") for error in semantic_failures
        )
    )
    if run_semantic and not semantic_available() and not semantic_failures:
        semantic_failures = ["semantic look-alikes skipped: DigUp is not installed"]
    semantic_attempted = (
        len(image_records) if semantic_ready and len(image_records) >= 2 else 0
    )
    human_failures = stage_errors["human_detection"]
    face_failures = stage_errors["face_detection"]
    human_scope_count = len(independent_records) if (find_no_humans and not incremental_empty) else 0
    face_records = [
        record
        for record in independent_records
        if record.media_type in FACE_MEDIA_TYPES
    ]
    face_attempted = len(face_records) if (count_faces and not incremental_empty) else 0
    resolution_records = [
        record
        for record in independent_records
        if record.media_type in set(low_resolution_bounds)
        and record.extension not in RAW_IMAGE_EXTS
    ]
    resolution_failures = [
        record for record in resolution_records if not (record.width and record.height)
    ]
    low_res_attempted = (
        len(resolution_records) if (find_low_resolution and not incremental_empty) else 0
    )

    stages = {
        "inventory": StageDiagnostics(
            unit="roots",
            attempted=attempted_roots_count,
            succeeded=len(resolved_roots),
            failed=len(root_errors),
            duration_seconds=stage_durations.get("inventory", 0.0),
            warnings=[*root_errors, *live_warnings][:10],
        ),
        "exact": StageDiagnostics(
            attempted=len(exact_candidates) if run_exact else 0,
            succeeded=(len(exact_candidates) - len(exact_failures)) if run_exact else 0,
            failed=len(exact_failures) if run_exact else 0,
            skipped=len(records) - (len(exact_candidates) if run_exact else 0),
            duration_seconds=stage_durations.get("exact", 0.0),
            warnings=(
                [f"{len(exact_failures)} exact-hash candidate(s) failed"]
                if exact_failures
                else []
            ),
        ),
        "similar_image": StageDiagnostics(
            attempted=image_attempted,
            succeeded=max(0, image_attempted - len(image_failures)),
            failed=len(image_failures),
            skipped=len(records) - image_attempted,
            duration_seconds=stage_durations.get("similar_image", 0.0),
            warnings=(
                [f"{len(image_failures)} image hash(es) failed"]
                if image_failures
                else []
            ),
        ),
        "similar_video": StageDiagnostics(
            attempted=video_attempted,
            succeeded=max(0, video_attempted - len(video_failures)),
            failed=min(video_attempted, len(video_failures)),
            skipped=len(records) - video_attempted,
            duration_seconds=stage_durations.get("similar_video", 0.0),
            warnings=(
                ["ffmpeg/ffprobe unavailable; eligible videos were not analyzed"]
                if run_similar_videos and video_records and not video_dependency
                else (
                    [(
                        f"{min(video_attempted, len(video_failures))} video file(s) "
                        "could not be fully analyzed"
                    )]
                    if video_failures
                    else []
                )
            )
            + video_cache_warnings[:5],
        ),
        "semantic": StageDiagnostics(
            attempted=semantic_attempted,
            succeeded=max(0, semantic_attempted - len(semantic_failures)),
            failed=len(semantic_failures) if semantic_attempted else 0,
            skipped=len(records) - semantic_attempted,
            duration_seconds=stage_durations.get("semantic", 0.0),
            warnings=(
                semantic_failures[:1]
                if run_semantic and not semantic_attempted and semantic_failures
                else (
                    [f"{len(semantic_failures)} image(s) could not be embedded"]
                    if semantic_failures
                    else []
                )
            )
            + stage_errors["decider"][:1],
        ),
        "human_detection": StageDiagnostics(
            attempted=human_scope_count,
            succeeded=max(0, human_scope_count - len(human_failures)),
            failed=len(human_failures) if human_scope_count else 0,
            skipped=len(records) - human_scope_count,
            duration_seconds=stage_durations.get("human_detection", 0.0),
            warnings=(
                [f"{len(human_failures)} file(s) could not be analyzed for people"]
                if human_failures
                else []
            ),
        ),
        "face_detection": StageDiagnostics(
            attempted=face_attempted,
            succeeded=max(0, face_attempted - len(face_failures)),
            failed=len(face_failures) if face_attempted else 0,
            skipped=len(records) - face_attempted,
            duration_seconds=stage_durations.get("face_detection", 0.0),
            warnings=(
                [f"{len(face_failures)} file(s) could not be analyzed for faces"]
                if face_failures
                else []
            ),
        ),
        "black_borders": StageDiagnostics(
            attempted=len(border_records),
            succeeded=max(0, len(border_records) - len(stage_errors["black_borders"])),
            failed=len(stage_errors["black_borders"]),
            skipped=len(records) - len(border_records),
            duration_seconds=stage_durations.get("black_borders", 0.0),
            warnings=(
                [
                    f"{len(stage_errors['black_borders'])} file(s) could not be "
                    + "checked for crops"
                ]
                if stage_errors["black_borders"]
                else []
            ),
        ),
        "low_resolution": StageDiagnostics(
            attempted=low_res_attempted,
            succeeded=max(0, low_res_attempted - len(resolution_failures))
            if low_res_attempted
            else 0,
            failed=len(resolution_failures) if low_res_attempted else 0,
            skipped=len(records) - low_res_attempted,
            duration_seconds=stage_durations.get("low_resolution", 0.0),
            warnings=(
                [(
                    f"{len(resolution_failures)} file(s) could not be checked for "
                    "low resolution"
                )]
                if low_res_attempted and resolution_failures
                else []
            ),
        ),
    }
    if cache_errors:
        # A cache write failure means the next scan silently redoes the work;
        # surface it instead of dropping it.
        stages["cache"] = StageDiagnostics(
            unit="cache",
            attempted=1,
            succeeded=0,
            failed=len(cache_errors),
            warnings=cache_errors[:10],
        )
    total_duration = max(0.0, time.monotonic() - started)
    recorded_errors = list(
        dict.fromkeys(
            [*root_errors]
            + [error for errors in stage_errors.values() for error in errors]
            + cache_errors
            + [record.error for record in records if record.error]
        )
    )
    result = ScanResult(
        roots=[str(root) for root in resolved_roots],
        files=records,
        groups=groups,
        errors=recorded_errors,
        protected_folders=[str(p) for p in protected_roots],
        diagnostics=ScanDiagnostics(
            total_duration_seconds=total_duration,
            first_duplicate_seconds=first_duplicate_seconds,
            cache_hits=cache_hits,
            stages=stages,
        ),
    )
    result.recompute_stats()

    done_message = (
        f"Done — {result.exact_groups} exact, {result.similar_groups} similar groups, "
        f"{result.low_resolution_files} low-resolution, "
        f"{result.random_review_files} random review, {result.no_human_files} non-human"
        + (f", {result.faces_files} faces" if count_faces else "")
        + (f", {result.crop_files} to crop" if find_black_borders else "")
        + f" ({len(records)} files)"
    )
    with prog_lock:
        prog.groups_found = len(groups)
        prog.elapsed_seconds = total_duration
        prog.eta_seconds = 0.0
        if emit_done:
            prog.done = True
            prog.phase = "done"
            prog.message = done_message
        else:
            prog.done = False
            prog.phase = "finalizing"
            prog.message = "Finalizing results…"
        if progress:
            progress(replace(prog))

    return result


def _resolve_stream_roots(roots: list[str | Path]) -> tuple[list[Path], list[str]]:
    """De-duplicate and resolve scan roots, collecting errors for missing or blocked paths."""
    resolved: list[Path] = []
    errors: list[str] = []
    seen: set[str] = set()
    for root in roots:
        path = Path(root).expanduser().resolve(strict=False)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.exists():
            errors.append(f"scan root does not exist: {path}")
            continue
        if is_in_photos_library(path):
            errors.append(
                "Photos libraries cannot be scanned directly; export media from "
                f"Photos.app to a normal folder first: {path}"
            )
            continue
        resolved.append(path)
    return resolved, errors


def _aggregate_parallel_diagnostics(
    stream_diags: list[ScanDiagnostics],
    *,
    total_duration: float,
    root_errors: list[str],
    attempted_roots_count: int,
    resolved_roots_count: int,
    live_warnings: list[str],
) -> ScanDiagnostics:
    """Merge per-stream ScanDiagnostics into a combined ScanDiagnostics summary."""
    cache_hits = sum(diag.cache_hits for diag in stream_diags)
    first_candidates = [
        diag.first_duplicate_seconds
        for diag in stream_diags
        if diag.first_duplicate_seconds is not None
    ]
    first_duplicate_seconds = min(first_candidates) if first_candidates else None

    stage_names: list[str] = []
    seen_stages: set[str] = set()
    for diag in stream_diags:
        for name in diag.stages:
            if name not in seen_stages:
                seen_stages.add(name)
                stage_names.append(name)

    stages: dict[str, StageDiagnostics] = {}
    for name in stage_names:
        items = [diag.stages[name] for diag in stream_diags if name in diag.stages]
        unit = items[0].unit if items else "files"
        warnings = list(dict.fromkeys(w for item in items for w in item.warnings))
        if name == "inventory":
            stages[name] = StageDiagnostics(
                unit="roots",
                attempted=attempted_roots_count,
                succeeded=resolved_roots_count,
                failed=len(root_errors),
                duration_seconds=max((item.duration_seconds for item in items), default=0.0),
                warnings=list(dict.fromkeys([*root_errors, *warnings, *live_warnings]))[:10],
            )
        else:
            stages[name] = StageDiagnostics(
                unit=unit,
                attempted=sum(item.attempted for item in items),
                succeeded=sum(item.succeeded for item in items),
                failed=sum(item.failed for item in items),
                skipped=sum(item.skipped for item in items),
                duration_seconds=max((item.duration_seconds for item in items), default=0.0),
                warnings=warnings[:10],
            )
    if "inventory" not in stages:
        stages["inventory"] = StageDiagnostics(
            unit="roots",
            attempted=attempted_roots_count,
            succeeded=resolved_roots_count,
            failed=len(root_errors),
            warnings=[*root_errors, *live_warnings][:10],
        )

    return ScanDiagnostics(
        total_duration_seconds=total_duration,
        first_duplicate_seconds=first_duplicate_seconds,
        cache_hits=cache_hits,
        stages=stages,
    )


def run_scans_parallel(
    roots: list[str | Path],
    *,
    max_streams: int | None = None,
    preferred_folders: list[str] | None = None,
    protected_folders: list[str] | None = None,
    video_versions: bool = False,
    exact: bool = True,
    similar: bool = True,
    semantic: bool = False,
    semantic_threshold: float = SEMANTIC_THRESHOLD,
    semantic_duplicate_threshold: float = SEMANTIC_DUPLICATE_THRESHOLD,
    semantic_full_speed: bool = False,
    decider: bool = False,
    find_no_humans: bool = False,
    count_faces: bool = False,
    find_black_borders: bool = False,
    crop_aggressiveness: str = "balanced",
    find_low_resolution: bool = True,
    low_resolution_images: bool = True,
    low_resolution_gifs: bool = True,
    low_resolution_videos: bool = True,
    low_resolution_max_pixels: int = LOW_RESOLUTION_MAX_PIXELS,
    low_resolution_image_max_pixels: int | None = None,
    low_resolution_gif_max_pixels: int | None = None,
    low_resolution_video_max_pixels: int | None = None,
    random_review_count: int = DEFAULT_RANDOM_REVIEW_COUNT,
    human_backend: str = DEFAULT_HUMAN_BACKEND,
    photon_model: str = DEFAULT_PHOTON_MODEL,
    include_images: bool = True,
    include_gifs: bool = True,
    include_videos: bool = True,
    include_hidden: bool = False,
    image_threshold: int = IMG_THRESHOLD,
    video_threshold: int = VID_THRESHOLD,
    use_cache: bool = True,
    cache_path: str | Path | None = None,
    workers: int | None = None,
    exclusions: list[str] | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: ProgressCb | None = None,
    on_stream_progress: StreamProgressCb | None = None,
    on_group: GroupCb | None = None,
    on_groups: GroupsCb | None = None,
    emit_done: bool = True,
    baseline: dict[str, list] | None = None,
    new_only: bool | None = None,
) -> ScanResult:
    """Scan each root as an independent, concurrent stream."""
    if protected_folders:
        raise ValueError("Protected libraries require comparing folders together")
    _configure_media_runtime()
    resolved_roots, root_errors = _resolve_stream_roots(roots)
    attempted_roots_count = len({str(Path(r).expanduser().resolve(strict=False)) for r in roots})
    if not resolved_roots:
        empty_diag = _aggregate_parallel_diagnostics(
            [],
            total_duration=0.0,
            root_errors=root_errors,
            attempted_roots_count=attempted_roots_count,
            resolved_roots_count=0,
            live_warnings=[],
        )
        result = ScanResult(
            roots=[],
            files=[],
            groups=[],
            errors=root_errors,
            diagnostics=empty_diag,
        )
        result.recompute_stats()
        if progress:
            done = ScanProgress(
                phase="done" if emit_done else "finalizing",
                done=emit_done,
                message=(
                    "No valid folders to scan" if emit_done else "Finalizing results…"
                ),
            )
            progress(done)
        return result

    n_streams = len(resolved_roots)
    max_concurrent = min(n_streams, resolve_workers(max_streams))
    budget_streams = max_concurrent if max_streams is not None else n_streams
    per_stream_workers = max(2, -(-resolve_workers(workers) // budget_streams))

    started = time.monotonic()
    cancel_internal = threading.Event()

    def is_cancelled() -> bool:
        return cancel_internal.is_set() or bool(cancelled and cancelled())

    lock = threading.RLock()
    stream_progress: dict[int, ScanProgress] = {}
    last_agg = {"t": 0.0, "done": -1}
    all_files: list = []
    all_groups: list[DuplicateGroup] = []
    stream_diagnostics: list[ScanDiagnostics] = []
    seen_files: set[str] = set()
    seen_inodes: set[tuple[int, int]] = set()
    seen_groups: set[str] = set()
    group_roots: dict[str, str] = {}
    stream_errors: list[str] = []

    def emit_aggregate(*, force: bool = False) -> None:
        if progress is None:
            return
        now = time.monotonic()
        files_found = sum(p.files_found for p in stream_progress.values())
        files_processed = sum(p.files_processed for p in stream_progress.values())
        groups_found = sum(p.groups_found for p in stream_progress.values())
        bytes_scanned = sum(p.bytes_scanned for p in stream_progress.values())
        done_count = sum(1 for p in stream_progress.values() if p.done)
        all_done = done_count == n_streams
        if not (
            force
            or all_done
            or done_count != last_agg["done"]
            or last_agg["t"] == 0.0
            or now - last_agg["t"] >= PROGRESS_CALLBACK_MIN_INTERVAL
        ):
            return
        last_agg["t"] = now
        last_agg["done"] = done_count
        agg_done = all_done and emit_done
        agg_phase = "done" if agg_done else ("finalizing" if all_done else "scanning")
        agg_message = (
            f"Scanning {n_streams} folder{'s' if n_streams != 1 else ''} "
            f"in parallel — {done_count}/{n_streams} done"
            if not all_done
            else (
                f"Done — {done_count}/{n_streams} folders, {groups_found} groups"
                if emit_done
                else "Finalizing results…"
            )
        )
        agg = ScanProgress(
            phase=agg_phase,
            files_found=files_found,
            files_processed=files_processed,
            groups_found=groups_found,
            bytes_scanned=bytes_scanned,
            done=agg_done,
            elapsed_seconds=max(0.0, now - started),
            message=agg_message,
        )
        progress(agg)

    def scan_one(index: int, root: Path) -> ScanResult:
        def stream_progress_cb(prog: ScanProgress) -> None:
            tagged = replace(prog, stream_index=index, root=str(root))
            with lock:
                stream_progress[index] = tagged
                if on_stream_progress:
                    on_stream_progress(tagged)
                emit_aggregate()

        def stream_groups_cb(batch: list[DuplicateGroup]) -> None:
            fresh: list[DuplicateGroup] = []
            with lock:
                for group in batch:
                    if group.id in group_roots:
                        group.root = group_roots[group.id]
                        continue
                    group.root = str(root)
                    group_roots[group.id] = group.root
                    fresh.append(group)
            if not fresh:
                return
            if on_groups is not None:
                with lock:
                    on_groups(fresh)
            elif on_group is not None:
                with lock:
                    for group in fresh:
                        on_group(group)

        extra_kwargs: dict[str, object] = {}
        if baseline is not None or new_only is not None:
            extra_kwargs["baseline"] = baseline
            extra_kwargs["new_only"] = new_only

        run_scan_kwargs: dict[str, object] = {
            "preferred_folders": preferred_folders,
            "protected_folders": protected_folders,
            "video_versions": video_versions,
            "exact": exact,
            "similar": similar,
            "semantic": semantic,
            "semantic_threshold": semantic_threshold,
            "semantic_duplicate_threshold": semantic_duplicate_threshold,
            "semantic_full_speed": semantic_full_speed,
            "decider": decider,
            "find_no_humans": find_no_humans,
            "count_faces": count_faces,
            "find_black_borders": find_black_borders,
            "crop_aggressiveness": crop_aggressiveness,
            "find_low_resolution": find_low_resolution,
            "low_resolution_images": low_resolution_images,
            "low_resolution_gifs": low_resolution_gifs,
            "low_resolution_videos": low_resolution_videos,
            "low_resolution_max_pixels": low_resolution_max_pixels,
            "low_resolution_image_max_pixels": low_resolution_image_max_pixels,
            "low_resolution_gif_max_pixels": low_resolution_gif_max_pixels,
            "low_resolution_video_max_pixels": low_resolution_video_max_pixels,
            "random_review_count": random_review_count,
            "human_backend": human_backend,
            "photon_model": photon_model,
            "include_images": include_images,
            "include_gifs": include_gifs,
            "include_videos": include_videos,
            "include_hidden": include_hidden,
            "image_threshold": image_threshold,
            "video_threshold": video_threshold,
            "use_cache": use_cache,
            "cache_path": cache_path,
            "workers": per_stream_workers,
            "_workers_chosen": bool(workers and workers > 0),
            "exclusions": exclusions,
            "cancelled": is_cancelled,
            "progress": stream_progress_cb,
            "on_group": on_group,
            "_build_review_groups": False,
            **extra_kwargs,
        }
        if on_groups is not None or on_group is not None:
            run_scan_kwargs["on_groups"] = stream_groups_cb
            run_scan_kwargs["on_group"] = None
        return run_scan([root], **run_scan_kwargs)

    interrupted = False
    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        futures = {
            ex.submit(scan_one, i, root): (i, root)
            for i, root in enumerate(resolved_roots)
        }
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in finished:
                _i, root = futures[fut]
                try:
                    sub = fut.result()
                except InterruptedError:
                    interrupted = True
                    cancel_internal.set()
                    for remaining in pending:
                        remaining.cancel()
                    continue
                except Exception as exc:
                    stream_errors.append(f"{root}: {exc}")
                    continue
                with lock:
                    if sub.diagnostics is not None:
                        stream_diagnostics.append(sub.diagnostics)
                    for record in sub.files:
                        inode_key = (record.device, record.inode) if record.inode else None
                        if record.path in seen_files:
                            continue
                        if inode_key is not None and inode_key in seen_inodes:
                            continue
                        seen_files.add(record.path)
                        if inode_key is not None:
                            seen_inodes.add(inode_key)
                        all_files.append(record)
                    for group in sub.groups:
                        if group.id not in seen_groups:
                            seen_groups.add(group.id)
                            group.root = group_roots.get(group.id, str(root))
                            all_groups.append(group)
                    stream_errors.extend(sub.errors)

    if interrupted:
        raise InterruptedError("scan cancelled")

    incremental = baseline is not None and new_only is not False
    if incremental and baseline is not None:
        arrival_paths = {
            rec.path for rec in all_files if baseline.get(rec.path) != file_identity(rec)
        }
        independent_pool = [
            rec for rec in all_files if rec.path in arrival_paths and not rec.protected
        ]
    else:
        independent_pool = all_files

    review_groups: list[DuplicateGroup] = []
    if find_low_resolution and independent_pool:
        low_resolution_bounds = _low_resolution_bounds(
            default_max_pixels=low_resolution_max_pixels,
            images=low_resolution_images,
            gifs=low_resolution_gifs,
            videos=low_resolution_videos,
            image_max_pixels=low_resolution_image_max_pixels,
            gif_max_pixels=low_resolution_gif_max_pixels,
            video_max_pixels=low_resolution_video_max_pixels,
        )
        raw_excluded = {
            record.path for record in independent_pool if record.extension in RAW_IMAGE_EXTS
        }
        review_groups.extend(
            build_low_resolution_groups(
                independent_pool,
                max_pixels=max(1, int(low_resolution_max_pixels)),
                skip_paths=kept_paths(independent_pool) | raw_excluded,
                media_types=set(low_resolution_bounds),
                max_pixels_by_media_type=low_resolution_bounds,
            )
        )
    if independent_pool:
        review_groups.extend(
            build_random_review_groups(independent_pool, count=max(0, int(random_review_count)))
        )
    all_groups.extend(review_groups)
    if review_groups:
        if on_groups is not None:
            on_groups(list(review_groups))
        elif on_group is not None:
            for group in review_groups:
                on_group(group)

    live_warnings: list[str] = []
    if not (incremental and not independent_pool):
        live_pairs = detect_live_photo_pairs(all_files)
        live_warnings = _annotate_live_photo_groups(all_groups, live_pairs)

    all_groups.sort(key=lambda g: g.reclaimable_bytes, reverse=True)
    total_duration = max(0.0, time.monotonic() - started)
    agg_diagnostics = _aggregate_parallel_diagnostics(
        stream_diagnostics,
        total_duration=total_duration,
        root_errors=root_errors,
        attempted_roots_count=attempted_roots_count,
        resolved_roots_count=len(resolved_roots),
        live_warnings=live_warnings,
    )
    result = ScanResult(
        roots=[str(root) for root in resolved_roots],
        files=all_files,
        groups=all_groups,
        errors=list(dict.fromkeys([*root_errors, *stream_errors])),
        diagnostics=agg_diagnostics,
    )
    result.recompute_stats()

    if progress:
        final = ScanProgress(
            phase="done" if emit_done else "finalizing",
            done=emit_done,
            files_found=len(all_files),
            groups_found=len(all_groups),
            bytes_scanned=sum(r.size for r in all_files),
            elapsed_seconds=total_duration,
            eta_seconds=0.0,
            message=(
                (
                    f"Done — {result.exact_groups} exact, "
                    f"{result.similar_groups} similar groups, "
                    f"{result.low_resolution_files} low-resolution, "
                    f"{result.random_review_files} random review, "
                    f"{result.no_human_files} non-human, "
                    f"{result.faces_files} faces "
                    f"across {n_streams} folder{'s' if n_streams != 1 else ''}"
                )
                if emit_done
                else "Finalizing results…"
            ),
        )
        progress(final)

    return result
