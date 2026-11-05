"""Near-duplicate video detection via ffmpeg frame sampling + perceptual hashing.

Resource-conscious: at most a few concurrent ffmpeg jobs, each limited to a
single decoder thread, with one-pass frame extraction and small frame size.
"""

from __future__ import annotations

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
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from .cache import DistinctReviews
from .grouping import cluster_around_best
from .models import FileRecord, MediaType
from .parallel import DEFAULT_VIDEO_WORKERS_CAP, map_parallel, resolve_workers

ProgressCb = Callable[[str, int, int, str | None], None]

DEFAULT_THRESHOLD = 8  # Mean Hamming distance across aligned 64-bit frame hashes

# Error-kind prefix; engine diagnostics classify on it.
ERROR_VIDEO_FINGERPRINT_FAILED = "video fingerprint failed"
MAX_FRAMES = 8  # enough signal; fewer seeks/decodes than 12
FRAME_WIDTH = 320
HASH_FRAME_SIZE = 32
HASH_FRAME_FILTER = f"scale={HASH_FRAME_SIZE}:{HASH_FRAME_SIZE}:flags=lanczos,format=gray"


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


def _extract_hash_frames(path: str | Path, timestamps: list[float]) -> list[bytes]:
    """Fast-seek every sampled timestamp in a single ffmpeg process.

    The same file is opened once per timestamp so each output keeps the exact
    input-side seek of the per-frame extractor, which makes the buffers
    byte-identical to :func:`_extract_hash_frame` while spawning one process
    instead of one per frame. Tries hardware decode when available, falling
    back to software decode. Returns ``[]`` when the pass is incomplete so the
    caller can fall back.
    """
    if not timestamps:
        return []
    hwaccel = _hwaccel_args()
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
    with tempfile.TemporaryDirectory(prefix="dedupe-vidhash-") as tmp:
        targets = [Path(tmp) / f"frame_{index:03d}.raw" for index in range(len(timestamps))]
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
) -> tuple[str | None, int | None, int | None, float | None]:
    """
    Return (fingerprint_hex, width, height, duration).
    Fingerprint preserves the ordered pHash sequence of sampled frames.
    ``on_frame(frame_number, total_frames)`` is called after each sampled frame.
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg/ffprobe not available")

    import imagehash
    from PIL import Image

    path = Path(path)
    duration, width, height = probe_video(path)

    if duration is not None and duration > 0:
        timestamps = _sample_timestamps(duration)
        raw_frames = _extract_hash_frames(path, timestamps)
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


def aligned_position_indexes(
    left_len: int, right_len: int
) -> tuple[list[int], list[int]]:
    """Aligned sample positions for two fingerprint sequences of any length.

    Shared by video grouping, action revalidation, and the UI's similarity
    percentages so every comparison aligns frames the same way.
    """
    count = min(left_len, right_len)
    if count == 0:
        return [], []
    if count == 1:
        return [0], [0]
    left_indexes = [round(i * (left_len - 1) / (count - 1)) for i in range(count)]
    right_indexes = [round(i * (right_len - 1) / (count - 1)) for i in range(count)]
    return left_indexes, right_indexes


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
) -> tuple[str, str | None, int | None, int | None, float | None, str | None]:
    """Worker: (path, fingerprint, width, height, duration, error)."""
    try:
        fp, w, h, dur = compute_video_fingerprint(path, on_frame=on_frame)
        if not fp:
            return path, None, w, h, dur, f"{ERROR_VIDEO_FINGERPRINT_FAILED}: incomplete frames"
        return path, fp, w, h, dur, None
    except Exception as exc:
        return path, None, None, None, None, f"{ERROR_VIDEO_FINGERPRINT_FAILED}: {exc}"


def find_similar_video_groups(
    records: list[FileRecord],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    distinct: DistinctReviews | None = None,
    progress: ProgressCb | None = None,
    workers: int | None = None,
    cancelled: Callable[[], bool] | None = None,
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

    video_workers = resolve_workers(workers, cap=DEFAULT_VIDEO_WORKERS_CAP)

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
        by_path = {r.path: r for r in need}
        for record in need:
            # A failed upgrade must never leave a legacy fingerprint eligible
            # for clustering or persistence back into the cache.
            record.video_fingerprint = None

        def on_frame(frame: int, n_frames: int) -> None:
            with progress_lock:
                if progress:
                    progress(
                        "video-hash",
                        videos_done,
                        total,
                        f"Video hashing: {videos_done}/{total} · frame {frame}/{n_frames}",
                    )

        def job(path: str) -> tuple[str, str | None, int | None, int | None, float | None, str | None]:
            return _video_fingerprint_job(path, on_frame=on_frame)

        results = map_parallel(
            job,
            [r.path for r in need],
            workers=video_workers,
            # ffmpeg spawns real OS processes → threads buy true parallelism.
            backend="thread",
            progress=None,
            cancelled=cancelled,
        )
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

    if progress:
        progress("video-hash", videos_done, total, "")

    hashed = [r for r in videos if r.video_fingerprint]
    if len(hashed) < 2:
        return []

    adjacency: dict[str, set[str]] = {record.path: set() for record in hashed}
    fingerprints = [_fingerprint_hashes(record.video_fingerprint or "") for record in hashed]

    # Duration is already part of the match contract. Use its allowed ±10% range
    # as an exact candidate index instead of comparing every pair. Unlike a broad
    # Hamming BK-tree search, this remains quick with common black intro frames.
    known_durations = sorted(
        (record.duration, index)
        for index, record in enumerate(hashed)
        if record.duration is not None and record.duration > 0
    )
    duration_values = [duration for duration, _index in known_durations]
    unknown_duration_indexes = {
        index
        for index, record in enumerate(hashed)
        if record.duration is None or record.duration <= 0
    }
    max_frame_distance = max(threshold * 2, 4)

    for i, a in enumerate(hashed):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        if a.duration is not None and a.duration > 0:
            start = bisect_left(duration_values, a.duration * 0.9)
            end = bisect_right(duration_values, a.duration / 0.9)
            indexes = sorted(
                {
                    index
                    for _duration, index in known_durations[start:end]
                    if index > i
                }
                | {index for index in unknown_duration_indexes if index > i}
            )
        else:
            indexes = range(i + 1, len(hashed))
        left = fingerprints[i]
        for j in indexes:
            b = hashed[j]
            # Only pay for the lookup when reviews exist.
            if distinct and distinct.is_distinct(a, b):
                continue
            right = fingerprints[j]
            # These aligned positions are necessarily under the existing maximum.
            if (
                (left[0] ^ right[0]).bit_count() > max_frame_distance
                or (left[-1] ^ right[-1]).bit_count() > max_frame_distance
            ):
                continue
            left_indexes, right_indexes = aligned_position_indexes(
                len(left), len(right)
            )
            distances = [
                (left[li] ^ right[ri]).bit_count()
                for li, ri in zip(left_indexes, right_indexes, strict=True)
            ]
            if not distances:
                continue
            mean_distance = sum(distances) / len(distances)
            if mean_distance > threshold or max(distances) > max_frame_distance:
                continue
            # Near-identical: durations should be close when both known
            if a.duration and b.duration and a.duration > 0 and b.duration > 0:
                ratio = min(a.duration, b.duration) / max(a.duration, b.duration)
                if ratio < 0.9:
                    continue
            adjacency[a.path].add(b.path)
            adjacency[b.path].add(a.path)
        if progress and (i + 1) % 5 == 0:
            progress("video-cluster", i + 1, len(hashed), "")

    if progress:
        progress("video-cluster", len(hashed), len(hashed), "")

    return cluster_around_best(hashed, adjacency, distinct)


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
    frames: list[bytes] = []
    for start in range(0, len(timestamps), 12):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        chunk = timestamps[start:start + 12]
        frames.extend(_extract_hash_frames_once(path, chunk, (), cancelled=cancelled))
    if len(frames) != len(timestamps):
        raise ValueError("incomplete video version samples")
    return frames


def _version_cache_identity(record: FileRecord) -> str:
    stat = Path(record.path).stat()
    return json.dumps([
        VERSION_CACHE_VERSION, str(Path(record.path).resolve()), stat.st_dev, stat.st_ino,
        stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
    ], separators=(",", ":"))


def _version_samples(
    record: FileRecord, connection: sqlite3.Connection | None,
    cancelled: Callable[[], bool] | None,
    cache_errors: list[str] | None = None,
) -> list[tuple[float, int]]:
    import imagehash
    import numpy as np
    from PIL import Image

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
    duration = record.duration
    if not duration or not math.isfinite(duration):
        duration, record.width, record.height = probe_video(record.path)
        record.duration = duration
    if not duration or duration < VERSION_MIN_SECONDS:
        return []
    timestamps = _version_timestamps(duration)
    frames = _version_frame_batches(record.path, timestamps, cancelled)
    samples = []
    for timestamp, raw in zip(timestamps, frames, strict=True):
        # Blank frames and nearly uniform fades are not useful candidate evidence.
        if float(np.frombuffer(raw, dtype=np.uint8).std()) < 8:
            continue
        im = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), raw)
        samples.append((timestamp, int(str(imagehash.phash(im)), 16)))
    if connection is not None:
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


def _version_candidate_pairs(
    samples: list[list[tuple[float, int]]],
    cancelled: Callable[[], bool] | None = None,
) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """Index frame hashes once; avoid decoding or testing every pair of videos."""
    import pybktree

    occurrences: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for index, timeline in enumerate(samples):
        for timestamp, fingerprint in timeline:
            occurrences[fingerprint].append((index, timestamp))
    tree = pybktree.BKTree(lambda a, b: (a ^ b).bit_count(), occurrences)
    candidates: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for i, timeline in enumerate(samples):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        for ta, fingerprint in timeline:
            # Off-grid cuts shift samples by a fraction of a second. Candidate
            # lookup is deliberately looser than the aligned verification.
            matches = tree.find(fingerprint, 10)
            # Generic repeated title cards must not expand into an unbounded join.
            if sum(len(occurrences[h]) for _distance, h in matches) > 256:
                continue
            for _distance, matched in matches:
                for j, tb in occurrences[matched]:
                    if j > i:
                        candidates[(i, j)].append((ta, tb))
    return {
        pair: votes for pair, votes in candidates.items()
        if len({ta for ta, _tb in votes}) >= 3 and len({tb for _ta, tb in votes}) >= 3
    }


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
    left = _version_frame_batches(a.path, times, cancelled)
    right = _version_frame_batches(b.path, [t + offset for t in times], cancelled)
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
) -> str:
    """Conservative audio comparison of two distributed windows of the overlap.

    'same' means sampled audio agrees, not that complete tracks are identical.
    Decode failures stay 'unavailable' rather than becoming false differences.
    """
    import numpy as np
    from scipy.signal import correlate

    try:
        presence = [_audio_stream_present(path, cancelled) for path in (a, b)]
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
    del workers  # Analysis is intentionally serial and cancellable to bound ffmpeg RAM.
    videos = [r for r in records if r.media_type == MediaType.VIDEO]
    if len(videos) < 2 or not ffmpeg_available():
        return []
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
        samples = []
        for i, record in enumerate(videos):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            try:
                samples.append(_version_samples(record, connection, cancelled, cache_errors))
            except InterruptedError:
                raise
            except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
                samples.append([])
                if errors is not None:
                    errors.append(f"video version analysis failed: {record.path}: {exc}")
            if cache_errors and connection is not None:
                # One failed cache operation disables it for the remainder of
                # this scan. Already extracted samples remain usable.
                connection.close()
                connection = None
            if progress:
                progress("video-versions", i + 1, len(videos), "Finding video excerpts")
        matches = []
        pairs = _version_candidate_pairs(samples, cancelled)
        for (i, j), votes in pairs.items():
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            a, b = videos[i], videos[j]
            if distinct and distinct.is_distinct(a, b):
                continue
            step = max(float(a.duration or 0), float(b.duration or 0)) / VERSION_SAMPLE_LIMIT
            offsets = _offset_candidates(votes, max(0.25, step))
            evidence = None
            for offset in offsets:
                try:
                    evidence = _verify_video_offset(a, b, offset, cancelled)
                except InterruptedError:
                    raise
                except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
                    continue
                if evidence:
                    break
            if evidence is None:
                continue
            evidence["audio"] = compare_video_audio(a.path, b.path, evidence["ranges"], cancelled=cancelled)
            evidence["audio_scope"] = "sampled overlap"
            evidence["manual_review"] = True
            # Same-footage pairs with agreeing audio retain the standard matcher.
            if evidence["relation"] == "same_footage" and evidence["audio"] in {"same", "none"}:
                continue
            matches.append(([a, b], evidence))
        return matches
    finally:
        if connection is not None:
            connection.close()
        if cache_errors and errors is not None:
            errors.append(f"video version cache unavailable; continued uncached: {cache_errors[0]}")
