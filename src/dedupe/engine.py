"""Orchestrates full scan: inventory → exact → similar → groups."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .cache import HashCache
from .exact import (
    ERROR_PARTIAL_HASH_FAILED,
    ERROR_SHA256_FAILED,
    find_exact_groups,
)
from .face_detection import FACE_MEDIA_TYPES, count_faces_in_files
from .grouping import (
    DEFAULT_RANDOM_REVIEW_COUNT,
    LOW_RESOLUTION_MAX_PIXELS,
    build_faces_groups,
    build_low_resolution_groups,
    build_no_human_groups,
    build_one_group,
    build_random_review_groups,
)
from .human_detection import (
    DEFAULT_BACKEND as DEFAULT_HUMAN_BACKEND,
)
from .human_detection import (
    DEFAULT_PHOTON_MODEL,
    find_no_human_files,
)
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
from .parallel import map_parallel, resolve_workers, split_cpu_budget
from .scanner import inventory, is_in_photos_library
from .similar_image import DEFAULT_THRESHOLD as IMG_THRESHOLD
from .similar_image import (
    ERROR_IMAGE_HASH_FAILED,
    find_similar_image_groups,
    probe_image_dimensions,
)
from .similar_video import DEFAULT_THRESHOLD as VID_THRESHOLD
from .similar_video import (
    ERROR_VIDEO_FINGERPRINT_FAILED,
    ffmpeg_available,
    find_similar_video_groups,
    probe_video,
)

ProgressCb = Callable[[ScanProgress], None]
GroupCb = Callable[[DuplicateGroup], None]
StreamProgressCb = Callable[[ScanProgress], None]

# Minimum seconds between mid-stage progress callback invocations.
PROGRESS_CALLBACK_MIN_INTERVAL = 0.1


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
        and not (record.width and record.height)
    ]
    if not missing:
        return []

    def probe(record):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        try:
            if record.media_type == MediaType.VIDEO:
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
    exact: bool = True,
    similar: bool = True,
    find_no_humans: bool = False,
    count_faces: bool = False,
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
    _build_review_groups: bool = True,
) -> ScanResult:
    """Run a full scan.

    Every enabled check — exact hashing, image/GIF similarity, video
    similarity, dimension probing, person detection, and face counting — runs
    concurrently on one stage pool. ``on_group`` is called as soon as each
    duplicate group is finalized so UIs can stream results instead of waiting
    for the whole scan. Result-affecting order is kept with events: exact
    groups are always published before similar groups because similar grouping
    consults the exact-group membership, and Non-Human groups wait for face
    counts when faces are also being counted (a counted face vetoes
    membership).
    """
    n_workers = resolve_workers(workers)
    low_resolution_bounds = _low_resolution_bounds(
        default_max_pixels=low_resolution_max_pixels,
        images=low_resolution_images,
        gifs=low_resolution_gifs,
        videos=low_resolution_videos,
        image_max_pixels=low_resolution_image_max_pixels,
        gif_max_pixels=low_resolution_gif_max_pixels,
        video_max_pixels=low_resolution_video_max_pixels,
    )
    prog = ScanProgress(phase="starting", message="Starting scan…")
    started = time.monotonic()
    phase_started = started
    previous_phase = prog.phase
    groups: list[DuplicateGroup] = []
    exact_path_sets: list[set[str]] = []
    stage_durations: dict[str, float] = {}
    stage_errors: dict[str, list[str]] = {
        "exact": [],
        "similar_image": [],
        "similar_video": [],
        "human_detection": [],
        "face_detection": [],
        "low_resolution": [],
    }
    cache_hits = 0

    # Per-file progress arrives from every concurrent stage; invoking the
    # caller's callback for each one means hundreds of thousands of lock
    # acquisitions on a big library. Throttle mid-stage callbacks and always
    # deliver phase changes, milestones, and stage completions.
    last_callback = {"t": 0.0}

    def emit(phase: str, processed: int = 0, total: int = 0, message: str = "") -> None:
        nonlocal phase_started, previous_phase
        now = time.monotonic()
        phase_changed = phase != previous_phase
        if phase_changed:
            phase_started = now
            previous_phase = phase
        prog.phase = phase
        prog.files_processed = processed
        prog.groups_found = len(groups)
        if total:
            prog.files_found = max(prog.files_found, total)
        if message:
            prog.message = message
        prog.elapsed_seconds = max(0.0, now - started)
        phase_elapsed = max(0.0, now - phase_started)
        if total > 0 and 0 < processed < total and phase_elapsed > 0:
            rate = processed / phase_elapsed
            prog.eta_seconds = (total - processed) / rate if rate > 0 else None
        else:
            prog.eta_seconds = None
        if progress:
            milestone = bool(message) and (not total or processed >= total)
            stage_finished = bool(total) and processed >= total
            if (
                phase_changed
                or milestone
                or stage_finished
                or now - last_callback["t"] >= PROGRESS_CALLBACK_MIN_INTERVAL
            ):
                last_callback["t"] = now
                progress(prog)

    def check_cancelled() -> None:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")

    publish_lock = threading.Lock()

    def publish(kind: GroupKind, member_lists: list[list]) -> int:
        """Build groups for one phase and stream them via on_group. Returns count added.

        Thread-safe: the concurrent similar-image and similar-video stages both
        publish their groups from worker threads.
        """
        added = 0
        with publish_lock:
            for members in member_lists:
                g = build_one_group(
                    kind, members, exact_path_sets=exact_path_sets or None
                )
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

    resolved_roots: list[Path] = []
    root_errors: list[str] = []
    for root in roots:
        resolved = Path(root).expanduser().resolve(strict=False)
        if not resolved.exists():
            root_errors.append(f"scan root does not exist: {resolved}")
        elif is_in_photos_library(resolved):
            root_errors.append(
                "Photos libraries cannot be scanned directly; export media from "
                f"Photos.app to a normal folder first: {resolved}"
            )
        else:
            resolved_roots.append(resolved)

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
        cancelled=cancelled,
    )
    stage_durations["inventory"] = time.monotonic() - inventory_started
    prog.files_found = len(records)
    prog.bytes_scanned = sum(r.size for r in records)
    emit("inventory", len(records), len(records), f"Found {len(records)} media files")

    cache: HashCache | None = None
    if use_cache:
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

    run_exact = exact and bool(records)
    run_similar = similar
    run_dimensions = find_low_resolution and bool(records)
    run_human = find_no_humans and bool(records)
    run_faces = count_faces and bool(records)
    run_any_stage = (
        run_exact
        or run_similar
        or run_dimensions
        or run_human
        or run_faces
        or _build_review_groups
    )

    if run_any_stage:
        check_cancelled()
        distinct_pairs = (
            cache.distinct_pairs(records)
            if (run_similar and cache is not None)
            else set()
        )
        image_count = len(
            [r for r in records if r.media_type in (MediaType.IMAGE, MediaType.GIF)]
        )
        video_count = len([r for r in records if r.media_type == MediaType.VIDEO])
        face_candidates = [
            record for record in records if record.media_type in FACE_MEDIA_TYPES
        ]
        normalized_human_backend = human_backend.strip().lower()

        # Every enabled check runs concurrently on one stage pool. Exact
        # hashing is disk-bound, video fingerprinting is ffmpeg-subprocess-
        # bound, and Photon person inference is single-threaded, so they
        # overlap the CPU-bound stages nearly for free. Image hashing, OpenCV
        # person detection, and face counting are all image-decode + CPU
        # work, so they split one worker budget instead of stacking their
        # private caps (split_cpu_budget). Stages write disjoint record
        # fields (a same-record ``error`` write can race, which is a benign
        # last-writer-wins, same as sequential overwrites). Ordering that
        # affects results is enforced with events, never by serializing the
        # expensive analysis:
        #   - similar groups publish after exact groups exist because
        #     build_one_group consults exact_path_sets,
        #   - dimension probing waits for image/video hashing, which fills
        #     most dimensions as a side effect,
        #   - when faces are also counted, Non-Human groups publish only
        #     after face counts land (a counted face vetoes membership).
        # Progress from all stages is merged into one "processing" phase so
        # combined counts/ETA stay meaningful.
        cpu_stage_count = sum(
            (
                1 if run_similar and image_count else 0,
                1 if run_human and normalized_human_backend == "opencv" else 0,
                1 if run_faces else 0,
            )
        )
        cpu_workers = split_cpu_budget(n_workers, cpu_stage_count)
        human_workers = (
            cpu_workers if normalized_human_backend == "opencv" else n_workers
        )

        stage_lock = threading.Lock()
        stage_text: dict[str, str] = {}
        stage_counts: dict[str, tuple[int, int]] = {}
        exact_done = threading.Event()
        image_done = threading.Event()
        video_done = threading.Event()
        dims_done = threading.Event()
        human_done = threading.Event()

        def _emit_stages_locked() -> None:
            processed = sum(done for done, _total in stage_counts.values())
            total = sum(total for _done, total in stage_counts.values())
            message = " · ".join(
                text
                for text in (
                    stage_text.get("exact"),
                    stage_text.get("image"),
                    stage_text.get("video"),
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
                    stage_counts[stage] = (processed, total)
                _emit_stages_locked()

        def publish_prebuilt(new_groups: list[DuplicateGroup]) -> None:
            """Append already-built review groups and stream them (thread-safe)."""
            if not new_groups:
                return
            with publish_lock:
                for group in new_groups:
                    groups.append(group)
                    if on_group:
                        on_group(group)
                groups.sort(key=lambda x: x.reclaimable_bytes, reverse=True)

        with stage_lock:
            if run_exact:
                stage_text["exact"] = "Finding exact duplicates…"
                stage_counts["exact"] = (0, len(records))
            if run_similar and image_count:
                stage_text["image"] = f"Hashing {image_count} images…"
                stage_counts["image"] = (0, image_count)
            if run_similar and video_count:
                stage_text["video"] = f"Fingerprinting {video_count} videos…"
                stage_counts["video"] = (0, video_count)
            if run_dimensions:
                stage_text["dims"] = "Reading media dimensions…"
            if run_human:
                stage_text["human"] = (
                    f"Looking for media without people ({human_backend})…"
                )
                stage_counts["human"] = (0, len(records))
            if run_faces:
                stage_text["face"] = "Counting faces (OpenCV)…"
                stage_counts["face"] = (0, len(face_candidates))
            _emit_stages_locked()

        def exact_progress(phase: str, processed: int, total: int) -> None:
            _stage_progress(
                "exact", f"Exact hash {processed}/{total}", processed, total
            )

        def img_progress(phase: str, processed: int, total: int) -> None:
            if "hash" in phase:
                label = "hashing"
            elif "cluster" in phase:
                label = "clustering"
            else:
                label = phase.replace("-", " ")
            _stage_progress(
                "image", f"Images {label}: {processed}/{total}", processed, total
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
            _stage_progress("video", text, processed, total)

        def _exact_stage() -> float:
            started_at = time.monotonic()
            try:
                exact_member_lists = find_exact_groups(
                    records,
                    progress=exact_progress,
                    workers=n_workers,
                    cancelled=cancelled,
                )
                count = publish(GroupKind.EXACT, exact_member_lists)
                _stage_progress(
                    "exact",
                    f"Found {count} exact group{'s' if count != 1 else ''}",
                    0,
                    0,
                )
                return time.monotonic() - started_at
            finally:
                # Always unblock similar publishing, even on cancel/error.
                exact_done.set()

        def _image_stage() -> float:
            started_at = time.monotonic()
            try:
                img_groups = find_similar_image_groups(
                    records,
                    threshold=image_threshold,
                    distinct_pairs=distinct_pairs,
                    progress=img_progress,
                    workers=cpu_workers,
                    cancelled=cancelled,
                )
                exact_done.wait()
                count = publish(GroupKind.SIMILAR, img_groups)
                _stage_progress(
                    "image",
                    f"Found {count} similar image group{'s' if count != 1 else ''}",
                    0,
                    0,
                )
                return time.monotonic() - started_at
            finally:
                # Always unblock dimension probing, even on cancel/error.
                image_done.set()

        def _video_stage() -> float:
            started_at = time.monotonic()
            try:
                vid_groups = find_similar_video_groups(
                    records,
                    threshold=video_threshold,
                    distinct_pairs=distinct_pairs,
                    progress=vid_progress,
                    workers=n_workers,
                    cancelled=cancelled,
                )
                exact_done.wait()
                count = publish(GroupKind.SIMILAR, vid_groups)
                _stage_progress(
                    "video",
                    f"Found {count} similar video group{'s' if count != 1 else ''}",
                    0,
                    0,
                )
                return time.monotonic() - started_at
            finally:
                # Always unblock dimension probing, even on cancel/error.
                video_done.set()

        def _dimensions_stage() -> float:
            # Image/video hashing fills most dimensions as a side effect, so
            # probe only the leftovers once those stages are done. The waits
            # are excluded from the recorded duration.
            try:
                image_done.wait()
                video_done.wait()
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
                    records,
                    media_types=set(low_resolution_bounds),
                    workers=n_workers,
                    cancelled=cancelled,
                    progress=resolution_progress,
                )
                _stage_progress("dims", "Dimensions ready", 0, 0)
                return time.monotonic() - started_at
            finally:
                # Always unblock review-group publishing, even on cancel/error.
                dims_done.set()

        def _review_stage() -> float:
            # Low-resolution membership needs dimensions; random review does
            # not, but both publish together as one cheap step.
            dims_done.wait()
            check_cancelled()
            started_at = time.monotonic()
            review_groups: list[DuplicateGroup] = []
            if find_low_resolution:
                review_groups.extend(
                    build_low_resolution_groups(
                        records,
                        max_pixels=max(1, int(low_resolution_max_pixels)),
                        skip_paths=kept_paths(records),
                        media_types=set(low_resolution_bounds),
                        max_pixels_by_media_type=low_resolution_bounds,
                    )
                )
            review_groups.extend(
                build_random_review_groups(
                    records, count=max(0, int(random_review_count))
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
                    records,
                    backend=human_backend,
                    photon_model=photon_model,
                    workers=human_workers,
                    progress=human_progress,
                    cancelled=cancelled,
                    # When faces are also being counted, the face stage
                    # supplies the counts and a counted face vetoes membership
                    # at group build time, so the in-stage YuNet confirmation
                    # pass would only duplicate that work.
                    confirm_with_faces=not run_faces,
                )
                if run_faces:
                    # Confirmed Non-Human groups publish from the face stage
                    # once face counts land.
                    _stage_progress(
                        "human",
                        "Person detection finished; face counts will confirm "
                        "Non-Human candidates",
                        0,
                        0,
                    )
                else:
                    publish_prebuilt(build_no_human_groups(no_human_files))
                    _stage_progress(
                        "human",
                        f"Found {len(no_human_files)} file"
                        f"{'' if len(no_human_files) == 1 else 's'} "
                        "without detected people",
                        0,
                        0,
                    )
                return time.monotonic() - started_at
            finally:
                # Always unblock Non-Human publishing in the face stage.
                human_done.set()

        def _face_stage() -> float:
            started_at = time.monotonic()

            def face_progress(phase: str, processed: int, total: int) -> None:
                _stage_progress(
                    "face",
                    f"Face counting {processed}/{total}",
                    processed,
                    total,
                )

            count_faces_in_files(
                records,
                workers=cpu_workers,
                progress=face_progress,
                cancelled=cancelled,
            )
            files_with_faces = sum(
                1 for record in face_candidates if (record.face_count or 0) > 0
            )
            _stage_progress(
                "face",
                f"Found faces in {files_with_faces} "
                f"file{'s' if files_with_faces != 1 else ''}",
                0,
                0,
            )
            duration = time.monotonic() - started_at
            publish_prebuilt(build_faces_groups(face_candidates))
            if find_no_humans:
                # A counted face vetoes Non-Human membership, so confirmed
                # groups publish only after person detection lands too.
                human_done.wait()
                confirmed = build_no_human_groups(records)
                confirmed_count = sum(len(group.members) for group in confirmed)
                publish_prebuilt(confirmed)
                _stage_progress(
                    "human",
                    f"Found {confirmed_count} file"
                    f"{'' if confirmed_count == 1 else 's'} "
                    "without detected people",
                    0,
                    0,
                )
            return duration

        stage_jobs: dict[str, Callable[[], float]] = {}
        if run_exact:
            stage_jobs["exact"] = _exact_stage
        else:
            exact_done.set()
        if run_similar:
            stage_jobs["image"] = _image_stage
            stage_jobs["video"] = _video_stage
        else:
            image_done.set()
            video_done.set()
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
        if _build_review_groups:
            stage_jobs["review"] = _review_stage

        stage_results: dict[str, float] = {}
        if stage_jobs:
            with ThreadPoolExecutor(
                max_workers=len(stage_jobs), thread_name_prefix="scan-stage"
            ) as stage_pool:
                futures = {
                    name: stage_pool.submit(fn) for name, fn in stage_jobs.items()
                }
                for name, future in futures.items():
                    stage_results[name] = future.result()

        if run_exact:
            stage_durations["exact"] = stage_results["exact"]
            stage_errors["exact"] = [
                record.error
                for record in records
                if record.error
                and record.error.startswith(
                    (ERROR_PARTIAL_HASH_FAILED, ERROR_SHA256_FAILED)
                )
            ]
        if run_similar:
            stage_durations["similar_image"] = stage_results["image"]
            stage_durations["similar_video"] = stage_results["video"]
            stage_errors["similar_image"] = [
                record.error
                for record in records
                if record.error and record.error.startswith(ERROR_IMAGE_HASH_FAILED)
            ]
            stage_errors["similar_video"] = [
                record.error
                for record in records
                if record.error and record.error.startswith(ERROR_VIDEO_FINGERPRINT_FAILED)
            ]
        if run_dimensions:
            stage_durations["low_resolution"] = stage_results["dims"]
        if run_human:
            stage_durations["human_detection"] = stage_results["human"]
            stage_errors["human_detection"] = [
                record.error or "person analysis failed"
                for record in records
                if record.human_detection_status == "analysis_failed"
            ]
        if run_faces:
            stage_durations["face_detection"] = stage_results["face"]
            stage_errors["face_detection"] = [
                record.error or "face counting failed"
                for record in face_candidates
                if record.face_count is None
            ]

    cache_errors: list[str] = []
    if cache is not None:
        try:
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
    video_failures = stage_errors["similar_video"]
    image_attempted = len(image_records) if similar and len(image_records) >= 2 else 0
    video_dependency = ffmpeg_available()
    video_attempted = (
        len(video_records)
        if similar and len(video_records) >= 2 and video_dependency
        else 0
    )
    human_failures = stage_errors["human_detection"]
    face_failures = stage_errors["face_detection"]
    face_records = [
        record for record in records if record.media_type in FACE_MEDIA_TYPES
    ]
    resolution_records = [
        record
        for record in records
        if record.media_type in (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO)
    ]
    resolution_failures = [
        record for record in resolution_records if not (record.width and record.height)
    ]

    stages = {
        "inventory": StageDiagnostics(
            unit="roots",
            attempted=len(roots),
            succeeded=len(resolved_roots),
            failed=len(root_errors),
            duration_seconds=stage_durations.get("inventory", 0.0),
            warnings=root_errors[:10],
        ),
        "exact": StageDiagnostics(
            attempted=len(exact_candidates) if exact else 0,
            succeeded=(len(exact_candidates) - len(exact_failures)) if exact else 0,
            failed=len(exact_failures) if exact else 0,
            skipped=len(records) - (len(exact_candidates) if exact else 0),
            duration_seconds=stage_durations.get("exact", 0.0),
            warnings=(
                [f"{len(exact_failures)} exact-hash candidate(s) failed"]
                if exact_failures
                else []
            ),
        ),
        "similar_image": StageDiagnostics(
            attempted=image_attempted,
            succeeded=image_attempted - len(image_failures),
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
            succeeded=video_attempted - len(video_failures),
            failed=len(video_failures),
            skipped=len(records) - video_attempted,
            duration_seconds=stage_durations.get("similar_video", 0.0),
            warnings=(
                ["ffmpeg/ffprobe unavailable; eligible videos were not analyzed"]
                if similar and video_records and not video_dependency
                else (
                    [f"{len(video_failures)} video fingerprint(s) failed"]
                    if video_failures
                    else []
                )
            ),
        ),
        "human_detection": StageDiagnostics(
            attempted=len(records) if find_no_humans else 0,
            succeeded=(len(records) - len(human_failures)) if find_no_humans else 0,
            failed=len(human_failures) if find_no_humans else 0,
            skipped=0 if find_no_humans else len(records),
            duration_seconds=stage_durations.get("human_detection", 0.0),
            warnings=(
                [f"{len(human_failures)} file(s) could not be analyzed for people"]
                if human_failures
                else []
            ),
        ),
        "face_detection": StageDiagnostics(
            attempted=len(face_records) if count_faces else 0,
            succeeded=(len(face_records) - len(face_failures)) if count_faces else 0,
            failed=len(face_failures) if count_faces else 0,
            skipped=len(records) - (len(face_records) if count_faces else 0),
            duration_seconds=stage_durations.get("face_detection", 0.0),
            warnings=(
                [f"{len(face_failures)} file(s) could not be analyzed for faces"]
                if face_failures
                else []
            ),
        ),
        "low_resolution": StageDiagnostics(
            attempted=len(resolution_records) if find_low_resolution else 0,
            succeeded=(len(resolution_records) - len(resolution_failures))
            if find_low_resolution
            else 0,
            failed=len(resolution_failures) if find_low_resolution else 0,
            skipped=0 if find_low_resolution else len(resolution_records),
            duration_seconds=stage_durations.get("low_resolution", 0.0),
            warnings=(
                [
                    (f"{len(resolution_failures)} file(s) could not be checked for "
                    "low resolution")
                ]
                if find_low_resolution and resolution_failures
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
    recorded_errors = list(dict.fromkeys(
        [*root_errors]
        + [error for errors in stage_errors.values() for error in errors]
        + cache_errors
        + [record.error for record in records if record.error]
    ))
    result = ScanResult(
        roots=[str(root) for root in resolved_roots],
        files=records,
        groups=groups,
        errors=recorded_errors,
        diagnostics=ScanDiagnostics(
            total_duration_seconds=total_duration,
            cache_hits=cache_hits,
            stages=stages,
        ),
    )
    result.recompute_stats()

    prog.done = True
    prog.phase = "done"
    prog.groups_found = len(groups)
    prog.message = (
        f"Done — {result.exact_groups} exact, {result.similar_groups} similar groups, "
        f"{result.low_resolution_files} low-resolution, "
        f"{result.random_review_files} random review, {result.no_human_files} non-human"
        + (f", {result.faces_files} faces" if count_faces else " ")
        + f" ({len(records)} files)"
    )
    prog.elapsed_seconds = total_duration
    prog.eta_seconds = 0.0
    if progress:
        progress(prog)

    return result


def _resolve_stream_roots(roots: list[str | Path]) -> tuple[list[Path], list[str]]:
    """De-duplicate and resolve scan roots, collecting errors for missing paths."""
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
        resolved.append(path)
    return resolved, errors


def run_scans_parallel(
    roots: list[str | Path],
    *,
    max_streams: int | None = None,
    exact: bool = True,
    similar: bool = True,
    find_no_humans: bool = False,
    count_faces: bool = False,
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
) -> ScanResult:
    """Scan each root as an independent, concurrent stream.

    Unlike :func:`run_scan` — which merges every root into one pool and finds
    duplicates *across* folders — this runs a separate full pipeline per folder
    at the same time. There is no cross-folder deduplication: a group only ever
    contains files from a single root, and every streamed group carries its
    source ``root``.

    Callbacks:
    - ``on_stream_progress`` fires with each folder's own ``ScanProgress``
      (its ``stream_index`` and ``root`` are set) so a UI can show one progress
      indicator per folder.
    - ``progress`` fires with an aggregate ``ScanProgress`` across all streams.
    - ``on_group`` fires as each group is finalized, tagged with its ``root``.
    """
    resolved_roots, root_errors = _resolve_stream_roots(roots)
    if not resolved_roots:
        result = ScanResult(roots=[], files=[], groups=[], errors=root_errors)
        result.recompute_stats()
        if progress:
            done = ScanProgress(
                phase="done", done=True, message="No valid folders to scan"
            )
            progress(done)
        return result

    # Every folder is scanned (nothing is dropped); max_streams only throttles
    # how many run at once. The worker budget is divided across the folders
    # sharing it — not across the CPU-derived throttle — so per-stream counts
    # never depend on machine core count.
    n_streams = len(resolved_roots)
    max_concurrent = min(n_streams, resolve_workers(max_streams))
    budget_streams = max_concurrent if max_streams is not None else n_streams
    # Divide the worker budget across streams (rounding up, floor of 2): each
    # stream splits its budget again between concurrent CPU stages, and a
    # double division down to 1 worker serializes image hashing/face counting.
    per_stream_workers = max(2, -(-resolve_workers(workers) // budget_streams))

    started = time.monotonic()
    lock = threading.RLock()
    # Latest progress per stream, keyed by index, for aggregate reporting.
    stream_progress: dict[int, ScanProgress] = {}
    all_files: list = []
    all_groups: list[DuplicateGroup] = []
    stream_errors: list[str] = []

    def emit_aggregate() -> None:
        if progress is None:
            return
        files_found = sum(p.files_found for p in stream_progress.values())
        files_processed = sum(p.files_processed for p in stream_progress.values())
        groups_found = sum(p.groups_found for p in stream_progress.values())
        bytes_scanned = sum(p.bytes_scanned for p in stream_progress.values())
        done_count = sum(1 for p in stream_progress.values() if p.done)
        all_done = done_count == n_streams
        agg = ScanProgress(
            phase="done" if all_done else "scanning",
            files_found=files_found,
            files_processed=files_processed,
            groups_found=groups_found,
            bytes_scanned=bytes_scanned,
            done=all_done,
            elapsed_seconds=max(0.0, time.monotonic() - started),
            message=(
                f"Scanning {n_streams} folder{'s' if n_streams != 1 else ''} "
                f"in parallel — {done_count}/{n_streams} done"
                if not all_done
                else f"Done — {done_count}/{n_streams} folders, {groups_found} groups"
            ),
        )
        progress(agg)

    def scan_one(index: int, root: Path) -> ScanResult:
        def stream_progress_cb(prog: ScanProgress) -> None:
            with lock:
                prog.stream_index = index
                prog.root = str(root)
                stream_progress[index] = prog
                if on_stream_progress:
                    on_stream_progress(prog)
                emit_aggregate()

        def stream_group_cb(group: DuplicateGroup) -> None:
            with lock:
                group.root = str(root)
                if on_group:
                    on_group(group)

        return run_scan(
            [root],
            exact=exact,
            similar=similar,
            find_no_humans=find_no_humans,
            count_faces=count_faces,
            find_low_resolution=find_low_resolution,
            low_resolution_images=low_resolution_images,
            low_resolution_gifs=low_resolution_gifs,
            low_resolution_videos=low_resolution_videos,
            low_resolution_max_pixels=low_resolution_max_pixels,
            low_resolution_image_max_pixels=low_resolution_image_max_pixels,
            low_resolution_gif_max_pixels=low_resolution_gif_max_pixels,
            low_resolution_video_max_pixels=low_resolution_video_max_pixels,
            random_review_count=random_review_count,
            human_backend=human_backend,
            photon_model=photon_model,
            include_images=include_images,
            include_gifs=include_gifs,
            include_videos=include_videos,
            include_hidden=include_hidden,
            image_threshold=image_threshold,
            video_threshold=video_threshold,
            use_cache=use_cache,
            cache_path=cache_path,
            workers=per_stream_workers,
            exclusions=exclusions,
            cancelled=cancelled,
            progress=stream_progress_cb,
            on_group=stream_group_cb,
            _build_review_groups=False,
        )

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
                except Exception as exc:
                    stream_errors.append(f"{root}: {exc}")
                    continue
                with lock:
                    for group in sub.groups:
                        group.root = str(root)
                    all_files.extend(sub.files)
                    all_groups.extend(sub.groups)
                    stream_errors.extend(sub.errors)

    review_groups: list[DuplicateGroup] = []
    if find_low_resolution:
        low_resolution_bounds = _low_resolution_bounds(
            default_max_pixels=low_resolution_max_pixels,
            images=low_resolution_images,
            gifs=low_resolution_gifs,
            videos=low_resolution_videos,
            image_max_pixels=low_resolution_image_max_pixels,
            gif_max_pixels=low_resolution_gif_max_pixels,
            video_max_pixels=low_resolution_video_max_pixels,
        )
        review_groups.extend(
            build_low_resolution_groups(
                all_files,
                max_pixels=max(1, int(low_resolution_max_pixels)),
                skip_paths=kept_paths(all_files),
                media_types=set(low_resolution_bounds),
                max_pixels_by_media_type=low_resolution_bounds,
            )
        )
    review_groups.extend(
        build_random_review_groups(all_files, count=max(0, int(random_review_count)))
    )
    all_groups.extend(review_groups)
    if on_group:
        for group in review_groups:
            on_group(group)
    all_groups.sort(key=lambda g: g.reclaimable_bytes, reverse=True)
    result = ScanResult(
        roots=[str(root) for root in resolved_roots],
        files=all_files,
        groups=all_groups,
        errors=[*root_errors, *stream_errors],
    )
    result.recompute_stats()

    if progress:
        final = ScanProgress(
            phase="cancelled" if interrupted else "done",
            done=True,
            files_found=len(all_files),
            groups_found=len(all_groups),
            elapsed_seconds=max(0.0, time.monotonic() - started),
            message=(
                "Scan cancelled"
                if interrupted
                else (
                    f"Done — {result.exact_groups} exact, "
                    f"{result.similar_groups} similar groups, "
                    f"{result.low_resolution_files} low-resolution, "
                    f"{result.random_review_files} random review, "
                    f"{result.no_human_files} non-human, "
                    f"{result.faces_files} faces "
                    f"across {n_streams} folder{'s' if n_streams != 1 else ''}"
                )
            ),
        )
        progress(final)

    if interrupted:
        raise InterruptedError("scan cancelled")

    return result
