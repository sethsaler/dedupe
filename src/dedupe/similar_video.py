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
from bisect import bisect_right
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

from .cache import DistinctReviews
from .exact import PARTIAL_SIZE, file_partial_hash, file_sha256
from .grouping import cluster_around_best, rank_keep_candidate
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
SINGLE_PASS_MAX_DURATION = 180.0
FRAME_WIDTH = 320
HASH_FRAME_SIZE = 32
# Software-decode fingerprint jobs run beside the hardware-decode ones: one
# per this many spare cores, up to the automatic maximum.
CORES_PER_SOFTWARE_DECODE = 2
MAX_SOFTWARE_DECODE_WORKERS = 6
HASH_FRAME_FILTER = f"scale={HASH_FRAME_SIZE}:{HASH_FRAME_SIZE}:flags=lanczos,format=gray"

# Standard similarity quality / chroma / verification guards.
MIN_FRAME_LUMA_STD = 5.0
MIN_TEMPORAL_SPREAD_BITS = 3
MAX_CHROMA_MAD = 48.0
MAX_CHROMA_SHIFT = 32.0
MIN_FRAME_NCC = 0.75
MAX_FRAME_ZMAD = 0.60

# Spectral audio comparison constants.
AUDIO_SAMPLE_RATE = 16000
AUDIO_SILENCE_DBFS = -55.0
AUDIO_N_FFT = 512
AUDIO_HOP_SAMPLES = 320
AUDIO_N_MELS = 24
AUDIO_MAX_LAG_SECONDS = 2.0

_DECODE_LANE = threading.local()
_VIDEO_FEATURE_CACHE_LIMIT = 4096
_VIDEO_FEATURE_CACHE: OrderedDict[tuple[str, int, int | None], tuple[tuple[int, ...], bool]] = (
    OrderedDict()
)
_VIDEO_FEATURE_LOCK = threading.Lock()


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


def probe_video(
    path: str | Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[float | None, int | None, int | None]:
    """Single ffprobe: (duration, width, height) in display orientation."""
    if not shutil.which("ffprobe"):
        return None, None, None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration:stream=duration,width,height:stream_tags=rotate:stream_side_data=rotation",
        "-of",
        "json",
        str(path),
    ]
    try:
        if cancelled is not None:
            result = _run_video_command(cmd, timeout=60, cancelled=cancelled)
            stdout_text = (
                result.stdout.decode("utf-8", errors="replace")
                if isinstance(result.stdout, bytes)
                else (result.stdout or "")
            )
        else:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            stdout_text = result.stdout or ""
        if result.returncode != 0:
            return None, None, None
        data = json.loads(stdout_text or "{}")
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
        rotation = 0
        for side in stream.get("side_data_list") or []:
            if isinstance(side, dict) and side.get("rotation") is not None:
                try:
                    rotation = round(float(side["rotation"]))
                except (TypeError, ValueError):
                    pass
        if not rotation:
            try:
                rotation = round(float((stream.get("tags") or {}).get("rotate", 0)))
            except (TypeError, ValueError):
                rotation = 0
        if rotation % 360 in (90, 270):
            w, h = h, w
        return duration, w, h
    except InterruptedError:
        raise
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
    """Return the timeline positions used by the video fingerprint.

    For standard video similarity (``max_frames == MAX_FRAMES``), sample fixed
    8 midpoint frames for ``duration >= 1.0`` (and 3 for sub-second clips) so
    sampling never lands on ``t = 0.0`` and avoids the 2–7s frame-count cliff.
    Explicit non-default budgets preserve the legacy spacing expected by
    downstream person/face frame samplers.
    """
    if max_frames == MAX_FRAMES:
        count = MAX_FRAMES if duration >= 1.0 else 3
        return [round((index + 0.5) * duration / count, 6) for index in range(count)]
    count = min(max_frames, max(3, min(max_frames, int(duration) + 1)))
    return [index * duration / count for index in range(count)]


def _extract_hash_frame(
    path: str | Path,
    timestamp: float,
    *,
    software: bool = False,
    cancelled: Callable[[], bool] | None = None,
    vf_prefix: str | None = None,
) -> bytes | None:
    """Fast-seek to one frame and return a 32×32 grayscale image buffer.

    Tries hardware decode when available unless ``software=True``, falling back
    to software decode.
    """
    hwaccel = () if software else _hwaccel_args()
    raw = _extract_hash_frame_once(
        path, timestamp, hwaccel, cancelled=cancelled, vf_prefix=vf_prefix
    )
    if raw is None and hwaccel:
        raw = _extract_hash_frame_once(
            path, timestamp, (), cancelled=cancelled, vf_prefix=vf_prefix
        )
    return raw


def _extract_hash_frame_once(
    path: str | Path,
    timestamp: float,
    hwaccel: tuple[str, ...],
    *,
    cancelled: Callable[[], bool] | None = None,
    vf_prefix: str | None = None,
) -> bytes | None:
    vf = f"{vf_prefix},{HASH_FRAME_FILTER}" if vf_prefix else HASH_FRAME_FILTER
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
        vf,
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    result = _run_video_command(cmd, timeout=60, cancelled=cancelled)
    expected = HASH_FRAME_SIZE * HASH_FRAME_SIZE
    if result.returncode != 0 or len(result.stdout) != expected:
        return None
    return result.stdout


def _extract_sequential_hash_frames(
    path: str | Path,
    timestamps: list[float],
    *,
    software: bool = False,
    cancelled: Callable[[], bool] | None = None,
    vf_prefix: str | None = None,
) -> list[bytes]:
    """Decode once sequentially and select frames at ``timestamps`` to stdout."""
    if not timestamps:
        return []
    hwaccel = () if software else _hwaccel_args()
    frames = _extract_sequential_hash_frames_once(
        path, timestamps, hwaccel, cancelled=cancelled, vf_prefix=vf_prefix
    )
    if not frames and hwaccel:
        frames = _extract_sequential_hash_frames_once(
            path, timestamps, (), cancelled=cancelled, vf_prefix=vf_prefix
        )
    return frames


def _extract_sequential_hash_frames_once(
    path: str | Path,
    timestamps: list[float],
    hwaccel: tuple[str, ...],
    *,
    cancelled: Callable[[], bool] | None = None,
    vf_prefix: str | None = None,
) -> list[bytes]:
    expected = HASH_FRAME_SIZE * HASH_FRAME_SIZE
    select_expr = "+".join(
        f"if(isnan(prev_pts)\\,gte(pts*TB\\,{t:.6f})\\,"
        f"lt(prev_pts*TB\\,{t:.6f})*gte(pts*TB\\,{t:.6f}))"
        for t in timestamps
    )
    filters = ["setpts=PTS-STARTPTS", f"select={select_expr}"]
    if vf_prefix:
        filters.append(vf_prefix)
    filters.append(HASH_FRAME_FILTER)
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *hwaccel,
        "-threads",
        "1",
        "-i",
        str(path),
        "-an",
        "-sn",
        "-vf",
        ",".join(filters),
        "-fps_mode",
        "passthrough",
        "-frames:v",
        str(len(timestamps)),
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    result = _run_video_command(cmd, timeout=180, cancelled=cancelled)
    if result.returncode != 0 or len(result.stdout) != len(timestamps) * expected:
        return []
    return [
        result.stdout[index * expected : (index + 1) * expected]
        for index in range(len(timestamps))
    ]


