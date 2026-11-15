"""Near-duplicate video detection via ffmpeg frame sampling + perceptual hashing.

Resource-conscious: at most a few concurrent ffmpeg jobs, each limited to a
single decoder thread, with one-pass frame extraction and small frame size.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

from .cache import DistinctReviews
from .exact import PARTIAL_SIZE, file_partial_hash, file_sha256
from .grouping import cluster_around_best
from .models import FileRecord, MediaType
from .parallel import (
    DEFAULT_VIDEO_WORKERS_CAP,
    DEFAULT_WORKERS_CAP,
    map_parallel,
    resolve_workers,
)

T = TypeVar("T")
R = TypeVar("R")

ProgressCb = Callable[[str, int, int, str | None], None]

DEFAULT_THRESHOLD = 8  # Mean Hamming distance across aligned 64-bit frame hashes
# Pairs up to this multiple of the threshold get a second, offset-aligned
# frame check instead of rejection. Unrelated clips score about 22–34.
VERIFY_DISTANCE_FACTOR = 2

# Error-kind prefix; engine diagnostics classify on it.
ERROR_VIDEO_FINGERPRINT_FAILED = "video fingerprint failed"
MAX_FRAMES = 8  # enough signal; fewer seeks/decodes than 12
FRAME_WIDTH = 320
HASH_FRAME_SIZE = 32
# Software-decode fingerprint jobs run beside the hardware-decode ones: one
# per this many spare cores, up to the automatic maximum.
CORES_PER_SOFTWARE_DECODE = 2
MAX_SOFTWARE_DECODE_WORKERS = 6
HASH_FRAME_FILTER = f"scale={HASH_FRAME_SIZE}:{HASH_FRAME_SIZE}:flags=lanczos,format=gray"

_DECODE_LANE = threading.local()
_THREAD_SCRATCH = threading.local()
_SCRATCH_DIRS: set[Path] = set()
_SCRATCH_LOCK = threading.Lock()


@atexit.register
def _cleanup_scratch_dirs() -> None:
    with _SCRATCH_LOCK:
        dirs = list(_SCRATCH_DIRS)
        _SCRATCH_DIRS.clear()
    for directory in dirs:
        shutil.rmtree(directory, ignore_errors=True)


def _thread_scratch_dir() -> Path:
    """Return a lazily-created per-thread scratch directory cleaned up on exit."""
    scratch = getattr(_THREAD_SCRATCH, "dir", None)
    if scratch is None or not scratch.exists():
        scratch = Path(tempfile.mkdtemp(prefix="dedupe-vidhash-"))
        _THREAD_SCRATCH.dir = scratch
        with _SCRATCH_LOCK:
            _SCRATCH_DIRS.add(scratch)
    return scratch


def _next_scratch_seq() -> int:
    seq = getattr(_THREAD_SCRATCH, "seq", 0) + 1
    _THREAD_SCRATCH.seq = seq
    return seq


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# The hwaccel capability probe must bypass any test-patched subprocess.run so
# fake runners never see (or answer) the one-time "-hwaccels" query.
_REAL_SUBPROCESS_RUN = subprocess.run


@lru_cache(maxsize=1)
def _hwaccel_args() -> tuple[str, ...]:
    """Input-side ffmpeg args enabling hardware decode, or () when unsupported.

    On macOS, VideoToolbox offloads H.264/HEVC decoding to dedicated silicon —
    much faster on high-resolution sources and it frees CPU for the image
    hashing that now runs concurrently. H.264/HEVC decoder output is spec-exact,
    so fingerprints match software-decoded values. Every caller retries without
    these args on failure, and DEDUPE_DISABLE_HWACCEL=1 forces software decode.
    """
    if os.environ.get("DEDUPE_DISABLE_HWACCEL"):
        return ()
    if sys.platform != "darwin" or not shutil.which("ffmpeg"):
        return ()
    try:
        result = _REAL_SUBPROCESS_RUN(
            ["ffmpeg", "-hide_banner", "-hwaccels"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and "videotoolbox" in (result.stdout or ""):
            return ("-hwaccel", "videotoolbox")
    except Exception:
        pass
    return ()


def probe_video(path: str | Path) -> tuple[float | None, int | None, int | None]:
    """Single ffprobe: (duration, width, height)."""
    if not shutil.which("ffprobe"):
        return None, None, None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "format=duration:stream=duration,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            return None, None, None
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams") or []
        stream = streams[0] if streams else {}
        # Container duration includes audio. A longer soundtrack must not make
        # us seek beyond the end of an otherwise readable video stream.
        duration = None
        for raw in (stream.get("duration"), (data.get("format") or {}).get("duration")):
            try:
                candidate = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(candidate) and candidate > 0:
                duration = candidate
                break
        w = h = None
        if streams:
            w = streams[0].get("width")
            h = streams[0].get("height")
            w = int(w) if w else None
            h = int(h) if h else None
        return duration, w, h
    except Exception:
        return None, None, None


def probe_duration(path: str | Path) -> float | None:
    dur, _, _ = probe_video(path)
    return dur


def _extract_frames(
    path: str | Path,
    out_dir: Path,
    max_frames: int = MAX_FRAMES,
    *,
    require_complete: bool = True,
    duration: float | None = None,
    frame_width: int = FRAME_WIDTH,
) -> list[Path]:
    """
    One ffmpeg pass: evenly spaced low-res JPEGs.

    Uses a single decode with fps sampling instead of N independent seeks
    (each seek can re-decode from a keyframe).
    """
    if duration is None:
        duration, _, _ = probe_video(path)
    pattern = out_dir / "frame_%03d.jpg"
    n = max(3, max_frames)
    output_width = max(1, int(frame_width))
    if duration and duration > 0:
        n = min(max_frames, max(3, min(max_frames, int(duration) + 1)))
        # Spread ~n frames across the whole video.
        fps = n / duration
        vf = f"fps={fps:.8f},scale={output_width}:-1"
    else:
        n = max_frames
        vf = f"fps=1,scale={output_width}:-1"

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        # One decoder thread — we parallelize *across* files, not inside one.
        "-threads",
        "1",
        "-i",
        str(path),
        "-an",
        "-sn",
        "-vf",
        vf,
        "-frames:v",
        str(n),
        "-q:v",
        "5",
        str(pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    if require_complete and result.returncode != 0:
        return []
    return sorted(out_dir.glob("frame_*.jpg"))


def _sample_timestamps(duration: float, max_frames: int = MAX_FRAMES) -> list[float]:
    """Return the timeline positions used by the video fingerprint."""
    count = min(max_frames, max(3, min(max_frames, int(duration) + 1)))
    return [index * duration / count for index in range(count)]


def _extract_hash_frame(path: str | Path, timestamp: float) -> bytes | None:
    """Fast-seek to one frame and return a 32×32 grayscale image buffer.

    Tries hardware decode when available, falling back to software decode.
    """
    hwaccel = _hwaccel_args()
    raw = _extract_hash_frame_once(path, timestamp, hwaccel)
    if raw is None and hwaccel:
        raw = _extract_hash_frame_once(path, timestamp, ())
    return raw


def _extract_hash_frame_once(
    path: str | Path, timestamp: float, hwaccel: tuple[str, ...]
) -> bytes | None:
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *hwaccel,
        # Input-side seeking skips directly to the nearest keyframe instead of
        # decoding the entire timeline just to retain a handful of frames.
        "-ss",
        f"{timestamp:.6f}",
        "-threads",
        "1",
        "-i",
        str(path),
        "-an",
        "-sn",
        "-vf",
        HASH_FRAME_FILTER,
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    expected = HASH_FRAME_SIZE * HASH_FRAME_SIZE
    if result.returncode != 0 or len(result.stdout) != expected:
        return None
    return result.stdout


def _extract_hash_frames(
    path: str | Path, timestamps: list[float], *, software: bool = False
) -> list[bytes]:
    """Fast-seek every sampled timestamp in a single ffmpeg process.

    The same file is opened once per timestamp so each output keeps the exact
    input-side seek of the per-frame extractor, which makes the buffers
    byte-identical to :func:`_extract_hash_frame` while spawning one process
    instead of one per frame. Tries hardware decode when available, falling
    back to software decode; ``software`` skips the hardware attempt. Returns
    ``[]`` when the pass is incomplete so the caller can fall back.
    """
    if not timestamps:
        return []
    hwaccel = () if software else _hwaccel_args()
    frames = _extract_hash_frames_once(path, timestamps, hwaccel)
    if not frames and hwaccel:
        frames = _extract_hash_frames_once(path, timestamps, ())
    return frames


def _extract_hash_frames_once(
    path: str | Path, timestamps: list[float], hwaccel: tuple[str, ...],
    *, cancelled: Callable[[], bool] | None = None,
) -> list[bytes]:
    expected = HASH_FRAME_SIZE * HASH_FRAME_SIZE
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    for timestamp in timestamps:
        cmd += [*hwaccel, "-ss", f"{timestamp:.6f}", "-threads", "1", "-i", str(path)]
    scratch = _thread_scratch_dir()
    seq = _next_scratch_seq()
    targets = [
        scratch / f"frame_{seq}_{index:03d}.raw" for index in range(len(timestamps))
    ]
    for index, target in enumerate(targets):
        cmd += [
            "-map",
            f"{index}:v",
            "-an",
            "-sn",
            "-vf",
            HASH_FRAME_FILTER,
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-y",
            str(target),
        ]
    try:
        result = _run_video_command(cmd, timeout=180, cancelled=cancelled)
        if result.returncode != 0:
            return []
        frames: list[bytes] = []
        for target in targets:
            try:
                raw = target.read_bytes()
            except OSError:
                return []
            if len(raw) != expected:
                return []
            frames.append(raw)
        return frames
    finally:
        for target in targets:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass


def _extract_seek_frame_ppm(
    path: str | Path, timestamp: float, *, frame_width: int
) -> bytes | None:
    """Fast-seek to one aspect-preserving frame and return it as PPM bytes.

    Tries hardware decode when available, falling back to software decode.
    """
    hwaccel = _hwaccel_args()
    raw = _extract_seek_frame_ppm_once(path, timestamp, hwaccel, frame_width=frame_width)
    if raw is None and hwaccel:
        raw = _extract_seek_frame_ppm_once(path, timestamp, (), frame_width=frame_width)
    return raw


def _extract_seek_frame_ppm_once(
    path: str | Path,
    timestamp: float,
    hwaccel: tuple[str, ...],
    *,
    frame_width: int,
) -> bytes | None:
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *hwaccel,
        "-ss",
        f"{timestamp:.6f}",
        "-threads",
        "1",
        "-i",
        str(path),
        "-an",
        "-sn",
        "-vf",
        f"scale={max(1, int(frame_width))}:-2",
        "-frames:v",
        "1",
        "-c:v",
        "ppm",
        "-f",
        "image2pipe",
        "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if result.returncode != 0 or not result.stdout.startswith(b"P6"):
        return None
    return result.stdout


def _extract_seek_frames_ppm(
    path: str | Path, timestamps: list[float], *, frame_width: int
) -> list[bytes]:
    """Fast-seek every timestamp in a single ffmpeg process; PPM per frame.

    Same multi-input ``-ss`` pattern as :func:`_extract_hash_frames`: the file
    is opened once per timestamp so each output keeps the exact input-side
    seek of :func:`_extract_seek_frame_ppm`, with one process spawn instead of
    one per frame. Tries hardware decode when available, falling back to
    software decode. Returns ``[]`` when the pass is incomplete so the caller
    can fall back to per-frame seeks.
    """
    if not timestamps:
        return []
    hwaccel = _hwaccel_args()
    frames = _extract_seek_frames_ppm_once(
        path, timestamps, hwaccel, frame_width=frame_width
    )
    if not frames and hwaccel:
        frames = _extract_seek_frames_ppm_once(
            path, timestamps, (), frame_width=frame_width
        )
    return frames


def _extract_seek_frames_ppm_once(
    path: str | Path,
    timestamps: list[float],
    hwaccel: tuple[str, ...],
    *,
    frame_width: int,
) -> list[bytes]:
    scale = f"scale={max(1, int(frame_width))}:-2"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    for timestamp in timestamps:
        cmd += [*hwaccel, "-ss", f"{timestamp:.6f}", "-threads", "1", "-i", str(path)]
    with tempfile.TemporaryDirectory(prefix="dedupe-vidppm-") as tmp:
        targets = [Path(tmp) / f"frame_{index:03d}.ppm" for index in range(len(timestamps))]
        for index, target in enumerate(targets):
            cmd += [
                "-map",
                f"{index}:v",
                "-an",
                "-sn",
                "-vf",
                scale,
                "-frames:v",
                "1",
                "-c:v",
                "ppm",
                "-f",
                "image2",
                "-y",
                str(target),
            ]
        result = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
        if result.returncode != 0:
            return []
        frames: list[bytes] = []
        for target in targets:
            try:
                ppm = target.read_bytes()
            except OSError:
                return []
            if not ppm.startswith(b"P6"):
                return []
            frames.append(ppm)
        return frames


def compute_video_fingerprint(
    path: str | Path,
    *,
    on_frame: Callable[[int, int], None] | None = None,
    software: bool = False,
) -> tuple[str | None, int | None, int | None, float | None]:
    """
    Return (fingerprint_hex, width, height, duration).
    Fingerprint preserves the ordered pHash sequence of sampled frames.
    ``on_frame(frame_number, total_frames)`` is called after each sampled frame.
    ``software`` decodes the sampled frames on the CPU without trying the
    hardware decoder first.
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg/ffprobe not available")

    import imagehash
    from PIL import Image

    path = Path(path)
    duration, width, height = probe_video(path)

    if duration is not None and duration > 0:
        timestamps = _sample_timestamps(duration)
        raw_frames = _extract_hash_frames(path, timestamps, software=software)
        if len(raw_frames) != len(timestamps):
            # Per-frame fallback for inputs the batched pass cannot satisfy.
            raw_frames = []
            for timestamp in timestamps:
                raw = _extract_hash_frame(path, timestamp)
                if raw is None:
                    return None, width, height, duration
                raw_frames.append(raw)
        hashes = []
        for i, raw in enumerate(raw_frames):
            image = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), raw)
            hashes.append(imagehash.phash(image))
            if on_frame:
                on_frame(i + 1, len(timestamps))
        return "v3:" + ",".join(str(frame_hash) for frame_hash in hashes), width, height, duration

    # Rare fallback for containers whose duration cannot be probed. This still
    # uses the sequential extractor because there are no timestamps to seek to.
    with tempfile.TemporaryDirectory(prefix="dedupe-vid-") as tmp:
        frames = _extract_frames(path, Path(tmp), duration=duration)
        if not frames:
            return None, width, height, duration

        hashes = []
        for idx, fp in enumerate(frames):
            try:
                with Image.open(fp) as img:
                    hashes.append(imagehash.phash(img.convert("RGB")))
            except Exception:
                # Dropping a frame would shift the remaining sample positions
                # and allow an incomplete fingerprint to stand for the video.
                return None, width, height, duration
            else:
                if on_frame:
                    on_frame(idx + 1, len(frames))

        if not hashes:
            return None, width, height, duration

        return "v3:" + ",".join(str(frame_hash) for frame_hash in hashes), width, height, duration


