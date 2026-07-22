"""Orchestrates full scan: inventory → exact → similar → groups."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import time

from .cache import HashCache
from .exact import find_exact_groups
from .grouping import build_no_human_groups, build_one_group
from .human_detection import (
    DEFAULT_BACKEND as DEFAULT_HUMAN_BACKEND,
    DEFAULT_PHOTON_MODEL,
    find_no_human_files,
)
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
from .similar_image import find_similar_image_groups
from .similar_video import DEFAULT_THRESHOLD as VID_THRESHOLD
from .similar_video import ffmpeg_available, find_similar_video_groups

ProgressCb = Callable[[ScanProgress], None]
GroupCb = Callable[[DuplicateGroup], None]
StreamProgressCb = Callable[[ScanProgress], None]


def run_scan(
    roots: list[str | Path],
    *,
    exact: bool = True,
    similar: bool = True,
    find_no_humans: bool = False,
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
) -> ScanResult:
    """Run a full scan.

    ``on_group`` is called as soon as each duplicate group is finalized (exact
    first, then similar-image, then similar-video) so UIs can stream results
    instead of waiting for the whole scan.
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

    if exact and records:
        exact_started = time.monotonic()
        check_cancelled()
        emit("exact", 0, len(records), "Finding exact duplicates…")

        def exact_progress(phase: str, processed: int, total: int) -> None:
            emit(phase, processed, total, f"Exact hash {processed}/{total}")

        exact_member_lists = find_exact_groups(
            records,
            progress=exact_progress,
            workers=n_workers,
            cancelled=cancelled,
        )
        n_exact = publish(GroupKind.EXACT, exact_member_lists)
        emit(
            "exact",
            len(records),
            len(records),
            f"Found {n_exact} exact group{'s' if n_exact != 1 else ''}",
        )
        stage_durations["exact"] = time.monotonic() - exact_started
        stage_errors["exact"] = [
            record.error
            for record in records
            if record.error
            and record.error.startswith(("partial hash failed", "sha256 failed"))
        ]

    if similar:
        check_cancelled()
        distinct_pairs = cache.distinct_pairs(records) if cache is not None else set()
        emit("similar-image", 0, 0, "Hashing images for similarity…")

        def img_progress(phase: str, processed: int, total: int) -> None:
            emit(phase, processed, total, f"Images {phase}: {processed}/{total}")

        image_started = time.monotonic()
        img_groups = find_similar_image_groups(
            records,
            threshold=image_threshold,
            distinct_pairs=distinct_pairs,
            progress=img_progress,
            workers=n_workers,
            cancelled=cancelled,
        )
        n_img = publish(GroupKind.SIMILAR, img_groups)
        emit(
            "similar-image",
            0,
            0,
            f"Found {n_img} similar image group{'s' if n_img != 1 else ''}",
        )
        stage_durations["similar_image"] = time.monotonic() - image_started
        stage_errors["similar_image"] = [
            record.error
            for record in records
            if record.error and record.error.startswith("image hash failed")
        ]

        emit("similar-video", 0, 0, "Fingerprinting videos…")

        def vid_progress(phase: str, processed: int, total: int) -> None:
            emit(phase, processed, total, f"Videos {phase}: {processed}/{total}")

        video_started = time.monotonic()
        vid_groups = find_similar_video_groups(
            records,
            threshold=video_threshold,
            distinct_pairs=distinct_pairs,
            progress=vid_progress,
            workers=n_workers,
            cancelled=cancelled,
        )
        n_vid = publish(GroupKind.SIMILAR, vid_groups)
        emit(
            "similar-video",
            0,
            0,
            f"Found {n_vid} similar video group{'s' if n_vid != 1 else ''}",
        )
        stage_durations["similar_video"] = time.monotonic() - video_started
        stage_errors["similar_video"] = [
            record.error
            for record in records
            if record.error and record.error.startswith("video fingerprint failed")
        ]

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

    if cache is not None:
        try:
            cache.store_all(records)
            cache.close()
        except Exception:
            pass

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
    }
    total_duration = max(0.0, time.monotonic() - started)
    recorded_errors = list(dict.fromkeys(
        [*root_errors]
        + [error for errors in stage_errors.values() for error in errors]
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
        f"{result.no_human_files} non-human "
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
                    f"{result.no_human_files} non-human "
                    f"across {n_streams} folder{'s' if n_streams != 1 else ''}"
                )
            ),
        )
        progress(final)

    if interrupted:
        raise InterruptedError("scan cancelled")

    return result