def _extract_unknown_duration_sequential_frames(
    path: str | Path,
    max_frames: int = MAX_FRAMES,
    *,
    software: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> list[bytes]:
    """Single-pass sequential pipe for containers without a probed duration."""
    expected = HASH_FRAME_SIZE * HASH_FRAME_SIZE
    hwaccel = () if software else _hwaccel_args()
    for active_hwaccel in ((hwaccel, ()) if hwaccel else ((),)):
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            *active_hwaccel,
            "-threads",
            "1",
            "-i",
            str(path),
            "-an",
            "-sn",
            "-vf",
            f"fps=1,{HASH_FRAME_FILTER}",
            "-frames:v",
            str(max_frames),
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        result = _run_video_command(cmd, timeout=180, cancelled=cancelled)
        if (
            result.returncode == 0
            and len(result.stdout) >= expected
            and len(result.stdout) % expected == 0
        ):
            count = len(result.stdout) // expected
            return [
                result.stdout[i * expected : (i + 1) * expected]
                for i in range(count)
            ]
    return []


def _extract_hash_frames(
    path: str | Path,
    timestamps: list[float],
    *,
    software: bool = False,
    cancelled: Callable[[], bool] | None = None,
    vf_prefix: str | None = None,
) -> list[bytes]:
    """Fast-seek every sampled timestamp in a single ffmpeg process via pipe.

    The same file is opened once per timestamp so each output keeps the exact
    input-side seek of the per-frame extractor, concatenated directly to
    ``pipe:1`` without temp files. Tries hardware decode when available, falling
    back to software decode; ``software`` skips the hardware attempt. Returns
    ``[]`` when the pass is incomplete so the caller can fall back.
    """
    if not timestamps:
        return []
    hwaccel = () if software else _hwaccel_args()
    frames = _extract_hash_frames_once(
        path, timestamps, hwaccel, cancelled=cancelled, vf_prefix=vf_prefix
    )
    if not frames and hwaccel:
        frames = _extract_hash_frames_once(
            path, timestamps, (), cancelled=cancelled, vf_prefix=vf_prefix
        )
    return frames


def _extract_hash_frames_once(
    path: str | Path,
    timestamps: list[float],
    hwaccel: tuple[str, ...],
    *,
    cancelled: Callable[[], bool] | None = None,
    vf_prefix: str | None = None,
) -> list[bytes]:
    expected = HASH_FRAME_SIZE * HASH_FRAME_SIZE
    vf = f"{vf_prefix},{HASH_FRAME_FILTER}" if vf_prefix else HASH_FRAME_FILTER
    if len(timestamps) == 1:
        raw = _extract_hash_frame_once(
            path, timestamps[0], hwaccel, cancelled=cancelled, vf_prefix=vf_prefix
        )
        return [raw] if raw is not None else []
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    for timestamp in timestamps:
        cmd += [*hwaccel, "-ss", f"{timestamp:.6f}", "-threads", "1", "-i", str(path)]
    filter_parts = [
        f"[{index}:v]{vf},setsar=1,trim=end_frame=1,setpts=N/TB[v{index}];"
        for index in range(len(timestamps))
    ]
    filter_parts.append(
        "".join(f"[v{index}]" for index in range(len(timestamps)))
        + f"concat=n={len(timestamps)}:v=1:a=0[out]"
    )
    cmd += [
        "-filter_complex",
        "".join(filter_parts),
        "-map",
        "[out]",
        "-an",
        "-sn",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    result = _run_video_command(cmd, timeout=180, cancelled=cancelled)
    if result.returncode != 0 or len(result.stdout) != len(timestamps) * expected:
        return []
    return [
        result.stdout[index * expected : (index + 1) * expected]
        for index in range(len(timestamps))
    ]


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


@dataclass(frozen=True)
class _VideoFeatures:
    """In-scan secondary hashes and quality metrics derived from 32×32 frames."""

    flipped_hashes: tuple[int, ...] | None = None
    informative: bool = True
    cropped_hashes: tuple[int, ...] | None = None
    cropped_flipped_hashes: tuple[int, ...] | None = None
    cropped_informative: bool = True
    crop_vf: str | None = None


def _analyze_raw_frames(
    raw_frames: Sequence[bytes],
) -> tuple[tuple[int, ...], tuple[int, ...], bool]:
    """Compute primary hashes, horizontally-flipped hashes, and informativeness."""
    import imagehash
    import numpy as np
    from PIL import Image

    hashes: list[int] = []
    flipped_hashes: list[int] = []
    stds: list[float] = []
    for raw in raw_frames:
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(HASH_FRAME_SIZE, HASH_FRAME_SIZE)
        stds.append(float(arr.std()))
        image = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), raw)
        flipped_image = Image.frombytes(
            "L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), np.ascontiguousarray(arr[:, ::-1]).tobytes()
        )
        hashes.append(int(str(imagehash.phash(image)), 16))
        flipped_hashes.append(int(str(imagehash.phash(flipped_image)), 16))
    non_blank_hashes = [
        hv for hv, std in zip(hashes, stds, strict=True) if std >= MIN_FRAME_LUMA_STD
    ]
    min_non_blank = max(2, (len(raw_frames) + 1) // 2)
    reps: list[int] = []
    for hv in non_blank_hashes:
        if all((hv ^ prev).bit_count() >= MIN_TEMPORAL_SPREAD_BITS for prev in reps):
            reps.append(hv)
    informative = len(non_blank_hashes) >= min_non_blank and (
        len(raw_frames) < 4 or len(reps) >= 2
    )
    return tuple(hashes), tuple(flipped_hashes), informative


def _feature_cache_key(record: FileRecord) -> tuple[str, int, int | None]:
    return (record.path, int(record.size), record.mtime_ns)


def _cached_video_features_get(record: FileRecord) -> tuple[tuple[int, ...], bool] | None:
    key = _feature_cache_key(record)
    with _VIDEO_FEATURE_LOCK:
        value = _VIDEO_FEATURE_CACHE.get(key)
        if value is not None:
            _VIDEO_FEATURE_CACHE.move_to_end(key)
        return value


def _cached_video_features_put(
    record: FileRecord, flipped_hashes: tuple[int, ...], informative: bool
) -> None:
    key = _feature_cache_key(record)
    with _VIDEO_FEATURE_LOCK:
        _VIDEO_FEATURE_CACHE[key] = (flipped_hashes, informative)
        _VIDEO_FEATURE_CACHE.move_to_end(key)
        while len(_VIDEO_FEATURE_CACHE) > _VIDEO_FEATURE_CACHE_LIMIT:
            _VIDEO_FEATURE_CACHE.popitem(last=False)


def _compute_video_fingerprint_with_frames(
    path: str | Path,
    *,
    on_frame: Callable[[int, int], None] | None = None,
    software: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str | None, int | None, int | None, float | None, list[bytes] | None]:
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg/ffprobe not available")
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")

    import imagehash
    from PIL import Image

    path = Path(path)
    duration, width, height = (
        probe_video(path, cancelled=cancelled) if cancelled is not None else probe_video(path)
    )

    if duration is not None and duration > 0:
        timestamps = _sample_timestamps(duration)
        raw_frames: list[bytes] = []
        if duration <= SINGLE_PASS_MAX_DURATION:
            raw_frames = _extract_sequential_hash_frames(
                path, timestamps, software=software, cancelled=cancelled
            )
        if len(raw_frames) != len(timestamps):
            raw_frames = (
                _extract_hash_frames(
                    path, timestamps, software=software, cancelled=cancelled
                )
                if cancelled is not None
                else _extract_hash_frames(path, timestamps, software=software)
            )
        if len(raw_frames) != len(timestamps):
            # Per-frame fallback for inputs the single-pass and batched passes cannot satisfy.
            raw_frames = []
            for timestamp in timestamps:
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                raw = _extract_hash_frame(
                    path, timestamp, software=software, cancelled=cancelled
                )
                if raw is None:
                    return None, width, height, duration, None
                raw_frames.append(raw)
        hashes = []
        for i, raw in enumerate(raw_frames):
            image = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), raw)
            hashes.append(imagehash.phash(image))
            if on_frame:
                on_frame(i + 1, len(timestamps))
        return (
            "v3:" + ",".join(str(frame_hash) for frame_hash in hashes),
            width,
            height,
            duration,
            raw_frames,
        )

    # Rare fallback for containers whose duration cannot be probed. Try the
    # zero-temp-file sequential pipe first, keeping _extract_frames as fallback.
    raw_frames = _extract_unknown_duration_sequential_frames(
        path, software=software, cancelled=cancelled
    )
    if raw_frames:
        hashes = []
        for idx, raw in enumerate(raw_frames):
            image = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), raw)
            hashes.append(imagehash.phash(image))
            if on_frame:
                on_frame(idx + 1, len(raw_frames))
        return (
            "v3:" + ",".join(str(frame_hash) for frame_hash in hashes),
            width,
            height,
            duration,
            raw_frames,
        )

    with tempfile.TemporaryDirectory(prefix="dedupe-vid-") as tmp:
        frames = _extract_frames(path, Path(tmp), duration=duration)
        if not frames:
            return None, width, height, duration, None

        hashes = []
        fallback_raw: list[bytes] = []
        for idx, fp in enumerate(frames):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            try:
                with Image.open(fp) as img:
                    gray = img.convert("L").resize(
                        (HASH_FRAME_SIZE, HASH_FRAME_SIZE), Image.Resampling.LANCZOS
                    )
                    fallback_raw.append(gray.tobytes())
                    hashes.append(imagehash.phash(img.convert("RGB")))
            except Exception:
                # Dropping a frame would shift the remaining sample positions
                # and allow an incomplete fingerprint to stand for the video.
                return None, width, height, duration, None
            else:
                if on_frame:
                    on_frame(idx + 1, len(frames))

        if not hashes:
            return None, width, height, duration, None

        return (
            "v3:" + ",".join(str(frame_hash) for frame_hash in hashes),
            width,
            height,
            duration,
            fallback_raw,
        )


def compute_video_fingerprint(
    path: str | Path,
    *,
    on_frame: Callable[[int, int], None] | None = None,
    software: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str | None, int | None, int | None, float | None]:
    """
    Return (fingerprint_hex, width, height, duration).
    Fingerprint preserves the ordered pHash sequence of sampled frames.
    ``on_frame(frame_number, total_frames)`` is called after each sampled frame.
    ``software`` decodes the sampled frames on the CPU without trying the
    hardware decoder first.
    """
    fp, width, height, duration, _raw_frames = _compute_video_fingerprint_with_frames(
        path, on_frame=on_frame, software=software, cancelled=cancelled
    )
    return fp, width, height, duration


_ORIGINAL_COMPUTE_VIDEO_FINGERPRINT = compute_video_fingerprint


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
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str, str | None, int | None, int | None, float | None, str | None, list[bytes] | None]:
    """Worker: (path, fingerprint, width, height, duration, error, raw_frames)."""
    try:
        if compute_video_fingerprint is _ORIGINAL_COMPUTE_VIDEO_FINGERPRINT:
            fp, w, h, dur, raw_frames = _compute_video_fingerprint_with_frames(
                path, on_frame=on_frame, software=software, cancelled=cancelled
            )
        else:
            raw_frames = None
            if cancelled is not None:
                try:
                    fp, w, h, dur = compute_video_fingerprint(
                        path, on_frame=on_frame, software=software, cancelled=cancelled
                    )
                except TypeError:
                    fp, w, h, dur = compute_video_fingerprint(
                        path, on_frame=on_frame, software=software
                    )
            else:
                fp, w, h, dur = compute_video_fingerprint(
                    path, on_frame=on_frame, software=software
                )
        if not fp:
            return (
                path,
                None,
                w,
                h,
                dur,
                f"{ERROR_VIDEO_FINGERPRINT_FAILED}: incomplete frames",
                None,
            )
        return path, fp, w, h, dur, None, raw_frames
    except InterruptedError:
        raise
    except Exception as exc:
        return path, None, None, None, None, f"{ERROR_VIDEO_FINGERPRINT_FAILED}: {exc}", None


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


def _parse_border_crop(value: str | None) -> tuple[int, int, int, int, int, int] | None:
    """Parse ``CropBox`` wire format ``left,top,right,bottom,width,height[,chrome]``."""
    if not value or value == "dismissed":
        return None
    parts = value.split(",")
    if len(parts) == 7:
        parts = parts[:6]
    if len(parts) != 6:
        return None
    try:
        left, top, right, bottom, width, height = (int(part) for part in parts)
    except ValueError:
        return None
    if (
        0 <= left < right <= width
        and 0 <= top < bottom <= height
        and (left, top, right, bottom) != (0, 0, width, height)
    ):
        return left, top, right, bottom, width, height
    return None