@lru_cache(maxsize=128)
def _aligned_indexes_cached(
    left_len: int, right_len: int
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    count = min(left_len, right_len)
    if count == 0:
        return (), ()
    if count == 1:
        return (0,), (0,)
    left_indexes = tuple(round(i * (left_len - 1) / (count - 1)) for i in range(count))
    right_indexes = tuple(round(i * (right_len - 1) / (count - 1)) for i in range(count))
    return left_indexes, right_indexes


def aligned_position_indexes(
    left_len: int, right_len: int
) -> tuple[list[int], list[int]]:
    """Aligned sample positions for two fingerprint sequences of any length.

    Shared by video grouping, action revalidation, and the UI's similarity
    percentages so every comparison aligns frames the same way.
    """
    left_indexes, right_indexes = _aligned_indexes_cached(left_len, right_len)
    return list(left_indexes), list(right_indexes)


def video_fingerprint_distances(a: str, b: str) -> list[int] | None:
    """Compare ordered frame hashes at normalized positions."""
    left = _fingerprint_hashes(a)
    right = _fingerprint_hashes(b)
    if not left or not right:
        return None
    left_indexes, right_indexes = aligned_position_indexes(len(left), len(right))
    return [
        (left[left_index] ^ right[right_index]).bit_count()
        for left_index, right_index in zip(left_indexes, right_indexes, strict=True)
    ]


def _fingerprint_hashes(value: str) -> tuple[int, ...]:
    """Parse complete 64-bit samples; malformed cached data is not evidence."""
    raw = value[3:] if value.startswith(("v2:", "v3:")) else value
    parts = raw.split(",")
    if any(
        len(part) != 16 or any(char not in "0123456789abcdefABCDEF" for char in part)
        for part in parts
    ):
        return ()
    return tuple(int(part, 16) for part in parts)


def _video_fingerprint_job(
    path: str,
    *,
    on_frame: Callable[[int, int], None] | None = None,
    software: bool = False,
) -> tuple[str, str | None, int | None, int | None, float | None, str | None]:
    """Worker: (path, fingerprint, width, height, duration, error)."""
    try:
        fp, w, h, dur = compute_video_fingerprint(
            path, on_frame=on_frame, software=software
        )
        if not fp:
            return path, None, w, h, dur, f"{ERROR_VIDEO_FINGERPRINT_FAILED}: incomplete frames"
        return path, fp, w, h, dur, None
    except Exception as exc:
        return path, None, None, None, None, f"{ERROR_VIDEO_FINGERPRINT_FAILED}: {exc}"


def _software_decode_workers(
    video_workers: int, *, requested_workers: int | None = None
) -> int:
    """Extra fingerprint jobs that decode on the CPU beside the hardware ones.

    The hardware decoder is one shared device: past a few jobs it is the
    limit and more of them wait on it. Software decoding draws on CPU cores
    instead, so a few such jobs add throughput the hardware lane cannot, and
    they produce the same fingerprints. Each costs several cores, so they
    are added only on machines with cores to spare, and only when the
    hardware lane is already at its cap — a lower worker setting is a
    request for a lighter scan.
    """
    if video_workers < DEFAULT_VIDEO_WORKERS_CAP or not _hwaccel_args():
        return 0
    spare_cores = max(0, (os.cpu_count() or 1) - video_workers)
    if requested_workers is not None and requested_workers > DEFAULT_WORKERS_CAP:
        return min(
            spare_cores,
            max(MAX_SOFTWARE_DECODE_WORKERS, int(requested_workers) - video_workers),
        )
    return min(MAX_SOFTWARE_DECODE_WORKERS, spare_cores // CORES_PER_SOFTWARE_DECODE)


def _run_on_video_lanes(
    fn: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int | None,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[R]:
    """Run ``items`` across the hardware lane plus any spare-core software lanes."""
    hw_workers = resolve_workers(workers, cap=DEFAULT_VIDEO_WORKERS_CAP)
    sw_workers = _software_decode_workers(hw_workers, requested_workers=workers)
    total_workers = hw_workers + sw_workers
    hardware_slots = threading.BoundedSemaphore(hw_workers)

    def wrapped(item: T) -> R:
        prev = getattr(_DECODE_LANE, "software", False)
        if not sw_workers:
            _DECODE_LANE.software = False
            try:
                return fn(item)
            finally:
                _DECODE_LANE.software = prev
        acquired = hardware_slots.acquire(blocking=False)
        _DECODE_LANE.software = not acquired
        try:
            return fn(item)
        finally:
            _DECODE_LANE.software = prev
            if acquired:
                hardware_slots.release()

    return map_parallel(
        wrapped,
        items,
        workers=total_workers,
        # ffmpeg spawns real OS processes -> threads buy true parallelism.
        backend="thread",
        progress=progress,
        cancelled=cancelled,
    )


def _copy_video_fingerprint(donor: FileRecord, target: FileRecord) -> None:
    """Copy a resolved video fingerprint and stream metadata onto an exact sibling."""
    target.video_fingerprint = donor.video_fingerprint
    if donor.width:
        target.width = donor.width
    if donor.height:
        target.height = donor.height
    if donor.duration is not None:
        target.duration = donor.duration


def _safe_partial_hash(path: str) -> tuple[str, str | None]:
    try:
        return path, file_partial_hash(path)
    except OSError:
        return path, None


def _safe_sha256(path: str) -> tuple[str, str | None]:
    try:
        return path, file_sha256(path)
    except OSError:
        return path, None


def _evaluate_candidate_pairs(
    hashed: list[FileRecord],
    fingerprints: list[tuple[int, ...]],
    *,
    threshold: int,
    max_frame_distance: int,
    distinct: DistinctReviews,
    cancelled: Callable[[], bool] | None,
) -> tuple[dict[str, set[str]], list[tuple[FileRecord, FileRecord]]]:
    """Compare candidate pairs using vectorized popcount within ±10% duration windows."""
    import numpy as np

    adjacency: dict[str, set[str]] = {record.path: set() for record in hashed}
    verify_pairs: dict[tuple[int, int], tuple[FileRecord, FileRecord]] = {}
    verify_limit = threshold * VERIFY_DISTANCE_FACTOR

    known_durations = sorted(
        (float(record.duration), index)
        for index, record in enumerate(hashed)
        if record.duration is not None and record.duration > 0
    )
    unknown_indexes = [
        index
        for index, record in enumerate(hashed)
        if record.duration is None or record.duration <= 0
    ]

    def _record_pair(
        u: int, v: int, mean_distance: float, max_distance: int
    ) -> None:
        first, second = (u, v) if u < v else (v, u)
        a, b = hashed[first], hashed[second]
        if distinct and distinct.is_distinct(a, b):
            return
        if mean_distance <= threshold and max_distance <= max_frame_distance:
            adjacency[a.path].add(b.path)
            adjacency[b.path].add(a.path)
        elif mean_distance <= verify_limit:
            verify_pairs[(first, second)] = (a, b)

    def _check_mixed_pair(u: int, v: int) -> None:
        left, right = fingerprints[u], fingerprints[v]
        if (
            (left[0] ^ right[0]).bit_count() > max_frame_distance
            or (left[-1] ^ right[-1]).bit_count() > max_frame_distance
        ):
            return
        left_indexes, right_indexes = _aligned_indexes_cached(len(left), len(right))
        if not left_indexes:
            return
        distances = [
            (left[li] ^ right[ri]).bit_count()
            for li, ri in zip(left_indexes, right_indexes, strict=True)
        ]
        _record_pair(u, v, sum(distances) / len(distances), max(distances))

    if known_durations:
        kd_durations_list = [duration for duration, _ in known_durations]
        kd_indices = [index for _, index in known_durations]
        k_count = len(known_durations)
        kd_lengths = np.array([len(fingerprints[idx]) for idx in kd_indices], dtype=np.int64)
        kd_first = np.array([fingerprints[idx][0] for idx in kd_indices], dtype=np.uint64)
        kd_last = np.array([fingerprints[idx][-1] for idx in kd_indices], dtype=np.uint64)
        width = max(int(kd_lengths.max()), 1)
        kd_fps = np.zeros((k_count, width), dtype=np.uint64)
        for pos, idx in enumerate(kd_indices):
            fp = fingerprints[idx]
            kd_fps[pos, : len(fp)] = fp

        for pos, d_a in enumerate(kd_durations_list):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            u = kd_indices[pos]
            end = bisect_right(kd_durations_list, d_a / 0.9 + 1e-9)
            if end <= pos + 1:
                continue
            first_dist = _popcount64(kd_first[pos + 1 : end] ^ kd_first[pos])
            last_dist = _popcount64(kd_last[pos + 1 : end] ^ kd_last[pos])
            rel = np.flatnonzero(
                (first_dist <= max_frame_distance) & (last_dist <= max_frame_distance)
            )
            if not rel.size:
                continue
            rel = rel + (pos + 1)
            left_len = int(kd_lengths[pos])
            same_mask = kd_lengths[rel] == left_len

            if np.any(same_mask):
                same_rel = rel[same_mask]
                dists = _popcount64(kd_fps[same_rel, :left_len] ^ kd_fps[pos : pos + 1, :left_len])
                means = dists.sum(axis=1) / left_len
                maxs = dists.max(axis=1)
                keep = np.flatnonzero(means <= verify_limit)
                for k_i in keep.tolist():
                    c_pos = int(same_rel[k_i])
                    d_b = kd_durations_list[c_pos]
                    if min(d_a, d_b) / max(d_a, d_b) < 0.9:
                        continue
                    _record_pair(u, kd_indices[c_pos], float(means[k_i]), int(maxs[k_i]))

            if not np.all(same_mask):
                for c_pos in rel[~same_mask].tolist():
                    d_b = kd_durations_list[c_pos]
                    if min(d_a, d_b) / max(d_a, d_b) < 0.9:
                        continue
                    _check_mixed_pair(u, kd_indices[c_pos])

        for u in unknown_indexes:
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            left = fingerprints[u]
            left_len = len(left)
            first_dist = _popcount64(kd_first ^ np.uint64(left[0]))
            last_dist = _popcount64(kd_last ^ np.uint64(left[-1]))
            rel = np.flatnonzero(
                (first_dist <= max_frame_distance) & (last_dist <= max_frame_distance)
            )
            if not rel.size:
                continue
            same_mask = kd_lengths[rel] == left_len
            if np.any(same_mask):
                same_rel = rel[same_mask]
                left_arr = np.array(left, dtype=np.uint64)[None, :]
                dists = _popcount64(kd_fps[same_rel, :left_len] ^ left_arr)
                means = dists.sum(axis=1) / left_len
                maxs = dists.max(axis=1)
                keep = np.flatnonzero(means <= verify_limit)
                for k_i in keep.tolist():
                    c_pos = int(same_rel[k_i])
                    _record_pair(u, kd_indices[c_pos], float(means[k_i]), int(maxs[k_i]))
            if not np.all(same_mask):
                for c_pos in rel[~same_mask].tolist():
                    _check_mixed_pair(u, kd_indices[c_pos])

    for p, u in enumerate(unknown_indexes):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        for v in unknown_indexes[p + 1 :]:
            _check_mixed_pair(u, v)

    to_verify = [verify_pairs[k] for k in sorted(verify_pairs)]
    return adjacency, to_verify


class _FrameBatchCache:
    """Thread-safe singleflight cache for extracted verification frame batches."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, tuple[float, ...]], list] = {}

    def get(
        self,
        path: str,
        timestamps: list[float],
        cancelled: Callable[[], bool] | None,
    ) -> list[bytes]:
        key = (path, tuple(round(float(t), 6) for t in timestamps))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = [threading.Event(), None, None]
                self._entries[key] = entry
                owner = True
            else:
                owner = False
        if not owner:
            event = entry[0]
            while not event.wait(timeout=0.05):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
            if entry[2] is not None:
                raise entry[2]
            return entry[1]
        try:
            frames = _version_frame_batches(path, timestamps, cancelled)
        except BaseException as exc:
            with self._lock:
                entry[2] = exc
                if isinstance(exc, InterruptedError):
                    self._entries.pop(key, None)
            entry[0].set()
            raise
        entry[1] = frames
        entry[0].set()
        return frames


def find_similar_video_groups(
    records: list[FileRecord],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    distinct: DistinctReviews | None = None,
    progress: ProgressCb | None = None,
    workers: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    exact_done: threading.Event | None = None,
) -> list[list[FileRecord]]:
    """Cluster near-identical videos by fingerprint Hamming distance."""
    distinct = distinct or DistinctReviews.empty()
    videos = [r for r in records if r.media_type == MediaType.VIDEO]
    if len(videos) < 2:
        return []

    if not ffmpeg_available():
        if progress:
            progress("video-hash", 0, 0, "")
        return []

    total = len(videos)
    # v3 uses direct timeline seeks and raw grayscale frames. Recompute cached
    # v2 values once so old and new sampling methods are never compared.
    need = [
        r
        for r in videos
        if not r.video_fingerprint
        or not r.video_fingerprint.startswith("v3:")
        or not 1 <= len(_fingerprint_hashes(r.video_fingerprint)) <= MAX_FRAMES
    ]
    cached = total - len(need)
    videos_done = cached
    progress_lock = threading.Lock()

    if need:
        for record in need:
            # A failed upgrade must never leave a legacy fingerprint eligible
            # for clustering or persistence back into the cache.
            record.video_fingerprint = None

        need_paths = {r.path for r in need}
        donors_by_size_sha: dict[tuple[int, str], FileRecord] = {}
        for r in videos:
            if r.path not in need_paths and r.size > 0 and r.sha256 and r.video_fingerprint:
                donors_by_size_sha.setdefault((r.size, r.sha256), r)

        remaining_need: list[FileRecord] = []
        for r in need:
            donor = (
                donors_by_size_sha.get((r.size, r.sha256))
                if r.size > 0 and r.sha256
                else None
            )
            if donor is not None:
                _copy_video_fingerprint(donor, r)
                videos_done += 1
                if progress:
                    progress("video-hash", videos_done, total, "")
            else:
                remaining_need.append(r)

        def on_frame(frame: int, n_frames: int) -> None:
            with progress_lock:
                if progress:
                    progress(
                        "video-hash",
                        videos_done,
                        total,
                        f"Video hashing: {videos_done}/{total} · frame {frame}/{n_frames}",
                    )

        def _apply_fingerprint_results(
            targets: list[FileRecord],
            results: list[tuple[str, str | None, int | None, int | None, float | None, str | None]],
        ) -> None:
            nonlocal videos_done
            by_path = {r.path: r for r in targets}
            for path, fp, w, h, dur, err in results:
                rec = by_path[path]
                if err:
                    rec.error = err
                else:
                    rec.video_fingerprint = fp
                    if w:
                        rec.width = w
                    if h:
                        rec.height = h
                    if dur is not None:
                        rec.duration = dur
                videos_done += 1
                if progress:
                    progress("video-hash", videos_done, total, "")

        def _fingerprint_batch(targets: list[FileRecord]) -> None:
            if not targets:
                return
            results = _run_on_video_lanes(
                lambda path: _video_fingerprint_job(
                    path,
                    on_frame=on_frame,
                    software=bool(getattr(_DECODE_LANE, "software", False)),
                ),
                [r.path for r in targets],
                workers=workers,
                cancelled=cancelled,
            )
            _apply_fingerprint_results(targets, results)

        if remaining_need:
            size_counts: dict[int, int] = defaultdict(int)
            for r in videos:
                if r.size > 0:
                    size_counts[r.size] += 1
            shared_sizes = {
                r.size
                for r in remaining_need
                if r.size > 0 and size_counts[r.size] >= 2
            }

            partial_by_path: dict[str, str] = {}
            if shared_sizes:
                partial_targets: list[str] = []
                for r in videos:
                    if r.size not in shared_sizes:
                        continue
                    if r.partial_hash:
                        partial_by_path[r.path] = r.partial_hash
                    else:
                        partial_targets.append(r.path)
                if partial_targets:
                    partial_results = map_parallel(
                        _safe_partial_hash,
                        partial_targets,
                        workers=resolve_workers(workers, cap=DEFAULT_WORKERS_CAP),
                        backend="thread",
                        cancelled=cancelled,
                    )
                    for path, ph in partial_results:
                        if ph is not None:
                            partial_by_path[path] = ph

            remaining_set = {r.path for r in remaining_need}
            buckets: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
            for r in videos:
                if r.size in shared_sizes:
                    ph = partial_by_path.get(r.path)
                    if ph is not None:
                        buckets[(r.size, ph)].append(r)

            wave1: list[FileRecord] = []
            deferred: list[FileRecord] = []
            assigned_to_wave: set[str] = set()
            for bucket in buckets.values():
                if len(bucket) < 2:
                    continue
                has_cached_donor = any(
                    r.path not in remaining_set and r.video_fingerprint for r in bucket
                )
                picked_donor = has_cached_donor
                for r in bucket:
                    if r.path not in remaining_set or r.path in assigned_to_wave:
                        continue
                    assigned_to_wave.add(r.path)
                    if not picked_donor:
                        wave1.append(r)
                        picked_donor = True
                    else:
                        deferred.append(r)
            for r in remaining_need:
                if r.path not in assigned_to_wave:
                    wave1.append(r)

            _fingerprint_batch(wave1)

            if deferred:
                if exact_done is not None:
                    while not exact_done.wait(timeout=0.05):
                        if cancelled and cancelled():
                            raise InterruptedError("scan cancelled")

                bucket_by_key: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
                for key, bucket in buckets.items():
                    if len(bucket) >= 2:
                        bucket_by_key[key] = bucket

                deferred_keys: dict[str, tuple[int, str]] = {}
                for r in deferred:
                    ph = partial_by_path.get(r.path)
                    if ph is not None:
                        deferred_keys[r.path] = (r.size, ph)

                # Identify candidate donors with valid fingerprints in those buckets.
                donor_candidates: list[FileRecord] = []
                for key in set(deferred_keys.values()):
                    for r in bucket_by_key.get(key, ()):
                        if r.video_fingerprint:
                            donor_candidates.append(r)

                wave2: list[FileRecord] = []
                if donor_candidates:
                    sha_by_path: dict[str, str] = {}
                    sha_targets: list[str] = []
                    seen_sha_targets: set[str] = set()
                    for r in donor_candidates + deferred:
                        if r.path in seen_sha_targets:
                            continue
                        seen_sha_targets.add(r.path)
                        if r.sha256:
                            sha_by_path[r.path] = r.sha256
                        elif r.size <= PARTIAL_SIZE and r.path in partial_by_path:
                            sha_by_path[r.path] = partial_by_path[r.path]
                        else:
                            sha_targets.append(r.path)
                    if sha_targets:
                        sha_results = map_parallel(
                            _safe_sha256,
                            sha_targets,
                            workers=resolve_workers(workers, cap=DEFAULT_WORKERS_CAP),
                            backend="thread",
                            cancelled=cancelled,
                        )
                        for path, sh in sha_results:
                            if sh is not None:
                                sha_by_path[path] = sh

                    valid_donors: dict[tuple[int, str], FileRecord] = {}
                    for r in donor_candidates:
                        sh = sha_by_path.get(r.path)
                        if sh is not None:
                            valid_donors.setdefault((r.size, sh), r)

                    for r in deferred:
                        sh = sha_by_path.get(r.path)
                        donor = valid_donors.get((r.size, sh)) if sh is not None else None
                        if donor is not None:
                            _copy_video_fingerprint(donor, r)
                            videos_done += 1
                            if progress:
                                progress("video-hash", videos_done, total, "")
                        else:
                            wave2.append(r)
                else:
                    wave2 = deferred

                _fingerprint_batch(wave2)

    if progress:
        progress("video-hash", videos_done, total, "")

    hashed = [r for r in videos if r.video_fingerprint]
    if len(hashed) < 2:
        return []

    fingerprints = [_fingerprint_hashes(record.video_fingerprint or "") for record in hashed]
    max_frame_distance = max(threshold * 2, 4)
    adjacency, to_verify = _evaluate_candidate_pairs(
        hashed,
        fingerprints,
        threshold=threshold,
        max_frame_distance=max_frame_distance,
        distinct=distinct,
        cancelled=cancelled,
    )

    if to_verify:
        frame_cache = _FrameBatchCache()
        remaining_verify: list[tuple[FileRecord, FileRecord]] = []
        for a, b in to_verify:
            if a.size > 0 and a.size == b.size and a.sha256 and a.sha256 == b.sha256:
                adjacency[a.path].add(b.path)
                adjacency[b.path].add(a.path)
            else:
                remaining_verify.append((a, b))

        if remaining_verify:
            def verify_progress(done: int, total_items: int) -> None:
                if progress:
                    progress(
                        "video-cluster",
                        done,
                        total_items,
                        f"Verifying video matches: {done}/{total_items}",
                    )

            verified = _run_on_video_lanes(
                lambda pair: _same_footage_after_reframing(
                    pair[0], pair[1], cancelled, frame_cache=frame_cache
                ),
                remaining_verify,
                workers=workers,
                progress=verify_progress,
                cancelled=cancelled,
            )
            for (a, b), matched in zip(remaining_verify, verified, strict=True):
                if matched:
                    adjacency[a.path].add(b.path)
                    adjacency[b.path].add(a.path)

    if progress:
        progress("video-cluster", len(hashed), len(hashed), "")

    return cluster_around_best(hashed, adjacency, distinct)


def _same_footage_after_reframing(
    a: FileRecord,
    b: FileRecord,
    cancelled: Callable[[], bool] | None,
    *,
    frame_cache: _FrameBatchCache | None = None,
) -> bool:
    """Verify a near-miss pair at its start- and end-aligned offsets.

    A slight crop or re-encode moves every 32×32 frame hash a little, and a
    few frames added or cut at one end shift the proportional samples. Both
    can push a copy just past the fingerprint threshold. The offset check
    samples the overlap again and requires nearly every frame to agree, so a
    different take of the same scene stays separate.
    """
    if not a.duration or not b.duration:
        return False
    for offset in sorted({0.0, round(float(b.duration) - float(a.duration), 3)}, key=abs):
        try:
            if _verify_video_offset(a, b, offset, cancelled, frame_cache=frame_cache):
                return True
        except InterruptedError:
            raise
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            continue
    return False


# The optional version pass is independent of the v3 hashes. Its denser cache
# never changes the meaning of hashes used by existing reviews or revalidation.
VERSION_SAMPLE_LIMIT = 96
VERSION_SAMPLE_STEP = 0.5
VERSION_MIN_SECONDS = 2.0
VERSION_CACHE_VERSION = 2


def _run_video_command(
    command: list[str], *, timeout: float = 90,
    cancelled: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess:
    if cancelled is None:
        return subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if cancelled():
        raise InterruptedError("scan cancelled")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancelled():
                raise InterruptedError("scan cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("video analysis timed out")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def _version_timestamps(duration: float) -> list[float]:
    count = min(VERSION_SAMPLE_LIMIT, max(1, math.ceil(duration / VERSION_SAMPLE_STEP)))
    step = duration / count
    # Stay away from boundary frames which can be black or absent after an edit.
    return [(i + 0.25) * step for i in range(count)]


def _version_frame_batches(
    path: str, timestamps: list[float], cancelled: Callable[[], bool] | None,
) -> list[bytes]:
    software = bool(getattr(_DECODE_LANE, "software", False))
    hwaccel = () if software else _hwaccel_args()
    frames: list[bytes] = []
    for start in range(0, len(timestamps), 12):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        chunk = timestamps[start:start + 12]
        chunk_frames = _extract_hash_frames_once(path, chunk, hwaccel, cancelled=cancelled)
        if not chunk_frames and hwaccel:
            chunk_frames = _extract_hash_frames_once(path, chunk, (), cancelled=cancelled)
        frames.extend(chunk_frames)
    if len(frames) != len(timestamps):
        raise ValueError("incomplete video version samples")
    return frames


def _version_cache_identity(record: FileRecord) -> str:
    stat = Path(record.path).stat()
    return json.dumps([
        VERSION_CACHE_VERSION, str(Path(record.path).resolve()), stat.st_dev, stat.st_ino,
        stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
    ], separators=(",", ":"))


def _extract_version_samples_uncached(
    record: FileRecord,
    cancelled: Callable[[], bool] | None,
) -> tuple[float | None, int | None, int | None, list[tuple[float, int]], str | None]:
    import imagehash
    import numpy as np
    from PIL import Image

    try:
        duration = record.duration
        width, height = record.width, record.height
        if not duration or not math.isfinite(duration):
            duration, probed_w, probed_h = probe_video(record.path)
            if probed_w:
                width = probed_w
            if probed_h:
                height = probed_h
        if not duration or duration < VERSION_MIN_SECONDS:
            return duration, width, height, [], None
        timestamps = _version_timestamps(duration)
        frames = _version_frame_batches(record.path, timestamps, cancelled)
        extracted: list[tuple[float, int]] = []
        for timestamp, raw in zip(timestamps, frames, strict=True):
            # Blank frames and nearly uniform fades are not useful candidate evidence.
            if float(np.frombuffer(raw, dtype=np.uint8).std()) < 8:
                continue
            im = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), raw)
            extracted.append((timestamp, int(str(imagehash.phash(im)), 16)))
        return duration, width, height, extracted, None
    except InterruptedError:
        raise
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return None, None, None, [], str(exc)


def _version_samples(
    record: FileRecord, connection: sqlite3.Connection | None,
    cancelled: Callable[[], bool] | None,
    cache_errors: list[str] | None = None,
) -> list[tuple[float, int]]:
    key = _version_cache_identity(record) if connection is not None else None
    if connection is not None:
        try:
            row = connection.execute(
                "SELECT samples FROM samples WHERE identity = ?", (key,),
            ).fetchone()
        except sqlite3.Error as exc:
            if cache_errors is not None:
                cache_errors.append(str(exc))
            connection, row = None, None
        if row:
            try:
                data = json.loads(row[0])
                duration = float(data["duration"])
                samples = [(float(t), int(h, 16)) for t, h in data["samples"]]
                if (
                    math.isfinite(duration) and duration > 0
                    and len(samples) <= VERSION_SAMPLE_LIMIT
                    and all(0 <= t < duration and 0 <= h < (1 << 64) for t, h in samples)
                ):
                    record.duration = duration
                    return samples
            except (KeyError, TypeError, ValueError):
                pass
    duration, width, height, samples, err = _extract_version_samples_uncached(record, cancelled)
    if err is not None:
        raise ValueError(err)
    record.duration = duration
    if width:
        record.width = width
    if height:
        record.height = height
    if connection is not None and duration and duration >= VERSION_MIN_SECONDS:
        try:
            connection.execute(
                "INSERT OR REPLACE INTO samples(identity, samples) VALUES (?, ?)",
                (key, json.dumps({"duration": duration,
                                  "samples": [(t, f"{h:016x}") for t, h in samples]})),
            )
            connection.commit()
        except sqlite3.Error as exc:
            if cache_errors is not None:
                cache_errors.append(str(exc))
    return samples


# Hash pairs compared per batch of the candidate join: bounds its working
# arrays to tens of megabytes however many videos a scan holds.
_VERSION_JOIN_BATCH_PAIRS = 1 << 21


def _version_candidate_pairs(
    samples: list[list[tuple[float, int]]],
    cancelled: Callable[[], bool] | None = None,
) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """Index frame hashes once; avoid decoding or testing every pair of videos."""
    import numpy as np

    occurrences: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for index, timeline in enumerate(samples):
        for timestamp, fingerprint in timeline:
            occurrences[fingerprint].append((index, timestamp))
    if not occurrences:
        return {}
    # Every distinct hash is compared with every other in vectorised batches.
    # A tree index prunes almost nothing at this radius on 64-bit hashes, and
    # walking one in Python took minutes for a thousand videos.
    keys = sorted(occurrences)
    values = np.array(keys, dtype=np.uint64)
    weights = np.array([len(occurrences[key]) for key in keys], dtype=np.int64)
    rows = max(1, _VERSION_JOIN_BATCH_PAIRS // len(keys))
    near: dict[int, list[int]] = {}
    for begin in range(0, len(keys), rows):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        # Off-grid cuts shift samples by a fraction of a second. Candidate
        # lookup is deliberately looser than the aligned verification.
        distances = _popcount64(values[begin:begin + rows, None] ^ values[None, :])
        hits = distances <= 10
        # Generic repeated title cards must not expand into an unbounded join.
        bounded = np.flatnonzero(hits @ weights <= 256)
        for row in bounded.tolist():
            columns = np.flatnonzero(hits[row])
            order = np.argsort(distances[row, columns], kind="stable")
            near[keys[begin + row]] = [keys[column] for column in columns[order].tolist()]
    candidates: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for i, timeline in enumerate(samples):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        for ta, fingerprint in timeline:
            for matched in near.get(fingerprint, ()):
                for j, tb in occurrences[matched]:
                    if j > i:
                        candidates[(i, j)].append((ta, tb))
    return {
        pair: votes for pair, votes in candidates.items()
        if len({ta for ta, _tb in votes}) >= 3 and len({tb for _ta, tb in votes}) >= 3
    }


def _popcount64(values):
    """Set bits of each uint64 in an array, whatever its shape."""
    import numpy as np

    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(values)
    as_bytes = np.ascontiguousarray(values).view(np.uint8).reshape(*values.shape, 8)
    return np.unpackbits(as_bytes, axis=-1).sum(axis=-1)


def _offset_candidates(
    votes: list[tuple[float, float]], step: float,
) -> list[float]:
    bins: dict[int, list[float]] = defaultdict(list)
    for ta, tb in votes:
        offset = tb - ta
        bins[round(offset / step)].append(offset)
    ranked = sorted(bins.values(), key=lambda values: (-len(values), abs(sum(values) / len(values))))
    return [sorted(values)[len(values) // 2] for values in ranked[:3]]


def _verify_video_offset(
    a: FileRecord, b: FileRecord, offset: float,
    cancelled: Callable[[], bool] | None,
    *,
    frame_cache: _FrameBatchCache | None = None,
) -> dict | None:
    """Verify distributed samples across the overlap, not just matching titles.

    Offset maps an A timestamp onto B. This deliberately requires essentially
    all of the shorter clip, not an arbitrary small scene shared by two movies.
    """
    import imagehash
    import numpy as np
    from PIL import Image

    da, db = float(a.duration or 0), float(b.duration or 0)
    start = max(0.0, -offset)
    end = min(da, db - offset)
    overlap = end - start
    if overlap < VERSION_MIN_SECONDS or overlap < min(da, db) * 0.8:
        return None
    times = [start + overlap * fraction for fraction in (0.08, 0.22, 0.36, 0.5, 0.64, 0.78, 0.92)]
    right_times = [t + offset for t in times]
    if frame_cache is not None:
        left = frame_cache.get(a.path, times, cancelled)
        right = frame_cache.get(b.path, right_times, cancelled)
    else:
        left = _version_frame_batches(a.path, times, cancelled)
        right = _version_frame_batches(b.path, right_times, cancelled)
    distances, residuals, hashes = [], [], []
    for ra, rb in zip(left, right, strict=True):
        ia = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), ra)
        ib = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), rb)
        ha, hb = imagehash.phash(ia), imagehash.phash(ib)
        hashes.append(int(str(ha), 16))
        distances.append(ha - hb)
        aa, ab = np.frombuffer(ra, np.uint8).astype(float), np.frombuffer(rb, np.uint8).astype(float)
        residuals.append(float(np.abs(aa - ab).mean()))
    diverse = sum((hashes[0] ^ h).bit_count() >= 4 for h in hashes[1:]) >= 2
    good = [d <= 10 and residual <= 18 for d, residual in zip(distances, residuals, strict=True)]
    if not diverse or sum(good) < 6 or sum(distances) / len(distances) > 7:
        return None
    return {
        "ranges": {a.path: [round(start, 3), round(end, 3)],
                   b.path: [round(start + offset, 3), round(end + offset, 3)]},
        "overlap_seconds": round(overlap, 3),
        "mean_frame_distance": round(float(sum(distances)) / len(distances), 3),
        "verified_samples": len(times),
        "approximate_alignment": True,
        "relation": "same_footage" if abs(offset) < 0.1 and abs(da - db) < 0.2 else "excerpt",
    }


def _audio_stream_present(path: str, cancelled: Callable[[], bool] | None) -> bool:
    result = _run_video_command([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index",
        "-of", "json", path,
    ], timeout=15, cancelled=cancelled)
    if result.returncode:
        raise ValueError("audio stream unavailable")
    return bool(json.loads(result.stdout).get("streams"))


def _audio_window(path: str, start: float, duration: float, cancelled) -> bytes:
    result = _run_video_command([
        "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.4f}", "-threads", "1",
        "-i", path, "-t", f"{duration:.4f}", "-vn", "-ac", "1", "-ar", "8000",
        "-f", "s16le", "pipe:1",
    ], timeout=30, cancelled=cancelled)
    if result.returncode:
        raise ValueError("audio sample unavailable")
    return result.stdout


def compare_video_audio(
    a: str, b: str, ranges: dict[str, list[float]],
    *, cancelled: Callable[[], bool] | None = None,
    presence_cache: dict[str, bool] | None = None,
    presence_lock: threading.Lock | None = None,
) -> str:
    """Conservative audio comparison of two distributed windows of the overlap.

    'same' means sampled audio agrees, not that complete tracks are identical.
    Decode failures stay 'unavailable' rather than becoming false differences.
    """
    import numpy as np
    from scipy.signal import correlate

    def _has_audio(path: str) -> bool:
        if presence_cache is None:
            return _audio_stream_present(path, cancelled)
        if presence_lock is not None:
            with presence_lock:
                if path in presence_cache:
                    return presence_cache[path]
        elif path in presence_cache:
            return presence_cache[path]
        present = _audio_stream_present(path, cancelled)
        if presence_lock is not None:
            with presence_lock:
                presence_cache[path] = present
        else:
            presence_cache[path] = present
        return present

    try:
        presence = [_has_audio(path) for path in (a, b)]
        if presence == [False, False]:
            return "none"
        if presence[0] != presence[1]:
            return "different"
        overlap = min(ranges[a][1] - ranges[a][0], ranges[b][1] - ranges[b][0])
        duration = min(6.0, overlap * 0.4)
        scores = []
        for fraction in (0.1, 0.5):
            arrays = [np.frombuffer(_audio_window(
                path, ranges[path][0] + overlap * fraction, duration, cancelled,
            ), dtype="<i2").astype(float) for path in (a, b)]
            n = min(map(len, arrays))
            if n < 800:
                return "unavailable"
            left, right = (array[:n] - array[:n].mean() for array in arrays)
            norms = [float(np.linalg.norm(array)) for array in (left, right)]
            if min(norms) < 1:
                scores.append(1.0 if max(norms) < 1 else 0.0)
                continue
            cross = correlate(left, right, mode="full", method="fft")
            radius = min(4000, n // 5)
            center = n - 1
            scores.append(float(np.max(cross[center - radius:center + radius + 1]) / np.prod(norms)))
        if min(scores) >= 0.85:
            return "same"
        return "different" if max(scores) < 0.5 else "unavailable"
    except InterruptedError:
        raise
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
        return "unavailable"


def find_video_version_matches(
    records: list[FileRecord], *, distinct: DistinctReviews | None = None,
    progress: ProgressCb | None = None, workers: int | None = None,
    cancelled: Callable[[], bool] | None = None, cache_path: str | Path | None = None,
    errors: list[str] | None = None, use_cache: bool = True,
) -> list[tuple[list[FileRecord], dict]]:
    """Find manual-review video versions and attach aligned ranges/audio evidence.

    Dense samples are cached independently of the standard v3 fingerprint. No
    full pairwise video decoding is performed: a frame-hash index proposes pairs
    and at most three offsets per pair receive seven-frame verification. Bounded
    sampling can miss very short excerpts in long movies; it never proves that
    unsampled frames/audio are identical.
    """
    videos = [r for r in records if r.media_type == MediaType.VIDEO]
    if len(videos) < 2 or not ffmpeg_available():
        return []
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    if cache_path is None:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        cache_path = base / "dedupe" / "video-versions.sqlite3"
    connection = None
    cache_errors: list[str] = []
    try:
        if use_cache:
            try:
                Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(str(cache_path), timeout=0.25)
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS samples(identity TEXT PRIMARY KEY, samples TEXT)"
                )
            except (OSError, sqlite3.Error) as exc:
                cache_errors.append(str(exc))
                if connection is not None:
                    connection.close()
                    connection = None

        samples: list[list[tuple[float, int]]] = [[] for _ in videos]
        identities: list[str | None] = [None] * len(videos)
        uncached_indices: list[int] = []
        done_count = 0

        for i, record in enumerate(videos):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            cached_hit = False
            if connection is not None:
                try:
                    key = _version_cache_identity(record)
                    identities[i] = key
                except OSError as exc:
                    if errors is not None:
                        errors.append(f"video version analysis failed: {record.path}: {exc}")
                    done_count += 1
                    if progress:
                        progress("video-versions", done_count, len(videos), "Finding video excerpts")
                    continue
                try:
                    row = connection.execute(
                        "SELECT samples FROM samples WHERE identity = ?", (key,),
                    ).fetchone()
                except sqlite3.Error as exc:
                    cache_errors.append(str(exc))
                    connection.close()
                    connection = None
                    row = None
                if row:
                    try:
                        data = json.loads(row[0])
                        duration = float(data["duration"])
                        cached_samples = [(float(t), int(h, 16)) for t, h in data["samples"]]
                        if (
                            math.isfinite(duration) and duration > 0
                            and len(cached_samples) <= VERSION_SAMPLE_LIMIT
                            and all(
                                0 <= t < duration and 0 <= h < (1 << 64)
                                for t, h in cached_samples
                            )
                        ):
                            record.duration = duration
                            samples[i] = cached_samples
                            cached_hit = True
                    except (KeyError, TypeError, ValueError):
                        pass
            if cached_hit:
                done_count += 1
                if progress:
                    progress("video-versions", done_count, len(videos), "Finding video excerpts")
            else:
                uncached_indices.append(i)

        sha_by_idx: dict[int, str] = {
            i: r.sha256 for i, r in enumerate(videos) if r.sha256
        }

        if uncached_indices:
            uncached_set = set(uncached_indices)
            size_counts: dict[int, int] = defaultdict(int)
            for r in videos:
                if r.size > 0:
                    size_counts[r.size] += 1
            shared_sizes = {
                videos[i].size
                for i in uncached_indices
                if videos[i].size > 0 and size_counts[videos[i].size] >= 2
            }

            if shared_sizes:
                colliding = [i for i, r in enumerate(videos) if r.size in shared_sizes]
                partial_by_idx: dict[int, str] = {}
                partial_needed: list[tuple[int, str]] = []
                for idx in colliding:
                    rec = videos[idx]
                    if rec.partial_hash:
                        partial_by_idx[idx] = rec.partial_hash
                    elif idx not in sha_by_idx:
                        partial_needed.append((idx, rec.path))
                if partial_needed:
                    part_results = map_parallel(
                        _safe_partial_hash,
                        [path for _, path in partial_needed],
                        workers=resolve_workers(workers, cap=DEFAULT_WORKERS_CAP),
                        backend="thread",
                        cancelled=cancelled,
                    )
                    for (idx, _path), (_p, ph) in zip(partial_needed, part_results, strict=True):
                        if ph is not None:
                            partial_by_idx[idx] = ph

                partial_buckets: dict[tuple[int, str], list[int]] = defaultdict(list)
                for idx in colliding:
                    ph = partial_by_idx.get(idx)
                    if ph is not None:
                        partial_buckets[(videos[idx].size, ph)].append(idx)

                sha_needed: list[tuple[int, str]] = []
                for (sz, _ph), group_idxs in partial_buckets.items():
                    if len(group_idxs) < 2:
                        continue
                    for idx in group_idxs:
                        if idx not in sha_by_idx:
                            if sz <= PARTIAL_SIZE and idx in partial_by_idx:
                                sha_by_idx[idx] = partial_by_idx[idx]
                            else:
                                sha_needed.append((idx, videos[idx].path))
                if sha_needed:
                    sha_results = map_parallel(
                        _safe_sha256,
                        [path for _, path in sha_needed],
                        workers=resolve_workers(workers, cap=DEFAULT_WORKERS_CAP),
                        backend="thread",
                        cancelled=cancelled,
                    )
                    for (idx, _path), (_p, sh) in zip(sha_needed, sha_results, strict=True):
                        if sh is not None:
                            sha_by_idx[idx] = sh

            cached_by_sha: dict[tuple[int, str], int] = {}
            for i, rec in enumerate(videos):
                if i in uncached_set or rec.size <= 0 or not rec.duration:
                    continue
                sh = sha_by_idx.get(i)
                if sh is not None:
                    cached_by_sha.setdefault((rec.size, sh), i)

            to_store: list[tuple[str, str]] = []
            representatives: list[int] = []
            rep_by_sha: dict[tuple[int, str], int] = {}
            siblings_by_rep: dict[int, list[int]] = defaultdict(list)

            for i in uncached_indices:
                rec = videos[i]
                sh = sha_by_idx.get(i) if rec.size > 0 else None
                key_sha = (rec.size, sh) if sh is not None else None
                if key_sha is not None and key_sha in cached_by_sha:
                    donor_idx = cached_by_sha[key_sha]
                    donor = videos[donor_idx]
                    donor_samples = list(samples[donor_idx])
                    rec.duration = donor.duration
                    if donor.width:
                        rec.width = donor.width
                    if donor.height:
                        rec.height = donor.height
                    samples[i] = donor_samples
                    if (
                        connection is not None
                        and identities[i]
                        and donor.duration
                        and donor.duration >= VERSION_MIN_SECONDS
                    ):
                        to_store.append((
                            identities[i],
                            json.dumps({
                                "duration": donor.duration,
                                "samples": [(t, f"{h:016x}") for t, h in donor_samples],
                            }),
                        ))
                    done_count += 1
                    if progress:
                        progress("video-versions", done_count, len(videos), "Finding video excerpts")
                elif key_sha is not None and key_sha in rep_by_sha:
                    siblings_by_rep[rep_by_sha[key_sha]].append(i)
                else:
                    if key_sha is not None:
                        rep_by_sha[key_sha] = i
                    representatives.append(i)

            if representatives:
                base_done = done_count
                remaining_total = len(videos) - base_done

                def sample_progress(done: int, rep_total: int) -> None:
                    if progress and rep_total > 0:
                        reported = base_done + (done * remaining_total) // rep_total
                        progress("video-versions", reported, len(videos), "Finding video excerpts")

                extracted_results = _run_on_video_lanes(
                    lambda idx: _extract_version_samples_uncached(videos[idx], cancelled),
                    representatives,
                    workers=workers,
                    progress=sample_progress,
                    cancelled=cancelled,
                )

                for rep_idx, (dur, w, h, extracted, err) in zip(
                    representatives, extracted_results, strict=True
                ):
                    group_idxs = [rep_idx, *siblings_by_rep.get(rep_idx, ())]
                    for idx in group_idxs:
                        rec = videos[idx]
                        if err is not None:
                            samples[idx] = []
                            if errors is not None:
                                errors.append(f"video version analysis failed: {rec.path}: {err}")
                        else:
                            rec.duration = dur
                            if w:
                                rec.width = w
                            if h:
                                rec.height = h
                            samples[idx] = list(extracted)
                            if (
                                connection is not None
                                and identities[idx]
                                and dur
                                and dur >= VERSION_MIN_SECONDS
                            ):
                                to_store.append((
                                    identities[idx],
                                    json.dumps({
                                        "duration": dur,
                                        "samples": [(t, f"{hv:016x}") for t, hv in extracted],
                                    }),
                                ))

                if progress:
                    progress("video-versions", len(videos), len(videos), "Finding video excerpts")

            if connection is not None and to_store:
                try:
                    connection.executemany(
                        "INSERT OR REPLACE INTO samples(identity, samples) VALUES (?, ?)",
                        to_store,
                    )
                    connection.commit()
                except sqlite3.Error as exc:
                    cache_errors.append(str(exc))

        pairs = _version_candidate_pairs(samples, cancelled)
        candidate_items: list[tuple[int, int, list[tuple[float, float]]]] = []
        for (i, j), votes in pairs.items():
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            a, b = videos[i], videos[j]
            if distinct and distinct.is_distinct(a, b):
                continue
            sha_a = sha_by_idx.get(i)
            if (
                a.size > 0
                and a.size == b.size
                and sha_a is not None
                and sha_a == sha_by_idx.get(j)
            ):
                # Byte-identical copies always produce same_footage + matching audio.
                continue
            candidate_items.append((i, j, votes))

        if not candidate_items:
            return []

        frame_cache = _FrameBatchCache()
        audio_presence_cache: dict[str, bool] = {}
        audio_presence_lock = threading.Lock()

        def _verify_candidate(
            item: tuple[int, int, list[tuple[float, float]]],
        ) -> tuple[list[FileRecord], dict] | None:
            i, j, votes = item
            a, b = videos[i], videos[j]
            step = max(float(a.duration or 0), float(b.duration or 0)) / VERSION_SAMPLE_LIMIT
            offsets = _offset_candidates(votes, max(0.25, step))
            evidence = None
            for offset in offsets:
                try:
                    evidence = _verify_video_offset(
                        a, b, offset, cancelled, frame_cache=frame_cache
                    )
                except InterruptedError:
                    raise
                except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
                    continue
                if evidence:
                    break
            if evidence is None:
                return None
            evidence["audio"] = compare_video_audio(
                a.path,
                b.path,
                evidence["ranges"],
                cancelled=cancelled,
                presence_cache=audio_presence_cache,
                presence_lock=audio_presence_lock,
            )
            evidence["audio_scope"] = "sampled overlap"
            evidence["manual_review"] = True
            # Same-footage pairs with agreeing audio retain the standard matcher.
            if evidence["relation"] == "same_footage" and evidence["audio"] in {"same", "none"}:
                return None
            return [a, b], evidence

        verified = _run_on_video_lanes(
            _verify_candidate,
            candidate_items,
            workers=workers,
            cancelled=cancelled,
        )
        return [match for match in verified if match is not None]
    finally:
        if connection is not None:
            connection.close()
        if cache_errors and errors is not None:
            errors.append(f"video version cache unavailable; continued uncached: {cache_errors[0]}")
