"""Orchestrates full scan: inventory → exact → similar → groups."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import time

from .cache import HashCache
from .exact import find_exact_groups
from .grouping import (
    DEFAULT_RANDOM_REVIEW_COUNT,
    LOW_RESOLUTION_MAX_PIXELS,
    build_low_resolution_groups,
    build_no_human_groups,
    build_one_group,
    build_random_review_groups,
)
from .human_detection import (
    DEFAULT_BACKEND as DEFAULT_HUMAN_BACKEND,
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
from .parallel import resolve_workers
from .scanner import inventory, is_in_photos_library
from .similar_image import DEFAULT_THRESHOLD as IMG_THRESHOLD
from .similar_image import find_similar_image_groups, probe_image_dimensions
from .similar_video import DEFAULT_THRESHOLD as VID_THRESHOLD
from .similar_video import ffmpeg_available, find_similar_video_groups, probe_video

ProgressCb = Callable[[ScanProgress], None]
GroupCb = Callable[[DuplicateGroup], None]
StreamProgressCb = Callable[[ScanProgress], None]


def _populate_missing_dimensions(
    records: list,
    *,
    workers: int,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[str]:
    """Populate dimensions needed by low-resolution review without full hashing."""
    missing = [
        record
        for record in records
        if record.media_type in (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO)
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
        except Exception as exc:  # noqa: BLE001 - report per-file media failures
            return f"resolution probe failed for {record.path}: {exc}"

    errors: list[str] = []
    with ThreadPoolExecutor(
        max_workers=max(1, min(workers, len(missing))),
        thread_name_prefix="media-dimensions",
    ) as pool:
        for done, error in enumerate(pool.map(probe, missing), start=1):
            if error:
                errors.append(error)
            if progress:
                progress(done, len(missing))
    return errors


def run_scan(
    roots: list[str | Path],
    *,
    exact: bool = True,
    similar: bool = True,
    find_no_humans: bool = False,
    find_low_resolution: bool = True,
    low_resolution_max_pixels: int = LOW_RESOLUTION_MAX_PIXELS,
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

    Exact hashing, image/GIF similarity, and video similarity all run
    concurrently. ``on_group`` is called as soon as each duplicate group is
    finalized so UIs can stream results instead of waiting for the whole scan;
    exact groups are always published before similar groups because similar
    grouping consults the exact-group membership.
    """
    n_workers = resolve_workers(workers)
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
        "low_resolution": [],
    }
    cache_hits = 0

    def emit(phase: str, processed: int = 0, total: int = 0, message: str = "") -> None:
        nonlocal phase_started, previous_phase
        now = time.monotonic()
        if phase != previous_phase:
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

    if run_exact or run_similar:
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

        # Exact hashing (disk-bound), image/GIF hashing (CPU-bound), and video
        # fingerprinting (ffmpeg subprocess-bound) all run concurrently. Images
        # /GIFs and videos are disjoint file sets that can never share a
        # duplicate group, and exact hashing touches different record fields
        # than the similarity stages (a same-record ``error`` write can race,
        # which is a benign last-writer-wins, same as sequential overwrites).
        # Similar groups are only *published* after exact groups exist because
        # build_one_group consults exact_path_sets; the expensive hashing work
        # itself never waits. Progress from all stages is merged into one
        # "processing" phase so combined counts/ETA stay meaningful.
        stage_lock = threading.Lock()
        stage_text: dict[str, str] = {}
        stage_counts: dict[str, tuple[int, int]] = {}
        exact_done = threading.Event()

        def _emit_stages_locked() -> None:
            processed = sum(done for done, _total in stage_counts.values())
            total = sum(total for _done, total in stage_counts.values())
            message = " · ".join(
                text
                for text in (
                    stage_text.get("exact"),
                    stage_text.get("image"),
                    stage_text.get("video"),
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

        def _exact_stage() -> tuple[int, float]:
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
                return count, time.monotonic() - started_at
            finally:
                # Always unblock similar publishing, even on cancel/error.
                exact_done.set()

        def _image_stage() -> tuple[int, float]:
            started_at = time.monotonic()
            img_groups = find_similar_image_groups(
                records,
                threshold=image_threshold,
                distinct_pairs=distinct_pairs,
                progress=img_progress,
                workers=n_workers,
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
            return count, time.monotonic() - started_at

        def _video_stage() -> tuple[int, float]:
            started_at = time.monotonic()
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
            return count, time.monotonic() - started_at

        stage_jobs: dict[str, Callable[[], tuple[int, float]]] = {}
        if run_exact:
            stage_jobs["exact"] = _exact_stage
        else:
            exact_done.set()
        if run_similar:
            stage_jobs["image"] = _image_stage
            stage_jobs["video"] = _video_stage

        stage_results: dict[str, tuple[int, float]] = {}
        with ThreadPoolExecutor(
            max_workers=len(stage_jobs), thread_name_prefix="scan-stage"
        ) as stage_pool:
            futures = {
                name: stage_pool.submit(fn) for name, fn in stage_jobs.items()
            }
            for name, future in futures.items():
                stage_results[name] = future.result()

        if run_exact:
            stage_durations["exact"] = stage_results["exact"][1]
            stage_errors["exact"] = [
                record.error
                for record in records
                if record.error
                and record.error.startswith(("partial hash failed", "sha256 failed"))
            ]
        if run_similar:
            stage_durations["similar_image"] = stage_results["image"][1]
            stage_durations["similar_video"] = stage_results["video"][1]
            stage_errors["similar_image"] = [
                record.error
                for record in records
                if record.error and record.error.startswith("image hash failed")
            ]
            stage_errors["similar_video"] = [
                record.error
                for record in records
                if record.error and record.error.startswith("video fingerprint failed")
            ]

    if find_low_resolution and records:
        resolution_started = time.monotonic()
        check_cancelled()
        eligible = [
            record
            for record in records
            if record.media_type in (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO)
        ]
        emit(
            "low-resolution",
            0,
            len(eligible),
            "Reading media dimensions for low-resolution suggestions…",
        )

        def resolution_progress(processed: int, total: int) -> None:
            emit(
                "low-resolution",
                processed,
                total,
                f"Reading dimensions {processed}/{total}",
            )

        stage_errors["low_resolution"] = _populate_missing_dimensions(
            records,
            workers=n_workers,
            cancelled=cancelled,
            progress=resolution_progress,
        )
        stage_durations["low_resolution"] = time.monotonic() - resolution_started

    if _build_review_groups:
        review_groups: list[DuplicateGroup] = []
        if find_low_resolution:
            review_groups.extend(
                build_low_resolution_groups(
                    records,
                    max_pixels=max(1, int(low_resolution_max_pixels)),
                    skip_paths=kept_paths(records),
                )
            )
        review_groups.extend(
            build_random_review_groups(records, count=max(0, int(random_review_count)))
        )
        for group in review_groups:
            groups.append(group)
            if on_group:
                on_group(group)
        groups.sort(key=lambda x: x.reclaimable_bytes, reverse=True)

    if find_no_humans and records:
        human_started = time.monotonic()
        check_cancelled()
        emit(
            "human-detection",
            0,
            len(records),
            f"Looking for media without people ({human_backend})…",
        )

        def human_progress(phase: str, processed: int, total: int) -> None:
            emit(phase, processed, total, f"Person detection {processed}/{total}")

        no_human_files = find_no_human_files(
            records,
            backend=human_backend,
            photon_model=photon_model,
            workers=n_workers,
            progress=human_progress,
            cancelled=cancelled,
        )
        for group in build_no_human_groups(no_human_files):
            groups.append(group)
            if on_group:
                on_group(group)
        groups.sort(key=lambda x: x.reclaimable_bytes, reverse=True)
        emit(
            "human-detection",
            len(records),
            len(records),
            f"Found {len(no_human_files)} file{'s' if len(no_human_files) != 1 else ''} without detected people",
        )
        stage_durations["human_detection"] = time.monotonic() - human_started
        stage_errors["human_detection"] = [
            record.error or "person analysis failed"
            for record in records
            if record.human_detection_status == "analysis_failed"
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
                    f"{len(resolution_failures)} file(s) could not be checked for "
                    "low resolution"
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
        f"{result.random_review_files} random review, {result.no_human_files} non-human "
        f"({len(records)} files)"
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
    find_low_resolution: bool = True,
    low_resolution_max_pixels: int = LOW_RESOLUTION_MAX_PIXELS,
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

    n_streams = min(len(resolved_roots), resolve_workers(max_streams))
    per_stream_workers = max(1, resolve_workers(workers) // n_streams)

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
            find_low_resolution=find_low_resolution,
            low_resolution_max_pixels=low_resolution_max_pixels,
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
    with ThreadPoolExecutor(max_workers=n_streams) as ex:
        futures = {
            ex.submit(scan_one, i, root): (i, root)
            for i, root in enumerate(resolved_roots)
        }
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in finished:
                i, root = futures[fut]
                try:
                    sub = fut.result()
                except InterruptedError:
                    interrupted = True
                except Exception as exc:  # noqa: BLE001 - surface per-folder failures
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
        review_groups.extend(
            build_low_resolution_groups(
                all_files,
                max_pixels=max(1, int(low_resolution_max_pixels)),
                skip_paths=kept_paths(all_files),
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
                    f"{result.no_human_files} non-human "
                    f"across {n_streams} folder{'s' if n_streams != 1 else ''}"
                )
            ),
        )
        progress(final)

    if interrupted:
        raise InterruptedError("scan cancelled")

    return result