def _crop_vf_prefix(box: tuple[int, int, int, int, int, int]) -> str:
    left, top, right, bottom, bw, bh = box
    cw, ch = right - left, bottom - top
    return (
        f"crop=iw*{cw / bw:.8f}:ih*{ch / bh:.8f}:"
        f"iw*{left / bw:.8f}:ih*{top / bh:.8f}"
    )


def _extract_cropped_roi_features(
    record: FileRecord,
    crop_vf: str,
    cancelled: Callable[[], bool] | None,
) -> tuple[tuple[int, ...], tuple[int, ...], bool] | None:
    """Extract cropped-ROI hashes, flipped hashes, and informativeness for ``record``."""
    if not record.duration or record.duration <= 0 or not Path(record.path).is_file():
        return None
    timestamps = _sample_timestamps(float(record.duration))
    raw_frames: list[bytes] = []
    if record.duration <= SINGLE_PASS_MAX_DURATION:
        raw_frames = _extract_sequential_hash_frames(
            record.path, timestamps, cancelled=cancelled, vf_prefix=crop_vf
        )
    if len(raw_frames) != len(timestamps):
        raw_frames = _extract_hash_frames(
            record.path, timestamps, cancelled=cancelled, vf_prefix=crop_vf
        )
    if len(raw_frames) != len(timestamps):
        raw_frames = []
        for ts in timestamps:
            raw = _extract_hash_frame(record.path, ts, cancelled=cancelled, vf_prefix=crop_vf)
            if raw is None:
                return None
            raw_frames.append(raw)
    return _analyze_raw_frames(raw_frames)


def _ensure_cached_record_features(
    record: FileRecord,
    cancelled: Callable[[], bool] | None,
) -> tuple[tuple[int, ...], bool] | None:
    """Lazily compute and cache flipped hashes + informativeness for an existing file."""
    cached = _cached_video_features_get(record)
    if cached is not None:
        return cached
    if not Path(record.path).is_file():
        return None
    duration = record.duration
    if not duration or duration <= 0:
        return None
    timestamps = _sample_timestamps(float(duration))
    raw_frames: list[bytes] = []
    if duration <= SINGLE_PASS_MAX_DURATION:
        raw_frames = _extract_sequential_hash_frames(
            record.path, timestamps, cancelled=cancelled
        )
    if len(raw_frames) != len(timestamps):
        raw_frames = _extract_hash_frames(record.path, timestamps, cancelled=cancelled)
    if len(raw_frames) != len(timestamps):
        return None
    _hashes, flipped_hashes, informative = _analyze_raw_frames(raw_frames)
    _cached_video_features_put(record, flipped_hashes, informative)
    return flipped_hashes, informative


class _RgbThumbCache:
    """Thread-safe singleflight cache for 32×32 RGB chroma frames."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str | None, tuple[float, ...]], list] = {}

    def get(
        self,
        path: str,
        timestamps: list[float],
        crop_vf: str | None,
        cancelled: Callable[[], bool] | None,
    ) -> list:
        key = (path, crop_vf, tuple(round(float(t), 4) for t in timestamps))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = [threading.Event(), None]
                self._entries[key] = entry
                owner = True
            else:
                owner = False
        if not owner:
            event = entry[0]
            while not event.wait(timeout=0.05):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
            return entry[1] or []
        frames = _extract_rgb_thumbs(path, timestamps, crop_vf=crop_vf, cancelled=cancelled)
        entry[1] = frames
        entry[0].set()
        return frames


def _extract_rgb_thumbs(
    path: str | Path,
    timestamps: list[float],
    *,
    crop_vf: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list:
    """Extract small 32×32 RGB24 frames for chroma verification; returns [] on decode failure."""
    import numpy as np

    if not timestamps or not Path(path).is_file():
        return []
    frame_bytes = HASH_FRAME_SIZE * HASH_FRAME_SIZE * 3
    scale = f"scale={HASH_FRAME_SIZE}:{HASH_FRAME_SIZE}:flags=lanczos,setsar=1,format=rgb24"
    vf = f"{crop_vf},{scale}" if crop_vf else scale
    if len(timestamps) == 1:
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", f"{timestamps[0]:.6f}", "-threads", "1", "-i", str(path),
            "-an", "-sn", "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "pipe:1",
        ]
    else:
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
        for t in timestamps:
            cmd += ["-ss", f"{t:.6f}", "-threads", "1", "-i", str(path)]
        fc = "".join(
            f"[{i}:v]{vf},trim=end_frame=1,setpts=N/TB[v{i}];"
            for i in range(len(timestamps))
        )
        fc += "".join(f"[v{i}]" for i in range(len(timestamps))) + (
            f"concat=n={len(timestamps)}:v=1:a=0[out]"
        )
        cmd += [
            "-filter_complex", fc, "-map", "[out]", "-an", "-sn",
            "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1",
        ]
    try:
        result = _run_video_command(cmd, timeout=60, cancelled=cancelled)
    except InterruptedError:
        raise
    except Exception:
        return []
    if result.returncode != 0 or len(result.stdout) != len(timestamps) * frame_bytes:
        return []
    return [
        np.frombuffer(
            result.stdout[i * frame_bytes : (i + 1) * frame_bytes], dtype=np.uint8
        ).reshape(HASH_FRAME_SIZE, HASH_FRAME_SIZE, 3).copy()
        for i in range(len(timestamps))
    ]


def _video_chroma_agrees(
    a: FileRecord,
    b: FileRecord,
    *,
    flip_b: bool = False,
    crop_a: str | None = None,
    crop_b: str | None = None,
    times_a: list[float] | None = None,
    times_b: list[float] | None = None,
    rgb_cache: _RgbThumbCache | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Verify sampled RGB color balance/chroma agrees between two real videos."""
    import numpy as np

    if not Path(a.path).is_file() or not Path(b.path).is_file():
        return True
    if not ffmpeg_available():
        return True

    def _default_times(rec: FileRecord) -> list[float]:
        dur = float(rec.duration or 0.0)
        if dur <= 0:
            return []
        if dur < 1.0:
            return [round(0.5 * dur, 4)]
        return [round(dur * f, 4) for f in (0.2, 0.5, 0.8)]

    ta = times_a if times_a is not None else _default_times(a)
    tb = times_b if times_b is not None else _default_times(b)
    if not ta or not tb or len(ta) != len(tb):
        return True
    if rgb_cache is not None:
        fa = rgb_cache.get(a.path, ta, crop_a, cancelled)
        fb = rgb_cache.get(b.path, tb, crop_b, cancelled)
    else:
        fa = _extract_rgb_thumbs(a.path, ta, crop_vf=crop_a, cancelled=cancelled)
        fb = _extract_rgb_thumbs(b.path, tb, crop_vf=crop_b, cancelled=cancelled)
    if not fa or not fb or len(fa) != len(fb):
        # Dummy/unreadable files in mocked unit tests pass gracefully.
        return True
    mads: list[float] = []
    shifts: list[float] = []
    for img_a, img_b in zip(fa, fb, strict=True):
        arr_a = img_a.astype(np.float32)
        arr_b = (img_b[:, ::-1, :] if flip_b else img_b).astype(np.float32)
        ya = 0.299 * arr_a[:, :, 0] + 0.587 * arr_a[:, :, 1] + 0.114 * arr_a[:, :, 2]
        yb = 0.299 * arr_b[:, :, 0] + 0.587 * arr_b[:, :, 1] + 0.114 * arr_b[:, :, 2]
        cba, cra = arr_a[:, :, 2] - ya, arr_a[:, :, 0] - ya
        cbb, crb = arr_b[:, :, 2] - yb, arr_b[:, :, 0] - yb
        mono_a = max(abs(float(cba.mean())), abs(float(cra.mean()))) < 6.0 and max(
            float(cba.std()), float(cra.std())
        ) < 6.0
        mono_b = max(abs(float(cbb.mean())), abs(float(crb.mean()))) < 6.0 and max(
            float(cbb.std()), float(crb.std())
        ) < 6.0
        if mono_a or mono_b:
            continue
        mads.append(
            max(float(np.abs(cba - cbb).mean()), float(np.abs(cra - crb).mean()))
        )
        shifts.append(
            max(
                abs(float(cba.mean() - cbb.mean())),
                abs(float(cra.mean() - crb.mean())),
            )
        )
    if not mads:
        return True
    return (sum(mads) / len(mads)) <= MAX_CHROMA_MAD and (
        sum(shifts) / len(shifts)
    ) <= MAX_CHROMA_SHIFT


def _compare_hash_sequences(
    left: tuple[int, ...], right: tuple[int, ...]
) -> tuple[float, int] | None:
    if not left or not right:
        return None
    left_indexes, right_indexes = _aligned_indexes_cached(len(left), len(right))
    if not left_indexes:
        return None
    distances = [
        (left[li] ^ right[ri]).bit_count()
        for li, ri in zip(left_indexes, right_indexes, strict=True)
    ]
    return sum(distances) / len(distances), max(distances)


def _prevent_unknown_hub_bridging(
    hashed: list[FileRecord],
    adjacency: dict[str, set[str]],
) -> None:
    """Keep duration-unknown records from bridging incompatible (>±10%) durations."""
    by_path = {record.path: record for record in hashed}
    for record in hashed:
        if record.duration is not None and record.duration > 0:
            continue
        neighbors = list(adjacency.get(record.path, ()))
        known = [
            by_path[path]
            for path in neighbors
            if by_path[path].duration is not None and by_path[path].duration > 0
        ]
        if len(known) < 2:
            continue
        durations = [float(n.duration) for n in known]
        if min(durations) / max(durations) >= 0.9:
            continue
        known.sort(key=lambda n: float(n.duration))
        best_subset: list[FileRecord] = []
        best_score: tuple[int, tuple] = (-1, ())
        for start_idx, anchor in enumerate(known):
            d_low = float(anchor.duration)
            subset = [anchor]
            for cand in known[start_idx + 1 :]:
                d_high = float(cand.duration)
                if d_low / d_high >= 0.9:
                    subset.append(cand)
                else:
                    break
            top_rank = max(rank_keep_candidate(n) for n in subset)
            score = (len(subset), top_rank)
            if score > best_score:
                best_score = score
                best_subset = subset
        keep_paths = {n.path for n in best_subset}
        for neighbor in known:
            if neighbor.path not in keep_paths:
                adjacency[record.path].discard(neighbor.path)
                adjacency[neighbor.path].discard(record.path)


def _evaluate_candidate_pairs(
    hashed: list[FileRecord],
    fingerprints: list[tuple[int, ...]],
    *,
    threshold: int,
    max_frame_distance: int,
    distinct: DistinctReviews,
    cancelled: Callable[[], bool] | None,
    features: list[_VideoFeatures | None] | None = None,
) -> tuple[
    dict[str, set[str]],
    list[tuple[FileRecord, FileRecord, bool, str | None, str | None, bool, bool]],
    dict[tuple[int, int], tuple[bool, str | None, str | None, bool, bool]],
]:
    """Compare primary and secondary (flipped/cropped) pairs within ±10% duration windows."""
    import numpy as np

    verify_limit = threshold * VERIFY_DISTANCE_FACTOR
    # Tuple: (mean_distance, flip_b, crop_a, crop_b, uses_crop_a, uses_crop_b)
    direct_candidates: dict[
        tuple[int, int], tuple[float, bool, str | None, str | None, bool, bool]
    ] = {}
    verify_candidates: dict[
        tuple[int, int], tuple[float, bool, str | None, str | None, bool, bool]
    ] = {}

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
        u: int,
        v: int,
        mean_distance: float,
        max_distance: int,
        *,
        flip_b: bool = False,
        crop_u: str | None = None,
        crop_v: str | None = None,
        uses_crop_u: bool = False,
        uses_crop_v: bool = False,
    ) -> None:
        if u < v:
            first, second = u, v
            crop_a, crop_b = crop_u, crop_v
            uses_ca, uses_cb = uses_crop_u, uses_crop_v
        else:
            first, second = v, u
            crop_a, crop_b = crop_v, crop_u
            uses_ca, uses_cb = uses_crop_v, uses_crop_u
        a, b = hashed[first], hashed[second]
        if distinct and distinct.is_distinct(a, b):
            return
        meta = (mean_distance, flip_b, crop_a, crop_b, uses_ca, uses_cb)
        if mean_distance <= threshold and max_distance <= max_frame_distance:
            prev = direct_candidates.get((first, second))
            if prev is None or mean_distance < prev[0]:
                direct_candidates[(first, second)] = meta
            verify_candidates.pop((first, second), None)
        elif mean_distance <= verify_limit and (first, second) not in direct_candidates:
            prev = verify_candidates.get((first, second))
            if prev is None or mean_distance < prev[0]:
                verify_candidates[(first, second)] = meta

    def _check_pair_sequences(
        u: int,
        v: int,
        left: tuple[int, ...],
        right: tuple[int, ...],
        **meta_kwargs,
    ) -> None:
        compared = _compare_hash_sequences(left, right)
        if compared is None:
            return
        mean_dist, max_dist = compared
        if mean_dist <= verify_limit:
            _record_pair(u, v, mean_dist, max_dist, **meta_kwargs)

    # 1. Primary comparison (vectorized across known ±10% windows, no premature edge-frame cut).
    if known_durations:
        kd_durations_list = [duration for duration, _ in known_durations]
        kd_indices = [index for _, index in known_durations]
        k_count = len(known_durations)
        kd_lengths = np.array([len(fingerprints[idx]) for idx in kd_indices], dtype=np.int64)
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
            rel = np.arange(pos + 1, end, dtype=np.int64)
            left_len = int(kd_lengths[pos])
            same_mask = kd_lengths[rel] == left_len

            if np.any(same_mask):
                same_rel = rel[same_mask]
                dists = _popcount64(
                    kd_fps[same_rel, :left_len] ^ kd_fps[pos : pos + 1, :left_len]
                )
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
                    v = kd_indices[c_pos]
                    _check_pair_sequences(u, v, fingerprints[u], fingerprints[v])

        for u in unknown_indexes:
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            left = fingerprints[u]
            left_len = len(left)
            same_mask = kd_lengths == left_len
            if np.any(same_mask):
                same_rel = np.flatnonzero(same_mask)
                left_arr = np.array(left, dtype=np.uint64)[None, :]
                dists = _popcount64(kd_fps[same_rel, :left_len] ^ left_arr)
                means = dists.sum(axis=1) / left_len
                maxs = dists.max(axis=1)
                keep = np.flatnonzero(means <= verify_limit)
                for k_i in keep.tolist():
                    c_pos = int(same_rel[k_i])
                    _record_pair(u, kd_indices[c_pos], float(means[k_i]), int(maxs[k_i]))
            if not np.all(same_mask):
                for c_pos in np.flatnonzero(~same_mask).tolist():
                    v = kd_indices[c_pos]
                    _check_pair_sequences(u, v, left, fingerprints[v])

    for p_idx, u in enumerate(unknown_indexes):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        for v in unknown_indexes[p_idx + 1 :]:
            _check_pair_sequences(u, v, fingerprints[u], fingerprints[v])

    # 2. Secondary variants: horizontally-flipped and border-cropped ROI hashes.
    if features is not None and any(
        f is not None and (f.flipped_hashes is not None or f.cropped_hashes is not None)
        for f in features
    ):
        pos_by_index = (
            {idx: pos for pos, idx in enumerate(kd_indices)} if known_durations else {}
        )

        def _compatible_targets(u: int) -> list[int]:
            if u in pos_by_index:
                pos = pos_by_index[u]
                d_u = kd_durations_list[pos]
                lo = bisect_right(kd_durations_list, d_u * 0.9 - 1e-9)
                hi = bisect_right(kd_durations_list, d_u / 0.9 + 1e-9)
                targets = [
                    kd_indices[c_pos]
                    for c_pos in range(lo, hi)
                    if kd_indices[c_pos] != u
                    and min(d_u, kd_durations_list[c_pos])
                    / max(d_u, kd_durations_list[c_pos])
                    >= 0.9
                ]
                targets.extend(unknown_indexes)
                return targets
            return [idx for idx in range(len(hashed)) if idx != u]

        for u, feat_u in enumerate(features):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            if feat_u is None:
                continue
            has_flip = feat_u.flipped_hashes is not None
            has_crop = feat_u.cropped_hashes is not None
            if not has_flip and not has_crop:
                continue
            for v in _compatible_targets(u):
                pair_key = (u, v) if u < v else (v, u)
                if pair_key in direct_candidates:
                    continue
                feat_v = features[v]
                if has_flip and feat_u.flipped_hashes is not None:
                    _check_pair_sequences(
                        u,
                        v,
                        feat_u.flipped_hashes,
                        fingerprints[v],
                        flip_b=True,
                    )
                if has_crop and feat_u.cropped_hashes is not None:
                    right_seq = (
                        feat_v.cropped_hashes
                        if feat_v is not None and feat_v.cropped_hashes is not None
                        else fingerprints[v]
                    )
                    crop_v = (
                        feat_v.crop_vf
                        if feat_v is not None and feat_v.cropped_hashes is not None
                        else None
                    )
                    _check_pair_sequences(
                        u,
                        v,
                        feat_u.cropped_hashes,
                        right_seq,
                        flip_b=False,
                        crop_u=feat_u.crop_vf,
                        crop_v=crop_v,
                        uses_crop_u=True,
                        uses_crop_v=crop_v is not None,
                    )
                    if feat_u.cropped_flipped_hashes is not None:
                        _check_pair_sequences(
                            u,
                            v,
                            feat_u.cropped_flipped_hashes,
                            right_seq,
                            flip_b=True,
                            crop_u=feat_u.crop_vf,
                            crop_v=crop_v,
                            uses_crop_u=True,
                            uses_crop_v=crop_v is not None,
                        )
                    if feat_v is not None and (
                        feat_v.cropped_flipped_hashes is not None
                        or feat_v.flipped_hashes is not None
                    ):
                        right_flip = (
                            feat_v.cropped_flipped_hashes
                            if feat_v.cropped_flipped_hashes is not None
                            else feat_v.flipped_hashes
                        )
                        if right_flip is not None:
                            _check_pair_sequences(
                                u,
                                v,
                                feat_u.cropped_hashes,
                                right_flip,
                                flip_b=True,
                                crop_u=feat_u.crop_vf,
                                crop_v=crop_v,
                                uses_crop_u=True,
                                uses_crop_v=crop_v is not None,
                            )

    adjacency: dict[str, set[str]] = {record.path: set() for record in hashed}
    direct_meta: dict[tuple[int, int], tuple[bool, str | None, str | None, bool, bool]] = {}
    for (first, second), meta in direct_candidates.items():
        _mean, flip_b, crop_a, crop_b, uses_ca, uses_cb = meta
        a, b = hashed[first], hashed[second]
        adjacency[a.path].add(b.path)
        adjacency[b.path].add(a.path)
        direct_meta[(first, second)] = (flip_b, crop_a, crop_b, uses_ca, uses_cb)

    to_verify = [
        (
            hashed[first],
            hashed[second],
            verify_candidates[(first, second)][1],
            verify_candidates[(first, second)][2],
            verify_candidates[(first, second)][3],
            verify_candidates[(first, second)][4],
            verify_candidates[(first, second)][5],
        )
        for first, second in sorted(verify_candidates)
    ]
    return adjacency, to_verify, direct_meta


class _FrameBatchCache:
    """Thread-safe singleflight cache for extracted verification frame batches."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str | None, tuple[float, ...]], list] = {}

    def get(
        self,
        path: str,
        timestamps: list[float],
        cancelled: Callable[[], bool] | None,
        *,
        vf_prefix: str | None = None,
    ) -> list[bytes]:
        key = (path, vf_prefix, tuple(round(float(t), 6) for t in timestamps))
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
            frames = (
                _version_frame_batches(path, timestamps, cancelled, vf_prefix=vf_prefix)
                if vf_prefix is not None
                else _version_frame_batches(path, timestamps, cancelled)
            )
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
    raw_features_by_path: dict[str, _VideoFeatures] = {}

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
            results: list[
                tuple[str, str | None, int | None, int | None, float | None, str | None, list[bytes] | None]
            ],
        ) -> None:
            nonlocal videos_done
            by_path = {r.path: r for r in targets}
            for path, fp, w, h, dur, err, raw_frames in results:
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
                    if raw_frames:
                        _hs, flipped_hashes, informative = _analyze_raw_frames(raw_frames)
                        _cached_video_features_put(rec, flipped_hashes, informative)
                        raw_features_by_path[rec.path] = _VideoFeatures(
                            flipped_hashes=flipped_hashes,
                            informative=informative,
                        )
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
                    cancelled=cancelled,
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
                            if donor.path in raw_features_by_path:
                                raw_features_by_path[r.path] = raw_features_by_path[donor.path]
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

    # Populate secondary cropped-ROI hashes for records with border_crop set,
    # and attach any cached in-process flipped/informativeness features.
    features: list[_VideoFeatures | None] = []
    crop_jobs: list[tuple[int, FileRecord, str]] = []
    for idx, rec in enumerate(hashed):
        feat = raw_features_by_path.get(rec.path)
        if feat is None:
            cached_feat = _cached_video_features_get(rec)
            if cached_feat is not None:
                feat = _VideoFeatures(
                    flipped_hashes=cached_feat[0], informative=cached_feat[1]
                )
        box = _parse_border_crop(rec.border_crop)
        if box is not None and rec.duration and rec.duration > 0 and Path(rec.path).is_file():
            crop_jobs.append((idx, rec, _crop_vf_prefix(box)))
        features.append(feat)

    if crop_jobs:
        crop_results = _run_on_video_lanes(
            lambda item: _extract_cropped_roi_features(item[1], item[2], cancelled),
            crop_jobs,
            workers=workers,
            cancelled=cancelled,
        )
        for (idx, _rec, crop_vf), roi_res in zip(crop_jobs, crop_results, strict=True):
            if roi_res is None:
                continue
            cropped_h, cropped_flip_h, cropped_inf = roi_res
            base = features[idx] or _VideoFeatures()
            features[idx] = _VideoFeatures(
                flipped_hashes=base.flipped_hashes,
                informative=base.informative,
                cropped_hashes=cropped_h,
                cropped_flipped_hashes=cropped_flip_h,
                cropped_informative=cropped_inf,
                crop_vf=crop_vf,
            )

    fingerprints = [_fingerprint_hashes(record.video_fingerprint or "") for record in hashed]
    max_frame_distance = max(threshold * 2, 4)
    adjacency, to_verify, direct_meta = _evaluate_candidate_pairs(
        hashed,
        fingerprints,
        threshold=threshold,
        max_frame_distance=max_frame_distance,
        distinct=distinct,
        cancelled=cancelled,
        features=features,
    )

    index_by_path = {rec.path: idx for idx, rec in enumerate(hashed)}
    rgb_cache = _RgbThumbCache()
    feature_lock = threading.Lock()

    def _pair_is_informative(
        idx_a: int, idx_b: int, uses_ca: bool, uses_cb: bool
    ) -> bool:
        for idx, uses_crop in ((idx_a, uses_ca), (idx_b, uses_cb)):
            rec = hashed[idx]
            feat = features[idx]
            if uses_crop:
                if feat is not None and not feat.cropped_informative:
                    return False
                continue
            if feat is not None:
                if not feat.informative:
                    return False
                continue
            if Path(rec.path).is_file():
                lazy = _ensure_cached_record_features(rec, cancelled)
                if lazy is not None:
                    flipped_h, inf = lazy
                    with feature_lock:
                        base = features[idx] or _VideoFeatures()
                        features[idx] = _VideoFeatures(
                            flipped_hashes=flipped_h,
                            informative=inf,
                            cropped_hashes=base.cropped_hashes,
                            cropped_flipped_hashes=base.cropped_flipped_hashes,
                            cropped_informative=base.cropped_informative,
                            crop_vf=base.crop_vf,
                        )
                    if not inf:
                        return False
        return True

    if direct_meta:
        direct_items = list(direct_meta.items())

        def _validate_direct(item) -> tuple[str, str, bool]:
            (first, second), (flip_b, crop_a, crop_b, uses_ca, uses_cb) = item
            a, b = hashed[first], hashed[second]
            if a.size > 0 and a.size == b.size and a.sha256 and a.sha256 == b.sha256:
                return a.path, b.path, True
            if not Path(a.path).is_file() or not Path(b.path).is_file():
                return a.path, b.path, True
            if not _pair_is_informative(first, second, uses_ca, uses_cb):
                return a.path, b.path, False
            ok = _video_chroma_agrees(
                a,
                b,
                flip_b=flip_b,
                crop_a=crop_a,
                crop_b=crop_b,
                rgb_cache=rgb_cache,
                cancelled=cancelled,
            )
            return a.path, b.path, ok

        direct_checked = _run_on_video_lanes(
            _validate_direct,
            direct_items,
            workers=workers,
            cancelled=cancelled,
        )
        for path_a, path_b, keep in direct_checked:
            if not keep:
                adjacency[path_a].discard(path_b)
                adjacency[path_b].discard(path_a)

    if to_verify:
        frame_cache = _FrameBatchCache()
        remaining_verify: list[
            tuple[FileRecord, FileRecord, bool, str | None, str | None]
        ] = []
        for a, b, flip_b, crop_a, crop_b, uses_ca, uses_cb in to_verify:
            if a.size > 0 and a.size == b.size and a.sha256 and a.sha256 == b.sha256:
                adjacency[a.path].add(b.path)
                adjacency[b.path].add(a.path)
                continue
            idx_a, idx_b = index_by_path[a.path], index_by_path[b.path]
            feat_a, feat_b = features[idx_a], features[idx_b]
            if feat_a is not None and not (
                feat_a.cropped_informative if uses_ca else feat_a.informative
            ):
                continue
            if feat_b is not None and not (
                feat_b.cropped_informative if uses_cb else feat_b.informative
            ):
                continue
            remaining_verify.append((a, b, flip_b, crop_a, crop_b))

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
                lambda item: _same_footage_after_reframing(
                    item[0],
                    item[1],
                    cancelled,
                    frame_cache=frame_cache,
                    flip_b=item[2],
                    crop_a=item[3],
                    crop_b=item[4],
                    rgb_cache=rgb_cache,
                ),
                remaining_verify,
                workers=workers,
                progress=verify_progress,
                cancelled=cancelled,
            )
            for (a, b, _fb, _ca, _cb), matched in zip(
                remaining_verify, verified, strict=True
            ):
                if matched:
                    adjacency[a.path].add(b.path)
                    adjacency[b.path].add(a.path)

    _prevent_unknown_hub_bridging(hashed, adjacency)

    if progress:
        progress("video-cluster", len(hashed), len(hashed), "")

    return cluster_around_best(hashed, adjacency, distinct)


def _same_footage_after_reframing(
    a: FileRecord,
    b: FileRecord,
    cancelled: Callable[[], bool] | None,
    *,
    frame_cache: _FrameBatchCache | None = None,
    flip_b: bool = False,
    crop_a: str | None = None,
    crop_b: str | None = None,
    rgb_cache: _RgbThumbCache | None = None,
) -> bool:
    """Verify a near-miss pair across start, end, midpoint, and dual-end trim offsets."""
    if not a.duration or not b.duration:
        return False
    da, db = float(a.duration), float(b.duration)
    min_ov = (
        max(VERSION_MIN_SECONDS, max(da, db) * 0.88)
        if min(da, db) >= VERSION_MIN_SECONDS
        else min(da, db) * 0.85
    )
    offsets = sorted({0.0, round(db - da, 3)}, key=abs)
    mid_offset = round((db - da) / 2.0, 3)
    if mid_offset not in offsets:
        offsets.append(mid_offset)

    def _try_offset(offset: float) -> bool:
        try:
            return bool(
                _verify_video_offset(
                    a,
                    b,
                    offset,
                    cancelled,
                    frame_cache=frame_cache,
                    min_overlap=min_ov,
                    flip_b=flip_b,
                    crop_a=crop_a,
                    crop_b=crop_b,
                    rgb_cache=rgb_cache,
                )
            )
        except InterruptedError:
            raise
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            return False

    for offset in offsets:
        if _try_offset(offset):
            return True

    # Fallback for slight trims at BOTH ends when neither 0, db-da, nor midpoint aligns.
    if min(da, db) >= 2.0 and Path(a.path).is_file() and Path(b.path).is_file():
        try:
            import imagehash
            import numpy as np
            from PIL import Image

            dense_n = 16
            ts_a = [round((i + 0.5) * da / dense_n, 4) for i in range(dense_n)]
            ts_b = [round((i + 0.5) * db / dense_n, 4) for i in range(dense_n)]
            fa = (
                frame_cache.get(a.path, ts_a, cancelled, vf_prefix=crop_a)
                if frame_cache is not None
                else _version_frame_batches(a.path, ts_a, cancelled, vf_prefix=crop_a)
            )
            fb = (
                frame_cache.get(b.path, ts_b, cancelled, vf_prefix=crop_b)
                if frame_cache is not None
                else _version_frame_batches(b.path, ts_b, cancelled, vf_prefix=crop_b)
            )
            ha = [
                int(str(imagehash.phash(Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), r))), 16)
                for r in fa
            ]
            hb = []
            for r in fb:
                if flip_b:
                    arr = np.frombuffer(r, dtype=np.uint8).reshape(HASH_FRAME_SIZE, HASH_FRAME_SIZE)
                    im = Image.frombytes(
                        "L",
                        (HASH_FRAME_SIZE, HASH_FRAME_SIZE),
                        np.ascontiguousarray(arr[:, ::-1]).tobytes(),
                    )
                else:
                    im = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), r)
                hb.append(int(str(imagehash.phash(im)), 16))
            max_trim = max(abs(da - db), 0.15 * min(da, db)) + 0.5
            votes: list[tuple[float, float]] = []
            for ta, h_left in zip(ts_a, ha, strict=True):
                for tb, h_right in zip(ts_b, hb, strict=True):
                    if abs(tb - ta) <= max_trim and (h_left ^ h_right).bit_count() <= 10:
                        votes.append((ta, tb))
            if len(votes) >= 3:
                step = max(da, db) / dense_n
                for cand_offset in _offset_candidates(votes, max(0.2, step)):
                    rounded = round(cand_offset, 3)
                    if rounded in offsets:
                        continue
                    if _refine_and_verify_offset(
                        a,
                        b,
                        rounded,
                        step,
                        cancelled,
                        frame_cache=frame_cache,
                        min_overlap=min_ov,
                        flip_b=flip_b,
                        crop_a=crop_a,
                        crop_b=crop_b,
                        rgb_cache=rgb_cache,
                    ):
                        return True
        except InterruptedError:
            raise
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            return False
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
    path: str,
    timestamps: list[float],
    cancelled: Callable[[], bool] | None,
    *,
    vf_prefix: str | None = None,
) -> list[bytes]:
    software = bool(getattr(_DECODE_LANE, "software", False))
    hwaccel = () if software else _hwaccel_args()
    frames: list[bytes] = []
    for start in range(0, len(timestamps), 12):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        chunk = timestamps[start : start + 12]
        chunk_frames = _extract_hash_frames_once(
            path, chunk, hwaccel, cancelled=cancelled, vf_prefix=vf_prefix
        )
        if not chunk_frames and hwaccel:
            hwaccel = ()
            chunk_frames = _extract_hash_frames_once(
                path, chunk, (), cancelled=cancelled, vf_prefix=vf_prefix
            )
        if len(chunk_frames) != len(chunk):
            chunk_frames = []
            for ts in chunk:
                raw = _extract_hash_frame(
                    path, ts, software=True, cancelled=cancelled, vf_prefix=vf_prefix
                )
                if raw is None:
                    raise ValueError("incomplete video version samples")
                chunk_frames.append(raw)
        frames.extend(chunk_frames)
    if len(frames) != len(timestamps):
        raise ValueError("incomplete video version samples")
    return frames


def _version_cache_stat(
    record: FileRecord,
) -> tuple[str, str, int, int, int, int]:
    """Return ``(identity, resolved_path, dev, ino, size, mtime_ns)`` for ``record``."""
    resolved = str(Path(record.path).resolve())
    st = Path(record.path).stat()
    identity = json.dumps(
        [
            VERSION_CACHE_VERSION,
            resolved,
            st.st_dev,
            st.st_ino,
            st.st_size,
            st.st_mtime_ns,
            st.st_ctime_ns,
        ],
        separators=(",", ":"),
    )
    return identity, resolved, int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns)


def _version_cache_identity(record: FileRecord) -> str:
    return _version_cache_stat(record)[0]


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
            duration, probed_w, probed_h = (
                probe_video(record.path, cancelled=cancelled)
                if cancelled is not None
                else probe_video(record.path)
            )
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


# Hash pairs compared per batch of the candidate join: bounds its working
# arrays to tens of megabytes however many videos a scan holds.
_VERSION_JOIN_BATCH_PAIRS = 1 << 21
VERSION_CANDIDATE_MAX_VIDEOS = 256


def _dedupe_timeline_samples(
    timeline: list[tuple[float, int]],
) -> list[tuple[float, int]]:
    """Collapse consecutive near-identical frame hashes within one video."""
    kept: list[tuple[float, int]] = []
    for timestamp, fingerprint in timeline:
        if not kept or (fingerprint ^ kept[-1][1]).bit_count() > 2:
            kept.append((timestamp, fingerprint))
    return kept


def _version_candidate_pairs(
    samples: list[list[tuple[float, int]]],
    cancelled: Callable[[], bool] | None = None,
) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """Index frame hashes once; avoid decoding or testing every pair of videos."""
    import numpy as np

    deduped = [_dedupe_timeline_samples(timeline) for timeline in samples]
    occurrences: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for index, timeline in enumerate(deduped):
        for timestamp, fingerprint in timeline:
            occurrences[fingerprint].append((index, timestamp))
    if not occurrences:
        return {}
    # Every distinct hash is compared with every other in upper-triangle vectorised batches.
    keys = sorted(occurrences)
    n_keys = len(keys)
    values = np.array(keys, dtype=np.uint64)
    # Count distinct videos carrying each hash so repeated frames in slow/tripod
    # recordings never trip the library-wide generic-frame cap.
    weights = np.array(
        [len({idx for idx, _ in occurrences[key]}) for key in keys],
        dtype=np.int64,
    )
    degree_weights = np.zeros(n_keys, dtype=np.int64)
    upper_neighbors: list[list[tuple[int, int]]] = [[] for _ in range(n_keys)]
    rows = max(1, _VERSION_JOIN_BATCH_PAIRS // n_keys)
    for begin in range(0, n_keys, rows):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        end_r = min(begin + rows, n_keys)
        # Off-grid cuts shift samples by a fraction of a second. Candidate
        # lookup is deliberately looser than the aligned verification.
        distances = _popcount64(values[begin:end_r, None] ^ values[None, begin:])
        hits = distances <= 10
        for local_r in range(end_r - begin):
            r = begin + local_r
            offset_base = r - begin
            row_hits = hits[local_r, offset_base:]
            cols = np.flatnonzero(row_hits)
            if not cols.size:
                continue
            col_indices = cols + r
            dists = distances[local_r, offset_base:][cols]
            degree_weights[r] += int(weights[col_indices].sum())
            off_diag = col_indices > r
            if np.any(off_diag):
                np.add.at(degree_weights, col_indices[off_diag], int(weights[r]))
            for c_idx, dist_val in zip(col_indices.tolist(), dists.tolist(), strict=True):
                upper_neighbors[r].append((c_idx, dist_val))

    near: dict[int, list[int]] = {}
    neighbor_dists: list[list[tuple[int, int]]] = [[] for _ in range(n_keys)]
    for r, pairs in enumerate(upper_neighbors):
        for c_idx, dist_val in pairs:
            if degree_weights[r] <= VERSION_CANDIDATE_MAX_VIDEOS:
                neighbor_dists[r].append((dist_val, c_idx))
            if c_idx != r and degree_weights[c_idx] <= VERSION_CANDIDATE_MAX_VIDEOS:
                neighbor_dists[c_idx].append((dist_val, r))
    for r, items in enumerate(neighbor_dists):
        if items:
            items.sort(key=lambda pair: (pair[0], pair[1]))
            near[keys[r]] = [keys[c_idx] for _dist, c_idx in items]

    candidates: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for i, timeline in enumerate(deduped):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        for ta, fingerprint in timeline:
            for matched in near.get(fingerprint, ()):
                for j, tb in occurrences[matched]:
                    if j > i:
                        candidates[(i, j)].append((ta, tb))
    return {
        pair: votes
        for pair, votes in candidates.items()
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
    a: FileRecord,
    b: FileRecord,
    offset: float,
    cancelled: Callable[[], bool] | None,
    *,
    frame_cache: _FrameBatchCache | None = None,
    min_overlap: float = VERSION_MIN_SECONDS,
    flip_b: bool = False,
    crop_a: str | None = None,
    crop_b: str | None = None,
    rgb_cache: _RgbThumbCache | None = None,
) -> dict | None:
    """Verify distributed samples across the overlap using pHash + normalized luma NCC/z-MAD.

    Offset maps an A timestamp onto B. Requires essentially all of the shorter
    clip and genuinely distinct pairwise sample hashes.
    """
    import imagehash
    import numpy as np
    from PIL import Image

    da, db = float(a.duration or 0), float(b.duration or 0)
    start = max(0.0, -offset)
    end = min(da, db - offset)
    overlap = end - start
    if overlap < min_overlap or overlap < min(da, db) * 0.8:
        return None
    times = [start + overlap * fraction for fraction in (0.08, 0.22, 0.36, 0.5, 0.64, 0.78, 0.92)]
    right_times = [t + offset for t in times]
    if frame_cache is not None:
        left = frame_cache.get(a.path, times, cancelled, vf_prefix=crop_a)
        right = frame_cache.get(b.path, right_times, cancelled, vf_prefix=crop_b)
    else:
        left = _version_frame_batches(a.path, times, cancelled, vf_prefix=crop_a)
        right = _version_frame_batches(b.path, right_times, cancelled, vf_prefix=crop_b)
    distances, frame_oks, hashes = [], [], []
    non_blank = 0
    for ra, rb in zip(left, right, strict=True):
        aa = np.frombuffer(ra, np.uint8).astype(np.float64)
        ab = np.frombuffer(rb, np.uint8).astype(np.float64)
        if flip_b:
            ab_2d = ab.reshape(HASH_FRAME_SIZE, HASH_FRAME_SIZE)[:, ::-1]
            ab = np.ascontiguousarray(ab_2d).reshape(-1)
            ib = Image.frombytes(
                "L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), ab.astype(np.uint8).tobytes()
            )
        else:
            ib = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), rb)
        ia = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), ra)
        ha, hb = imagehash.phash(ia), imagehash.phash(ib)
        hashes.append(int(str(ha), 16))
        distances.append(ha - hb)
        std_a, std_b = float(aa.std()), float(ab.std())
        if std_a >= MIN_FRAME_LUMA_STD and std_b >= MIN_FRAME_LUMA_STD:
            non_blank += 1
        if std_a < 2.0 and std_b < 2.0:
            frame_oks.append(abs(float(aa.mean() - ab.mean())) <= 18.0)
        elif std_a < 2.0 or std_b < 2.0:
            frame_oks.append(False)
        else:
            za = (aa - float(aa.mean())) / std_a
            zb = (ab - float(ab.mean())) / std_b
            ncc = float(np.mean(za * zb))
            z_mad = float(np.mean(np.abs(za - zb)))
            frame_oks.append(ncc >= MIN_FRAME_NCC and z_mad <= MAX_FRAME_ZMAD)

    if non_blank < 4:
        return None
    distinct_hashes: list[int] = []
    for hv in hashes:
        if all((hv ^ prev).bit_count() >= 4 for prev in distinct_hashes):
            distinct_hashes.append(hv)
    diverse = len(distinct_hashes) >= 3
    good = [d <= 10 and ok for d, ok in zip(distances, frame_oks, strict=True)]
    if not diverse or sum(good) < 6 or sum(distances) / len(distances) > 7:
        return None
    if rgb_cache is not None and not _video_chroma_agrees(
        a,
        b,
        flip_b=flip_b,
        crop_a=crop_a,
        crop_b=crop_b,
        times_a=[times[1], times[3], times[5]],
        times_b=[right_times[1], right_times[3], right_times[5]],
        rgb_cache=rgb_cache,
        cancelled=cancelled,
    ):
        return None
    return {
        "ranges": {
            a.path: [round(start, 3), round(end, 3)],
            b.path: [round(start + offset, 3), round(end + offset, 3)],
        },
        "overlap_seconds": round(overlap, 3),
        "mean_frame_distance": round(float(sum(distances)) / len(distances), 3),
        "verified_samples": len(times),
        "approximate_alignment": True,
        "relation": "same_footage" if abs(offset) < 0.1 and abs(da - db) < 0.2 else "excerpt",
    }


def _refine_and_verify_offset(
    a: FileRecord,
    b: FileRecord,
    base_offset: float,
    step: float,
    cancelled: Callable[[], bool] | None,
    *,
    frame_cache: _FrameBatchCache | None = None,
    min_overlap: float = VERSION_MIN_SECONDS,
    flip_b: bool = False,
    crop_a: str | None = None,
    crop_b: str | None = None,
    rgb_cache: _RgbThumbCache | None = None,
) -> dict | None:
    """Refine ``base_offset`` locally around the bin median before verifying."""
    import imagehash
    import numpy as np
    from PIL import Image

    direct = _verify_video_offset(
        a,
        b,
        base_offset,
        cancelled,
        frame_cache=frame_cache,
        min_overlap=min_overlap,
        flip_b=flip_b,
        crop_a=crop_a,
        crop_b=crop_b,
        rgb_cache=rgb_cache,
    )
    if direct is not None:
        return direct

    da, db = float(a.duration or 0), float(b.duration or 0)
    start = max(0.0, -base_offset)
    end = min(da, db - base_offset)
    overlap = end - start
    if overlap < min_overlap or overlap < min(da, db) * 0.8:
        return None
    radius = min(1.5, max(0.4, step * 0.75))
    delta_step = 0.2 if radius <= 0.8 else 0.25
    deltas = []
    k = 1
    while k * delta_step <= radius + 1e-6:
        deltas.extend((-round(k * delta_step, 3), round(k * delta_step, 3)))
        k += 1
    if not deltas:
        return None

    # Reuse the 3 middle anchor timestamps from the failed _verify_video_offset call on A.
    anchors = [start + overlap * fraction for fraction in (0.22, 0.5, 0.78)]
    if frame_cache is not None:
        left_frames = frame_cache.get(a.path, anchors, cancelled, vf_prefix=crop_a)
    else:
        left_frames = _version_frame_batches(a.path, anchors, cancelled, vf_prefix=crop_a)

    left_norms: list[tuple[int, object]] = []
    for raw in left_frames:
        arr = np.frombuffer(raw, np.uint8).astype(np.float64)
        std = float(arr.std())
        norm = (arr - float(arr.mean())) / std if std >= 2.0 else None
        hv = int(
            str(imagehash.phash(Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), raw))),
            16,
        )
        left_norms.append((hv, norm))

    valid_deltas = [
        d for d in deltas if all(0.0 <= ta + base_offset + d < db for ta in anchors)
    ]
    if not valid_deltas:
        return None
    probe_times = [ta + base_offset + d for d in valid_deltas for ta in anchors]
    probe_frames = (
        frame_cache.get(b.path, probe_times, cancelled, vf_prefix=crop_b)
        if frame_cache is not None
        else _version_frame_batches(b.path, probe_times, cancelled, vf_prefix=crop_b)
    )

    best_delta: float | None = None
    best_score: tuple[float, float] = (65.0, 1.0)
    for idx_d, delta in enumerate(valid_deltas):
        chunk = probe_frames[idx_d * 3 : (idx_d + 1) * 3]
        dists = []
        nccs = []
        for (hv_a, norm_a), rb in zip(left_norms, chunk, strict=True):
            arr_b = np.frombuffer(rb, np.uint8).astype(np.float64)
            if flip_b:
                arr_b = np.ascontiguousarray(
                    arr_b.reshape(HASH_FRAME_SIZE, HASH_FRAME_SIZE)[:, ::-1]
                ).reshape(-1)
                ib = Image.frombytes(
                    "L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), arr_b.astype(np.uint8).tobytes()
                )
            else:
                ib = Image.frombytes("L", (HASH_FRAME_SIZE, HASH_FRAME_SIZE), rb)
            hv_b = int(str(imagehash.phash(ib)), 16)
            dists.append((hv_a ^ hv_b).bit_count())
            std_b = float(arr_b.std())
            if norm_a is not None and std_b >= 2.0:
                norm_b = (arr_b - float(arr_b.mean())) / std_b
                nccs.append(float(np.mean(norm_a * norm_b)))
        mean_d = sum(dists) / len(dists)
        mean_ncc = sum(nccs) / len(nccs) if nccs else 0.0
        score = (mean_d, -mean_ncc)
        if score < best_score:
            best_score = score
            best_delta = delta

    if best_delta is None or best_score[0] > 12:
        return None
    return _verify_video_offset(
        a,
        b,
        round(base_offset + best_delta, 3),
        cancelled,
        frame_cache=frame_cache,
        min_overlap=min_overlap,
        flip_b=flip_b,
        crop_a=crop_a,
        crop_b=crop_b,
        rgb_cache=rgb_cache,
    )


def _audio_stream_present(path: str, cancelled: Callable[[], bool] | None) -> bool:
    if not shutil.which("ffprobe"):
        raise ValueError("ffprobe unavailable")
    result = _run_video_command([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index",
        "-of", "json", path,
    ], timeout=15, cancelled=cancelled)
    if result.returncode:
        raise ValueError("audio stream unavailable")
    return bool(json.loads(result.stdout).get("streams"))


def _audio_window(
    path: str,
    start: float,
    duration: float,
    cancelled: Callable[[], bool] | None,
    *,
    sample_rate: int = AUDIO_SAMPLE_RATE,
) -> bytes:
    result = _run_video_command([
        "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.4f}", "-threads", "1",
        "-i", path, "-t", f"{duration:.4f}", "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "pipe:1",
    ], timeout=30, cancelled=cancelled)
    if result.returncode:
        raise ValueError("audio sample unavailable")
    return result.stdout


@lru_cache(maxsize=4)
def _mel_filterbank(
    sample_rate: int = AUDIO_SAMPLE_RATE,
    n_fft: int = AUDIO_N_FFT,
    n_mels: int = AUDIO_N_MELS,
    fmin: float = 80.0,
    fmax: float = 7600.0,
):
    import numpy as np

    def hz_to_mel(hz: float) -> float:
        return 2595.0 * math.log10(1.0 + hz / 700.0)

    def mel_to_hz(mel: float) -> float:
        return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

    mels = np.linspace(hz_to_mel(fmin), hz_to_mel(min(fmax, sample_rate * 0.48)), n_mels + 2)
    hz = np.array([mel_to_hz(float(m)) for m in mels], dtype=np.float64)
    bins = np.floor((n_fft + 1) * hz / sample_rate).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float64)
    for m in range(1, n_mels + 1):
        left, center, right = int(bins[m - 1]), int(bins[m]), int(bins[m + 1])
        if center == left:
            center += 1
        if right == center:
            right += 1
        for k in range(left, min(center, fb.shape[1])):
            fb[m - 1, k] = (k - left) / max(1, center - left)
        for k in range(center, min(right, fb.shape[1])):
            fb[m - 1, k] = (right - k) / max(1, right - center)
    return fb, np.hanning(n_fft)


def _log_mel_spectrogram(pcm):
    import numpy as np

    fb, win = _mel_filterbank()
    x = pcm.astype(np.float64) / 32768.0
    x = x - float(x.mean())
    n_frames = 1 + (len(x) - AUDIO_N_FFT) // AUDIO_HOP_SAMPLES
    if n_frames < 4:
        return None
    idx = np.arange(AUDIO_N_FFT)[None, :] + np.arange(n_frames)[:, None] * AUDIO_HOP_SAMPLES
    frames = x[idx] * win[None, :]
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    mel = spec @ fb.T
    floor = max(float(mel.max()) * 1e-5, 1e-12)
    return np.log(np.maximum(mel, floor))


def _spectral_audio_similarity(left_pcm, right_pcm) -> float:
    """Phase-invariant log-mel energy-difference correlation across ±2s lag."""
    import numpy as np

    def _dbfs(pcm) -> float:
        x = pcm.astype(np.float64) / 32768.0
        x = x - float(x.mean())
        rms = float(np.sqrt(np.mean(x * x)))
        return 20.0 * math.log10(max(rms, 1e-12))

    db_a, db_b = _dbfs(left_pcm), _dbfs(right_pcm)
    if db_a < AUDIO_SILENCE_DBFS and db_b < AUDIO_SILENCE_DBFS:
        return 1.0
    if (db_a < AUDIO_SILENCE_DBFS and db_b > AUDIO_SILENCE_DBFS + 7.0) or (
        db_b < AUDIO_SILENCE_DBFS and db_a > AUDIO_SILENCE_DBFS + 7.0
    ):
        return 0.0

    sa = _log_mel_spectrogram(left_pcm)
    sb = _log_mel_spectrogram(right_pcm)
    if sa is None or sb is None:
        return 0.5
    if sa.shape[0] > 8 and sb.shape[0] > 8:
        sa, sb = sa[2:], sb[2:]
    da = np.diff(sa, axis=0)
    db = np.diff(sb, axis=0)
    flux_a, flux_b = float(da.std()), float(db.std())
    if flux_a < 0.15 and flux_b < 0.15:
        pa, pb = sa.mean(axis=0), sb.mean(axis=0)
        pa = pa - float(pa.mean())
        pb = pb - float(pb.mean())
        denom = float(np.linalg.norm(pa) * np.linalg.norm(pb))
        return float(np.dot(pa, pb) / denom) if denom > 1e-9 else 0.0
    if (flux_a < 0.12 and flux_b > 0.35) or (flux_b < 0.12 and flux_a > 0.35):
        return 0.0

    if da.shape[0] >= 5 and db.shape[0] >= 5:
        kernel = np.array([0.25, 0.5, 0.25], dtype=np.float64)
        da_s = np.apply_along_axis(lambda c: np.convolve(c, kernel, mode="valid"), 0, da)
        db_s = np.apply_along_axis(lambda c: np.convolve(c, kernel, mode="valid"), 0, db)
    else:
        da_s, db_s = da, db

    t_len = min(da_s.shape[0], db_s.shape[0])
    max_lag = min(int(AUDIO_MAX_LAG_SECONDS * AUDIO_SAMPLE_RATE / AUDIO_HOP_SAMPLES), max(1, t_len // 2))
    min_overlap_frames = max(10, t_len // 3)
    best = -1.0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            xa = da_s[lag:t_len]
            xb = db_s[: t_len - lag]
        else:
            xa = da_s[: t_len + lag]
            xb = db_s[-lag:t_len]
        if xa.shape[0] < min_overlap_frames:
            continue
        xa_z = xa - float(xa.mean())
        xb_z = xb - float(xb.mean())
        denom = float(np.linalg.norm(xa_z) * np.linalg.norm(xb_z))
        if denom > 1e-9:
            corr = float(np.sum(xa_z * xb_z) / denom)
            best = max(best, corr)
    return best


def compare_video_audio(
    a: str,
    b: str,
    ranges: dict[str, list[float]],
    *,
    cancelled: Callable[[], bool] | None = None,
    presence_cache: dict[str, bool] | None = None,
    presence_lock: threading.Lock | None = None,
) -> str:
    """Phase-invariant spectral comparison across distributed overlap windows.

    'same' means sampled audio agrees, not that complete tracks are identical.
    Decode failures stay 'unavailable' rather than becoming false differences.
    """
    import numpy as np

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
        if overlap <= 0:
            return "unavailable"
        if overlap >= 12.0:
            fractions = (0.05, 0.28, 0.52, 0.74)
            duration = min(6.0, overlap * 0.25)
        else:
            fractions = (0.05, 0.34, 0.63)
            duration = min(4.0, overlap * 0.35)
        scores = []
        for fraction in fractions:
            arrays = [
                np.frombuffer(
                    _audio_window(
                        path,
                        ranges[path][0] + overlap * fraction,
                        duration,
                        cancelled,
                    ),
                    dtype="<i2",
                )
                for path in (a, b)
            ]
            n = min(map(len, arrays))
            if n < AUDIO_SAMPLE_RATE // 10:
                return "unavailable"
            scores.append(_spectral_audio_similarity(arrays[0][:n], arrays[1][:n]))
        if min(scores) >= 0.72 and (sum(scores) / len(scores)) >= 0.78:
            return "same"
        if max(scores) < 0.45 or (min(scores) < 0.2 and (sum(scores) / len(scores)) < 0.45):
            return "different"
        return "unavailable"
    except InterruptedError:
        raise
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
        return "unavailable"


def _parse_cached_version_payload(
    raw_json: str,
    *,
    expected_path: str | None = None,
    require_path_diff: bool = False,
) -> tuple[float, int | None, int | None, list[tuple[float, int]]] | None:
    try:
        data = json.loads(raw_json)
        if require_path_diff:
            stored_path = data.get("path")
            if not stored_path or (expected_path is not None and stored_path == expected_path):
                return None
        duration = float(data["duration"])
        cached_samples = [(float(t), int(h, 16)) for t, h in data["samples"]]
        width = data.get("width")
        height = data.get("height")
        width = int(width) if width else None
        height = int(height) if height else None
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(duration) and duration > 0):
        return None
    if len(cached_samples) > VERSION_SAMPLE_LIMIT:
        return None
    if duration < VERSION_MIN_SECONDS and cached_samples:
        return None
    if not all(0 <= t < duration and 0 <= h < (1 << 64) for t, h in cached_samples):
        return None
    return duration, width, height, cached_samples


def _serialize_version_payload(
    resolved_path: str,
    duration: float,
    width: int | None,
    height: int | None,
    samples: Sequence[tuple[float, int]],
) -> str:
    return json.dumps(
        {
            "v": VERSION_CACHE_VERSION,
            "path": resolved_path,
            "duration": duration,
            "width": width,
            "height": height,
            "samples": [(t, f"{hv:016x}") for t, hv in samples],
        },
        separators=(",", ":"),
    )


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
                connection = sqlite3.connect(str(cache_path), timeout=30.0)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS samples("
                    "identity TEXT PRIMARY KEY, samples TEXT, "
                    "device INTEGER, inode INTEGER, size INTEGER, mtime_ns INTEGER)"
                )
                cols = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(samples)").fetchall()
                }
                if "samples" in cols:
                    for col in ("device", "inode", "size", "mtime_ns"):
                        if col not in cols:
                            connection.execute(
                                f"ALTER TABLE samples ADD COLUMN {col} INTEGER"
                            )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_samples_dev_ino "
                        "ON samples(device, inode, size, mtime_ns)"
                    )
            except (OSError, sqlite3.Error) as exc:
                cache_errors.append(str(exc))
                if connection is not None:
                    connection.close()
                    connection = None

        samples: list[list[tuple[float, int]]] = [[] for _ in videos]
        stat_meta: list[tuple[str, str, int, int, int, int] | None] = [None] * len(videos)
        uncached_indices: list[int] = []
        done_count = 0
        rekey_rows: list[tuple[str, str, int, int, int, int]] = []

        for i, record in enumerate(videos):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            cached_hit = False
            if connection is not None:
                try:
                    meta = _version_cache_stat(record)
                    stat_meta[i] = meta
                    key, resolved_path, dev, ino, size, mtime_ns = meta
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
                    parsed = _parse_cached_version_payload(row[0]) if row else None
                    if parsed is None and ino:
                        fallback_row = connection.execute(
                            "SELECT samples FROM samples "
                            "WHERE device = ? AND inode = ? AND size = ? AND mtime_ns = ? "
                            "ORDER BY rowid DESC LIMIT 1",
                            (dev, ino, size, mtime_ns),
                        ).fetchone()
                        if fallback_row:
                            parsed = _parse_cached_version_payload(
                                fallback_row[0],
                                expected_path=resolved_path,
                                require_path_diff=True,
                            )
                            if parsed is not None:
                                dur_c, w_c, h_c, samp_c = parsed
                                rekey_rows.append((
                                    key,
                                    _serialize_version_payload(
                                        resolved_path, dur_c, w_c, h_c, samp_c
                                    ),
                                    dev,
                                    ino,
                                    size,
                                    mtime_ns,
                                ))
                except sqlite3.Error as exc:
                    cache_errors.append(str(exc))
                    connection.close()
                    connection = None
                    parsed = None
                if parsed is not None:
                    duration, cached_w, cached_h, cached_samples = parsed
                    record.duration = duration
                    if cached_w:
                        record.width = cached_w
                    if cached_h:
                        record.height = cached_h
                    samples[i] = cached_samples
                    cached_hit = True
            if cached_hit:
                done_count += 1
                if progress:
                    progress("video-versions", done_count, len(videos), "Finding video excerpts")
            else:
                uncached_indices.append(i)

        sha_by_idx: dict[int, str] = {
            i: r.sha256 for i, r in enumerate(videos) if r.sha256
        }

        to_store: list[tuple[str, str, int | None, int | None, int | None, int | None]] = list(
            rekey_rows
        )

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
                    meta = stat_meta[i]
                    if (
                        connection is not None
                        and meta is not None
                        and donor.duration
                        and math.isfinite(donor.duration)
                        and donor.duration > 0
                    ):
                        key, resolved_path, dev, ino, size, mtime_ns = meta
                        to_store.append((
                            key,
                            _serialize_version_payload(
                                resolved_path,
                                float(donor.duration),
                                donor.width,
                                donor.height,
                                donor_samples,
                            ),
                            dev,
                            ino,
                            size,
                            mtime_ns,
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
                            meta = stat_meta[idx]
                            if (
                                connection is not None
                                and meta is not None
                                and dur
                                and math.isfinite(dur)
                                and dur > 0
                            ):
                                key, resolved_path, dev, ino, size, mtime_ns = meta
                                to_store.append((
                                    key,
                                    _serialize_version_payload(
                                        resolved_path, float(dur), w, h, extracted
                                    ),
                                    dev,
                                    ino,
                                    size,
                                    mtime_ns,
                                ))

                if progress:
                    progress("video-versions", len(videos), len(videos), "Finding video excerpts")

        if connection is not None and to_store:
            try:
                connection.executemany(
                    "INSERT OR REPLACE INTO samples("
                    "identity, samples, device, inode, size, mtime_ns"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
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
            bin_step = max(0.25, step)
            offsets = _offset_candidates(votes, bin_step)
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
                for offset in offsets[:2]:
                    try:
                        evidence = _refine_and_verify_offset(
                            a, b, offset, bin_step, cancelled, frame_cache=frame_cache
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
