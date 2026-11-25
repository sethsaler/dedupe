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
# Sequential full-timeline decode is only faster than 8 keyframe seeks on short clips;
# above 15s, 4K/60fps videos otherwise decode thousands of frames on 1 thread.
SINGLE_PASS_MAX_DURATION = 15.0
# Cap simultaneous hardware-decoded inputs in one ffmpeg process so concurrent
# workers do not exhaust Apple Silicon VideoToolbox decoder sessions.
MAX_HWACCEL_MULTI_INPUTS = 4
# Dense excerpt sampling prefers a single sequential pass on short/medium videos
# (1 demuxer, 1 decoder) and smaller multi-input batches on long videos.
VERSION_BATCH_SIZE = 4
VERSION_SINGLE_PASS_MAX_DURATION = 90.0
VERSION_SINGLE_PASS_MAX_STEP = 2.5
MAX_CONSECUTIVE_SEEK_TIMEOUTS = 2
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

_REAL_SHUTIL_WHICH = shutil.which
_BINARY_PATH_LOCK = threading.Lock()
_BINARY_PATH_CACHE: dict[tuple[str, str], str | None] = {}

_AUDIO_PRESENCE_INFLIGHT_LOCK = threading.Lock()
_AUDIO_PRESENCE_INFLIGHT: dict[str, threading.Event] = {}


def _resolve_binary(name: str) -> str | None:
    """Resolve and cache an executable path, invalidating on PATH change or missing binary."""
    if shutil.which is not _REAL_SHUTIL_WHICH:
        return shutil.which(name)
    key = (name, os.environ.get("PATH", ""))
    with _BINARY_PATH_LOCK:
        if key in _BINARY_PATH_CACHE:
            cached = _BINARY_PATH_CACHE[key]
            if cached is None or (os.path.isfile(cached) and os.access(cached, os.X_OK)):
                return cached
        resolved = shutil.which(name)
        _BINARY_PATH_CACHE[key] = resolved
        return resolved


def _ffmpeg_bin() -> str:
    return _resolve_binary("ffmpeg") or "ffmpeg"


def _ffprobe_bin() -> str:
    return _resolve_binary("ffprobe") or "ffprobe"


def ffmpeg_available() -> bool:
    return _resolve_binary("ffmpeg") is not None and _resolve_binary("ffprobe") is not None


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
    ffmpeg = _resolve_binary("ffmpeg")
    if sys.platform != "darwin" or not ffmpeg:
        return ()
    try:
        result = _REAL_SUBPROCESS_RUN(
            [ffmpeg, "-hide_banner", "-hwaccels"],
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
    ffprobe = _resolve_binary("ffprobe")
    if not ffprobe:
        return None, None, None
    cmd = [
        ffprobe,
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
    cancelled: Callable[[], bool] | None = None,
) -> list[Path]:
    """
    One ffmpeg pass: evenly spaced low-res JPEGs.

    Uses a single decode with fps sampling instead of N independent seeks
    (each seek can re-decode from a keyframe).
    """
    if duration is None:
        duration, _, _ = (
            probe_video(path, cancelled=cancelled) if cancelled is not None else probe_video(path)
        )
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
        _ffmpeg_bin(),
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
    result = _run_video_command(cmd, timeout=180, cancelled=cancelled)
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
        _ffmpeg_bin(),
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
    result = _run_video_command(cmd, timeout=25, cancelled=cancelled)
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
    timeout: float = 120,
) -> list[bytes]:
    """Decode once sequentially and select frames at ``timestamps`` to stdout."""
    if not timestamps:
        return []
    hwaccel = () if software else _hwaccel_args()
    frames = _extract_sequential_hash_frames_once(
        path, timestamps, hwaccel, cancelled=cancelled, vf_prefix=vf_prefix, timeout=timeout
    )
    if not frames and hwaccel:
        frames = _extract_sequential_hash_frames_once(
            path, timestamps, (), cancelled=cancelled, vf_prefix=vf_prefix, timeout=timeout
        )
    return frames


def _extract_sequential_hash_frames_once(
    path: str | Path,
    timestamps: list[float],
    hwaccel: tuple[str, ...],
    *,
    cancelled: Callable[[], bool] | None = None,
    vf_prefix: str | None = None,
    timeout: float = 120,
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
        _ffmpeg_bin(),
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
    result = _run_video_command(cmd, timeout=timeout, cancelled=cancelled)
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
            _ffmpeg_bin(),
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
        result = _run_video_command(cmd, timeout=120, cancelled=cancelled)
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
    back to software decode; ``software`` skips the hardware attempt. Caps
    concurrent hardware-decoded inputs per process to avoid exhausting Apple
    Silicon VideoToolbox sessions. Returns ``[]`` when the pass is incomplete
    so the caller can fall back.
    """
    if not timestamps:
        return []
    hwaccel = () if software else _hwaccel_args()
    if hwaccel and len(timestamps) > MAX_HWACCEL_MULTI_INPUTS:
        frames: list[bytes] = []
        active_hwaccel: tuple[str, ...] = hwaccel
        for start in range(0, len(timestamps), MAX_HWACCEL_MULTI_INPUTS):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            chunk = timestamps[start : start + MAX_HWACCEL_MULTI_INPUTS]
            chunk_frames = _extract_hash_frames_once(
                path, chunk, active_hwaccel, cancelled=cancelled, vf_prefix=vf_prefix
            )
            if not chunk_frames and active_hwaccel:
                active_hwaccel = ()
                chunk_frames = _extract_hash_frames_once(
                    path, chunk, (), cancelled=cancelled, vf_prefix=vf_prefix
                )
            if len(chunk_frames) != len(chunk):
                return []
            frames.extend(chunk_frames)
        return frames
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
    # Never attach VideoToolbox simultaneously to > MAX_HWACCEL_MULTI_INPUTS
    # parallel inputs inside a single ffmpeg invocation.
    effective_hwaccel = hwaccel if len(timestamps) <= MAX_HWACCEL_MULTI_INPUTS else ()
    cmd = [_ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error"]
    for timestamp in timestamps:
        cmd += [*effective_hwaccel, "-ss", f"{timestamp:.6f}", "-threads", "1", "-i", str(path)]
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
    batch_timeout = min(120, max(30, 15 * len(timestamps)))
    result = _run_video_command(cmd, timeout=batch_timeout, cancelled=cancelled)
    if result.returncode != 0 or len(result.stdout) != len(timestamps) * expected:
        return []
    return [
        result.stdout[index * expected : (index + 1) * expected]
        for index in range(len(timestamps))
    ]


_ORIGINAL_EXTRACT_HASH_FRAMES_ONCE = _extract_hash_frames_once


def _extract_seek_frame_ppm(
    path: str | Path,
    timestamp: float,
    *,
    frame_width: int,
    cancelled: Callable[[], bool] | None = None,
) -> bytes | None:
    """Fast-seek to one aspect-preserving frame and return it as PPM bytes.

    Tries hardware decode when available, falling back to software decode.
    """
    hwaccel = _hwaccel_args()
    raw = _extract_seek_frame_ppm_once(
        path, timestamp, hwaccel, frame_width=frame_width, cancelled=cancelled
    )
    if raw is None and hwaccel:
        raw = _extract_seek_frame_ppm_once(
            path, timestamp, (), frame_width=frame_width, cancelled=cancelled
        )
    return raw


def _extract_seek_frame_ppm_once(
    path: str | Path,
    timestamp: float,
    hwaccel: tuple[str, ...],
    *,
    frame_width: int,
    cancelled: Callable[[], bool] | None = None,
) -> bytes | None:
    cmd = [
        _ffmpeg_bin(),
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
    try:
        result = _run_video_command(cmd, timeout=30, cancelled=cancelled)
    except InterruptedError:
        raise
    except (TimeoutError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.startswith(b"P6"):
        return None
    return result.stdout


def _extract_seek_frames_ppm(
    path: str | Path,
    timestamps: list[float],
    *,
    frame_width: int,
    cancelled: Callable[[], bool] | None = None,
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
        path, timestamps, hwaccel, frame_width=frame_width, cancelled=cancelled
    )
    if not frames and hwaccel:
        frames = _extract_seek_frames_ppm_once(
            path, timestamps, (), frame_width=frame_width, cancelled=cancelled
        )
    return frames


def _extract_seek_frames_ppm_once(
    path: str | Path,
    timestamps: list[float],
    hwaccel: tuple[str, ...],
    *,
    frame_width: int,
    cancelled: Callable[[], bool] | None = None,
) -> list[bytes]:
    scale = f"scale={max(1, int(frame_width))}:-2"
    effective_hwaccel = hwaccel if len(timestamps) <= MAX_HWACCEL_MULTI_INPUTS else ()
    cmd = [_ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error"]
    for timestamp in timestamps:
        cmd += [*effective_hwaccel, "-ss", f"{timestamp:.6f}", "-threads", "1", "-i", str(path)]
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
        batch_timeout = min(120, max(30, 15 * len(timestamps)))
        try:
            result = _run_video_command(cmd, timeout=batch_timeout, cancelled=cancelled)
        except InterruptedError:
            raise
        except (TimeoutError, subprocess.TimeoutExpired):
            return []
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


@lru_cache(maxsize=1)
def _dct_phash_tables():
    """Precompute symmetrized 8×32 DCT-II basis and bit weights matching imagehash.phash."""
    import numpy as np

    size = HASH_FRAME_SIZE
    k = np.arange(8, dtype=np.float64)[:, None]
    n = np.arange(size, dtype=np.float64)[None, :]
    basis = 2.0 * np.cos(np.pi * k * (2.0 * n + 1.0) / (2.0 * size))
    for row in range(1, 8):
        half = basis[row, : size // 2]
        basis[row, size // 2 :] = half[::-1] if row % 2 == 0 else -half[::-1]
        if row % 2 == 0:
            basis[row] -= basis[row].mean()
    pow2 = (1 << np.arange(63, -1, -1, dtype=np.uint64))
    return basis, pow2


def _phash_raw_frame(raw: bytes, *, flip: bool = False) -> int:
    """Compute exact 64-bit pHash directly from a 32×32 grayscale buffer."""
    import numpy as np

    basis, pow2 = _dct_phash_tables()
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(HASH_FRAME_SIZE, HASH_FRAME_SIZE)
    if flip:
        arr = arr[:, ::-1]
    dct_low = (basis @ arr.astype(np.float64)) @ basis.T
    dct_low[np.abs(dct_low) < 1e-9] = 0.0
    med = np.median(dct_low)
    bits = (dct_low > med).reshape(64)
    return int(np.sum(pow2[bits], dtype=np.uint64))


def _phash_raw_frames_batch(raw_frames: Sequence[bytes], *, flip: bool = False) -> tuple[int, ...]:
    """Compute 64-bit pHashes for a batch of 32×32 grayscale buffers via NumPy DCT."""
    import numpy as np

    if not raw_frames:
        return ()
    basis, pow2 = _dct_phash_tables()
    stack = np.empty((len(raw_frames), HASH_FRAME_SIZE, HASH_FRAME_SIZE), dtype=np.float64)
    for idx, raw in enumerate(raw_frames):
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(HASH_FRAME_SIZE, HASH_FRAME_SIZE)
        stack[idx] = arr[:, ::-1] if flip else arr
    dct_low = np.matmul(np.matmul(basis, stack), basis.T)
    dct_low[np.abs(dct_low) < 1e-9] = 0.0
    meds = np.median(dct_low.reshape(len(raw_frames), 64), axis=1, keepdims=True)
    bits = dct_low.reshape(len(raw_frames), 64) > meds
    hashes = (bits.astype(np.uint64) * pow2[None, :]).sum(axis=1)
    return tuple(int(v) for v in hashes.tolist())


def _analyze_raw_frames(
    raw_frames: Sequence[bytes],
    *,
    primary_hashes: Sequence[int] | None = None,
) -> tuple[tuple[int, ...], tuple[int, ...], bool]:
    """Compute primary hashes, horizontally-flipped hashes, and informativeness."""
    import numpy as np

    if not raw_frames:
        return (), (), False
    hashes = (
        tuple(int(h) for h in primary_hashes)
        if primary_hashes is not None
        else _phash_raw_frames_batch(raw_frames, flip=False)
    )
    flipped_hashes = _phash_raw_frames_batch(raw_frames, flip=True)
    stds = [
        float(np.frombuffer(raw, dtype=np.uint8).std())
        for raw in raw_frames
    ]
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
    return hashes, flipped_hashes, informative


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
            consecutive_timeouts = 0
            for timestamp in timestamps:
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                try:
                    raw = _extract_hash_frame(
                        path, timestamp, software=software, cancelled=cancelled
                    )
                except InterruptedError:
                    raise
                except (TimeoutError, subprocess.TimeoutExpired):
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= MAX_CONSECUTIVE_SEEK_TIMEOUTS:
                        return None, width, height, duration, None
                    return None, width, height, duration, None
                if raw is None:
                    return None, width, height, duration, None
                consecutive_timeouts = 0
                raw_frames.append(raw)
        hashes = _phash_raw_frames_batch(raw_frames)
        if on_frame:
            for i in range(len(hashes)):
                on_frame(i + 1, len(timestamps))
        return (
            "v3:" + ",".join(f"{frame_hash:016x}" for frame_hash in hashes),
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
        hashes = _phash_raw_frames_batch(raw_frames)
        if on_frame:
            for idx in range(len(hashes)):
                on_frame(idx + 1, len(raw_frames))
        return (
            "v3:" + ",".join(f"{frame_hash:016x}" for frame_hash in hashes),
            width,
            height,
            duration,
            raw_frames,
        )

    with tempfile.TemporaryDirectory(prefix="dedupe-vid-") as tmp:
        frames = (
            _extract_frames(path, Path(tmp), duration=duration, cancelled=cancelled)
            if cancelled is not None
            else _extract_frames(path, Path(tmp), duration=duration)
        )
        if not frames:
            return None, width, height, duration, None

        hashes_list: list[int] = []
        fallback_raw: list[bytes] = []
        for idx, fp in enumerate(frames):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            try:
                with Image.open(fp) as img:
                    gray = img.convert("L").resize(
                        (HASH_FRAME_SIZE, HASH_FRAME_SIZE), Image.Resampling.LANCZOS
                    )
                    raw_buf = gray.tobytes()
                    fallback_raw.append(raw_buf)
                    hashes_list.append(_phash_raw_frame(raw_buf))
            except Exception:
                # Dropping a frame would shift the remaining sample positions
                # and allow an incomplete fingerprint to stand for the video.
                return None, width, height, duration, None
            else:
                if on_frame:
                    on_frame(idx + 1, len(frames))

        if not hashes_list:
            return None, width, height, duration, None

        return (
            "v3:" + ",".join(f"{frame_hash:016x}" for frame_hash in hashes_list),
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
        consecutive_timeouts = 0
        for ts in timestamps:
            try:
                raw = _extract_hash_frame(record.path, ts, cancelled=cancelled, vf_prefix=crop_vf)
            except InterruptedError:
                raise
            except (TimeoutError, subprocess.TimeoutExpired):
                consecutive_timeouts += 1
                if consecutive_timeouts >= MAX_CONSECUTIVE_SEEK_TIMEOUTS:
                    return None
                return None
            if raw is None:
                return None
            consecutive_timeouts = 0
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
    primary = _fingerprint_hashes(record.video_fingerprint or "")
    _hashes, flipped_hashes, informative = _analyze_raw_frames(
        raw_frames,
        primary_hashes=primary if len(primary) == len(raw_frames) else None,
    )
    _cached_video_features_put(record, flipped_hashes, informative)
    return flipped_hashes, informative


class _RgbThumbCache:
    """Thread-safe bounded singleflight cache for 32×32 RGB chroma frames."""

    def __init__(self, *, maxsize: int = 2048) -> None:
        self._lock = threading.Lock()
        self._maxsize = max(1, int(maxsize))
        self._entries: OrderedDict[tuple[str, str | None, tuple[float, ...]], list] = OrderedDict()

    def _evict_locked(self) -> None:
        if len(self._entries) <= self._maxsize:
            return
        for old_key in list(self._entries):
            if len(self._entries) <= self._maxsize:
                break
            entry = self._entries[old_key]
            if entry[0].is_set():
                self._entries.pop(old_key, None)

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
                entry = [threading.Event(), None, None]
                self._entries[key] = entry
                owner = True
            else:
                self._entries.move_to_end(key)
                owner = False
        if not owner:
            event = entry[0]
            while not event.wait(timeout=0.05):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
            if entry[2] is not None:
                raise entry[2]
            return entry[1] or []
        try:
            frames = _extract_rgb_thumbs(path, timestamps, crop_vf=crop_vf, cancelled=cancelled)
        except BaseException as exc:
            with self._lock:
                entry[2] = exc
                self._entries.pop(key, None)
            entry[0].set()
            raise
        with self._lock:
            entry[1] = frames
            self._entries.move_to_end(key)
            self._evict_locked()
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
    software = bool(getattr(_DECODE_LANE, "software", False))
    hwaccel = (
        ()
        if software or len(timestamps) > MAX_HWACCEL_MULTI_INPUTS
        else _hwaccel_args()
    )
    for active_hwaccel in ((hwaccel, ()) if hwaccel else ((),)):
        if len(timestamps) == 1:
            cmd = [
                _ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error",
                *active_hwaccel,
                "-ss", f"{timestamps[0]:.6f}", "-threads", "1", "-i", str(path),
                "-an", "-sn", "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "pipe:1",
            ]
        else:
            cmd = [_ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error"]
            for t in timestamps:
                cmd += [*active_hwaccel, "-ss", f"{t:.6f}", "-threads", "1", "-i", str(path)]
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
            result = _run_video_command(cmd, timeout=45, cancelled=cancelled)
        except InterruptedError:
            raise
        except Exception:
            continue
        if result.returncode == 0 and len(result.stdout) == len(timestamps) * frame_bytes:
            return [
                np.frombuffer(
                    result.stdout[i * frame_bytes : (i + 1) * frame_bytes], dtype=np.uint8
                ).reshape(HASH_FRAME_SIZE, HASH_FRAME_SIZE, 3).copy()
                for i in range(len(timestamps))
            ]
    return []


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

    # 2. Secondary variants: vectorized horizontally-flipped and border-cropped ROI hashes
    # across the upper triangle only (no redundant (u, v) and (v, u) double-testing).
    if features is not None and any(
        f is not None and (f.flipped_hashes is not None or f.cropped_hashes is not None)
        for f in features
    ):
        def _record_vec_hits(
            u: int,
            rel_positions,
            left_arr,
            right_mat,
            left_len: int,
            *,
            flip_b: bool,
            crop_u: str | None = None,
            uses_crop_u: bool = False,
            use_right_crop: bool = False,
        ) -> None:
            dists = _popcount64(right_mat ^ left_arr)
            means = dists.sum(axis=1) / left_len
            maxs = dists.max(axis=1)
            keep = np.flatnonzero(means <= verify_limit)
            for k_i in keep.tolist():
                c_pos = int(rel_positions[k_i])
                d_b = kd_durations_list[c_pos]
                if min(d_a, d_b) / max(d_a, d_b) < 0.9:
                    continue
                v = kd_indices[c_pos]
                pair_key = (u, v) if u < v else (v, u)
                if pair_key in direct_candidates:
                    continue
                crop_v = None
                uses_crop_v = False
                if use_right_crop:
                    feat_v = features[v]
                    if feat_v is not None and feat_v.cropped_hashes is not None:
                        crop_v = feat_v.crop_vf
                        uses_crop_v = crop_v is not None
                _record_pair(
                    u,
                    v,
                    float(means[k_i]),
                    int(maxs[k_i]),
                    flip_b=flip_b,
                    crop_u=crop_u,
                    crop_v=crop_v,
                    uses_crop_u=uses_crop_u,
                    uses_crop_v=uses_crop_v,
                )

        if known_durations:
            kd_flip_fps = np.zeros((k_count, width), dtype=np.uint64)
            kd_flip_len = np.zeros(k_count, dtype=np.int64)
            kd_crop_fps = np.zeros((k_count, width), dtype=np.uint64)
            kd_crop_len = np.zeros(k_count, dtype=np.int64)
            kd_crop_flip_fps = np.zeros((k_count, width), dtype=np.uint64)
            kd_crop_flip_len = np.zeros(k_count, dtype=np.int64)
            # Effective right-side matrix for cropped comparisons: cropped if available, else primary.
            kd_eff_fps = kd_fps.copy()
            kd_eff_len = kd_lengths.copy()
            kd_eff_flip_fps = np.zeros((k_count, width), dtype=np.uint64)
            kd_eff_flip_len = np.zeros(k_count, dtype=np.int64)

            for pos, idx in enumerate(kd_indices):
                feat = features[idx]
                if feat is None:
                    continue
                if feat.flipped_hashes:
                    fl = feat.flipped_hashes
                    kd_flip_fps[pos, : len(fl)] = fl
                    kd_flip_len[pos] = len(fl)
                    kd_eff_flip_fps[pos, : len(fl)] = fl
                    kd_eff_flip_len[pos] = len(fl)
                if feat.cropped_hashes:
                    cr = feat.cropped_hashes
                    kd_crop_fps[pos, : len(cr)] = cr
                    kd_crop_len[pos] = len(cr)
                    kd_eff_fps[pos, :] = 0
                    kd_eff_fps[pos, : len(cr)] = cr
                    kd_eff_len[pos] = len(cr)
                if feat.cropped_flipped_hashes:
                    cf = feat.cropped_flipped_hashes
                    kd_crop_flip_fps[pos, : len(cf)] = cf
                    kd_crop_flip_len[pos] = len(cf)
                    kd_eff_flip_fps[pos, :] = 0
                    kd_eff_flip_fps[pos, : len(cf)] = cf
                    kd_eff_flip_len[pos] = len(cf)

            has_any_crop = bool(np.any(kd_crop_len > 0))
            for pos, d_a in enumerate(kd_durations_list):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                end = bisect_right(kd_durations_list, d_a / 0.9 + 1e-9)
                if end <= pos + 1:
                    continue
                u = kd_indices[pos]
                feat_u = features[u]
                rel = np.arange(pos + 1, end, dtype=np.int64)

                # Flipped comparison (symmetric: check flipped[u] vs primary[v],
                # or primary[u] vs flipped[v] when only v has flipped_hashes).
                u_flip_len = int(kd_flip_len[pos])
                if u_flip_len > 0:
                    same = kd_lengths[rel] == u_flip_len
                    if np.any(same):
                        s_rel = rel[same]
                        _record_vec_hits(
                            u,
                            s_rel,
                            kd_flip_fps[pos : pos + 1, :u_flip_len],
                            kd_fps[s_rel, :u_flip_len],
                            u_flip_len,
                            flip_b=True,
                        )
                    if not np.all(same) and feat_u is not None and feat_u.flipped_hashes:
                        for c_pos in rel[~same].tolist():
                            d_b = kd_durations_list[c_pos]
                            if min(d_a, d_b) / max(d_a, d_b) < 0.9:
                                continue
                            v = kd_indices[c_pos]
                            if ((u, v) if u < v else (v, u)) not in direct_candidates:
                                _check_pair_sequences(
                                    u, v, feat_u.flipped_hashes, fingerprints[v], flip_b=True
                                )
                else:
                    v_has_flip = kd_flip_len[rel] > 0
                    if np.any(v_has_flip):
                        f_rel = rel[v_has_flip]
                        u_len = int(kd_lengths[pos])
                        same = kd_flip_len[f_rel] == u_len
                        if np.any(same):
                            s_rel = f_rel[same]
                            _record_vec_hits(
                                u,
                                s_rel,
                                kd_fps[pos : pos + 1, :u_len],
                                kd_flip_fps[s_rel, :u_len],
                                u_len,
                                flip_b=True,
                            )
                        if not np.all(same):
                            for c_pos in f_rel[~same].tolist():
                                d_b = kd_durations_list[c_pos]
                                if min(d_a, d_b) / max(d_a, d_b) < 0.9:
                                    continue
                                v = kd_indices[c_pos]
                                feat_v = features[v]
                                if (
                                    feat_v is not None
                                    and feat_v.flipped_hashes is not None
                                    and ((u, v) if u < v else (v, u)) not in direct_candidates
                                ):
                                    _check_pair_sequences(
                                        u, v, fingerprints[u], feat_v.flipped_hashes, flip_b=True
                                    )

                if not has_any_crop:
                    continue

                # Border-cropped ROI comparisons (upper triangle, handling u-cropped and/or v-cropped).
                u_crop_len = int(kd_crop_len[pos])
                if u_crop_len > 0 and feat_u is not None and feat_u.cropped_hashes is not None:
                    same = kd_eff_len[rel] == u_crop_len
                    if np.any(same):
                        s_rel = rel[same]
                        _record_vec_hits(
                            u,
                            s_rel,
                            kd_crop_fps[pos : pos + 1, :u_crop_len],
                            kd_eff_fps[s_rel, :u_crop_len],
                            u_crop_len,
                            flip_b=False,
                            crop_u=feat_u.crop_vf,
                            uses_crop_u=True,
                            use_right_crop=True,
                        )
                    if not np.all(same):
                        for c_pos in rel[~same].tolist():
                            d_b = kd_durations_list[c_pos]
                            if min(d_a, d_b) / max(d_a, d_b) < 0.9:
                                continue
                            v = kd_indices[c_pos]
                            feat_v = features[v]
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
                    u_cf_len = int(kd_crop_flip_len[pos])
                    if u_cf_len > 0:
                        same_cf = kd_eff_len[rel] == u_cf_len
                        if np.any(same_cf):
                            s_rel = rel[same_cf]
                            _record_vec_hits(
                                u,
                                s_rel,
                                kd_crop_flip_fps[pos : pos + 1, :u_cf_len],
                                kd_eff_fps[s_rel, :u_cf_len],
                                u_cf_len,
                                flip_b=True,
                                crop_u=feat_u.crop_vf,
                                uses_crop_u=True,
                                use_right_crop=True,
                            )
                    else:
                        v_eff_flip = kd_eff_flip_len[rel] == u_crop_len
                        if np.any(v_eff_flip):
                            s_rel = rel[v_eff_flip]
                            _record_vec_hits(
                                u,
                                s_rel,
                                kd_crop_fps[pos : pos + 1, :u_crop_len],
                                kd_eff_flip_fps[s_rel, :u_crop_len],
                                u_crop_len,
                                flip_b=True,
                                crop_u=feat_u.crop_vf,
                                uses_crop_u=True,
                                use_right_crop=True,
                            )
                else:
                    v_has_crop = kd_crop_len[rel] > 0
                    if np.any(v_has_crop):
                        c_rel = rel[v_has_crop]
                        u_len = int(kd_lengths[pos])
                        same = kd_crop_len[c_rel] == u_len
                        if np.any(same):
                            s_rel = c_rel[same]
                            _record_vec_hits(
                                u,
                                s_rel,
                                kd_fps[pos : pos + 1, :u_len],
                                kd_crop_fps[s_rel, :u_len],
                                u_len,
                                flip_b=False,
                                use_right_crop=True,
                            )
                        same_cf = kd_crop_flip_len[c_rel] == u_len
                        if np.any(same_cf):
                            s_rel = c_rel[same_cf]
                            _record_vec_hits(
                                u,
                                s_rel,
                                kd_fps[pos : pos + 1, :u_len],
                                kd_crop_flip_fps[s_rel, :u_len],
                                u_len,
                                flip_b=True,
                                use_right_crop=True,
                            )
                        elif u_flip_len > 0:
                            same_uf = kd_crop_len[c_rel] == u_flip_len
                            if np.any(same_uf):
                                s_rel = c_rel[same_uf]
                                _record_vec_hits(
                                    u,
                                    s_rel,
                                    kd_flip_fps[pos : pos + 1, :u_flip_len],
                                    kd_crop_fps[s_rel, :u_flip_len],
                                    u_flip_len,
                                    flip_b=True,
                                    use_right_crop=True,
                                )

        # Secondary variants involving duration-unknown records (typically very small).
        if unknown_indexes:
            unknown_set = set(unknown_indexes)
            for u in range(len(hashed)):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                feat_u = features[u]
                if feat_u is None or (
                    feat_u.flipped_hashes is None and feat_u.cropped_hashes is None
                ):
                    continue
                targets = (
                    [idx for idx in range(u + 1, len(hashed))]
                    if u in unknown_set
                    else unknown_indexes
                )
                for v in targets:
                    if v == u:
                        continue
                    pair_key = (u, v) if u < v else (v, u)
                    if pair_key in direct_candidates:
                        continue
                    feat_v = features[v]
                    if feat_u.flipped_hashes is not None:
                        _check_pair_sequences(
                            u, v, feat_u.flipped_hashes, fingerprints[v], flip_b=True
                        )
                    if feat_u.cropped_hashes is not None:
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
    """Thread-safe bounded per-frame cache with singleflight batch deduplication.

    Caches decoded 32×32 grayscale frames individually by ``(path, vf_prefix, rounded_ts)``
    so subset lookups (such as the 3 middle anchors in ``_refine_and_verify_offset``)
    hit frames already decoded by a 7-frame verification pass.
    """

    def __init__(self, *, max_frames: int = 16384) -> None:
        self._lock = threading.Lock()
        self._max_frames = max(64, int(max_frames))
        self._frames: OrderedDict[tuple[str, str | None, float], bytes] = OrderedDict()
        self._inflight: dict[tuple[str, str | None, tuple[float, ...]], list] = {}

    def _evict_locked(self) -> None:
        while len(self._frames) > self._max_frames:
            self._frames.popitem(last=False)

    def get(
        self,
        path: str,
        timestamps: list[float],
        cancelled: Callable[[], bool] | None,
        *,
        vf_prefix: str | None = None,
    ) -> list[bytes]:
        if not timestamps:
            return []
        rounded = tuple(round(float(t), 4) for t in timestamps)
        frame_keys = [(path, vf_prefix, rt) for rt in rounded]

        with self._lock:
            if all(k in self._frames for k in frame_keys):
                out = []
                for k in frame_keys:
                    self._frames.move_to_end(k)
                    out.append(self._frames[k])
                return out

            # Check if only a strict subset of timestamps is missing; when running the
            # real extractor, fetch only the missing timestamps and assemble from cache.
            cached_hits = {
                idx: self._frames[k]
                for idx, k in enumerate(frame_keys)
                if k in self._frames
            }
            if (
                cached_hits
                and _version_frame_batches is _ORIGINAL_VERSION_FRAME_BATCHES
            ):
                missing_indices = [i for i in range(len(timestamps)) if i not in cached_hits]
                fetch_timestamps = [timestamps[i] for i in missing_indices]
                fetch_rounded = tuple(rounded[i] for i in missing_indices)
            else:
                cached_hits = {}
                missing_indices = list(range(len(timestamps)))
                fetch_timestamps = list(timestamps)
                fetch_rounded = rounded

            batch_key = (path, vf_prefix, fetch_rounded)
            entry = self._inflight.get(batch_key)
            if entry is None:
                entry = [threading.Event(), None, None]
                self._inflight[batch_key] = entry
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
            fetched = entry[1]
        else:
            try:
                fetched = (
                    _version_frame_batches(
                        path, fetch_timestamps, cancelled, vf_prefix=vf_prefix
                    )
                    if vf_prefix is not None
                    else _version_frame_batches(path, fetch_timestamps, cancelled)
                )
            except BaseException as exc:
                with self._lock:
                    entry[2] = exc
                    self._inflight.pop(batch_key, None)
                entry[0].set()
                raise
            with self._lock:
                entry[1] = fetched
                for rt, raw in zip(fetch_rounded, fetched, strict=True):
                    f_key = (path, vf_prefix, rt)
                    self._frames[f_key] = raw
                    self._frames.move_to_end(f_key)
                self._evict_locked()
                self._inflight.pop(batch_key, None)
            entry[0].set()

        if not cached_hits:
            return list(fetched)
        assembled: list[bytes] = [b""] * len(timestamps)
        for idx, raw in cached_hits.items():
            assembled[idx] = raw
        for idx, raw in zip(missing_indices, fetched, strict=True):
            assembled[idx] = raw
        return assembled


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
                        primary = _fingerprint_hashes(fp or "")
                        _hs, flipped_hashes, informative = _analyze_raw_frames(
                            raw_frames,
                            primary_hashes=primary if len(primary) == len(raw_frames) else None,
                        )
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
            ha = _phash_raw_frames_batch(fa, flip=False)
            hb = _phash_raw_frames_batch(fb, flip=flip_b)
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
    command: list[str],
    *,
    timeout: float = 90,
    cancelled: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess:
    if cancelled is None:
        return subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if cancelled():
        raise InterruptedError("scan cancelled")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    completed = False
    try:
        while True:
            if cancelled():
                raise InterruptedError("scan cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("video analysis timed out")
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                completed = True
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if not completed:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.communicate(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        for stream in (process.stdout, process.stderr):
                            if stream is not None:
                                try:
                                    stream.close()
                                except OSError:
                                    pass
            else:
                try:
                    process.communicate(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass


def _version_timestamps(duration: float) -> list[float]:
    count = min(VERSION_SAMPLE_LIMIT, max(1, math.ceil(duration / VERSION_SAMPLE_STEP)))
    step = duration / count
    # Stay away from boundary frames which can be black or absent after an edit,
    # and clamp slightly inside EOF so truncated/audio-longer tails don't fail seeks.
    max_ts = max(0.0, duration - 0.5) if duration > 1.0 else max(0.0, duration - 0.05)
    return [min((i + 0.25) * step, max_ts) for i in range(count)]


def _extract_version_frames_with_tolerance(
    path: str,
    timestamps: list[float],
    cancelled: Callable[[], bool] | None,
    *,
    vf_prefix: str | None = None,
    allow_partial: bool = False,
) -> tuple[list[float], list[bytes]]:
    """Extract dense version frames, preferring 1 sequential decode on short/medium videos."""
    if not timestamps:
        return [], []
    software = bool(getattr(_DECODE_LANE, "software", False))
    hwaccel = () if software else _hwaccel_args()

    # On short/medium videos (<= 90s or dense step <= 2.5s), a single sequential
    # pass uses 1 demuxer + 1 decoder instead of dozens of keyframe seeks/VT sessions.
    if (
        _extract_hash_frames_once is _ORIGINAL_EXTRACT_HASH_FRAMES_ONCE
        and len(timestamps) >= 8
        and timestamps[-1] <= VERSION_SINGLE_PASS_MAX_DURATION
        and all(timestamps[i] < timestamps[i + 1] for i in range(len(timestamps) - 1))
    ):
        avg_step = (timestamps[-1] - timestamps[0]) / max(1, len(timestamps) - 1)
        if avg_step <= VERSION_SINGLE_PASS_MAX_STEP:
            try:
                seq_frames = _extract_sequential_hash_frames(
                    path,
                    timestamps,
                    software=software,
                    cancelled=cancelled,
                    vf_prefix=vf_prefix,
                    timeout=120,
                )
            except InterruptedError:
                raise
            except (TimeoutError, subprocess.TimeoutExpired):
                seq_frames = []
            if len(seq_frames) == len(timestamps):
                return list(timestamps), seq_frames

    kept_ts: list[float] = []
    frames: list[bytes] = []
    min_required = (
        max(3, math.ceil(len(timestamps) * 0.9))
        if allow_partial and len(timestamps) >= 10
        else len(timestamps)
    )
    max_missing = len(timestamps) - min_required
    missing = 0

    for start in range(0, len(timestamps), VERSION_BATCH_SIZE):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        chunk = timestamps[start : start + VERSION_BATCH_SIZE]
        try:
            chunk_frames = _extract_hash_frames_once(
                path, chunk, hwaccel, cancelled=cancelled, vf_prefix=vf_prefix
            )
        except InterruptedError:
            raise
        except (TimeoutError, subprocess.TimeoutExpired):
            chunk_frames = []
        if not chunk_frames and hwaccel:
            hwaccel = ()
            try:
                chunk_frames = _extract_hash_frames_once(
                    path, chunk, (), cancelled=cancelled, vf_prefix=vf_prefix
                )
            except InterruptedError:
                raise
            except (TimeoutError, subprocess.TimeoutExpired):
                chunk_frames = []
        if len(chunk_frames) == len(chunk):
            kept_ts.extend(chunk)
            frames.extend(chunk_frames)
            continue

        consecutive_timeouts = 0
        for ts in chunk:
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            try:
                raw = _extract_hash_frame(
                    path, ts, software=True, cancelled=cancelled, vf_prefix=vf_prefix
                )
                consecutive_timeouts = 0
            except InterruptedError:
                raise
            except (TimeoutError, subprocess.TimeoutExpired):
                consecutive_timeouts += 1
                raw = None
                if consecutive_timeouts >= MAX_CONSECUTIVE_SEEK_TIMEOUTS:
                    raise TimeoutError("video analysis timed out") from None
            if raw is None:
                missing += 1
                if missing > max_missing:
                    raise ValueError("incomplete video version samples")
                continue
            kept_ts.append(ts)
            frames.append(raw)

    if len(frames) < min_required:
        raise ValueError("incomplete video version samples")
    return kept_ts, frames


def _version_frame_batches(
    path: str,
    timestamps: list[float],
    cancelled: Callable[[], bool] | None,
    *,
    vf_prefix: str | None = None,
) -> list[bytes]:
    _kept_ts, frames = _extract_version_frames_with_tolerance(
        path, timestamps, cancelled, vf_prefix=vf_prefix, allow_partial=False
    )
    return frames


_ORIGINAL_VERSION_FRAME_BATCHES = _version_frame_batches


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
    import numpy as np

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
        if _version_frame_batches is _ORIGINAL_VERSION_FRAME_BATCHES:
            sample_timestamps, frames = _extract_version_frames_with_tolerance(
                record.path, timestamps, cancelled, allow_partial=True
            )
        else:
            frames = _version_frame_batches(record.path, timestamps, cancelled)
            sample_timestamps = timestamps
        keep_ts: list[float] = []
        keep_raw: list[bytes] = []
        for timestamp, raw in zip(sample_timestamps, frames, strict=True):
            # Blank frames and nearly uniform fades are not useful candidate evidence.
            if float(np.frombuffer(raw, dtype=np.uint8).std()) < 8:
                continue
            keep_ts.append(timestamp)
            keep_raw.append(raw)
        if not keep_raw:
            return duration, width, height, [], None
        hashes = _phash_raw_frames_batch(keep_raw)
        extracted = list(zip(keep_ts, hashes, strict=True))
        return duration, width, height, extracted, None
    except InterruptedError:
        raise
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return None, None, None, [], str(exc)


# Hash pairs compared per batch of the candidate join: bounds its working
# arrays to tens of megabytes however many videos a scan holds.
_VERSION_JOIN_BATCH_PAIRS = 1 << 21
VERSION_CANDIDATE_MAX_VIDEOS = 256
VERSION_CANDIDATE_HAMMING_LIMIT = 10
MAX_SAME_HASH_PER_VIDEO = 4
# Exact Multi-Index Hashing partition of 64-bit hashes into 6 disjoint blocks
# (four 11-bit + two 10-bit = 64 bits). By the pigeonhole principle, any pair
# within Hamming distance <= 10 must share at least one block with <= 1 differing bit.
_MIH_BLOCKS: tuple[tuple[int, int, int], ...] = (
    (0, 11, (1 << 11) - 1),
    (11, 11, (1 << 11) - 1),
    (22, 11, (1 << 11) - 1),
    (33, 11, (1 << 11) - 1),
    (44, 10, (1 << 10) - 1),
    (54, 10, (1 << 10) - 1),
)


def _dedupe_timeline_samples(
    timeline: list[tuple[float, int]],
) -> list[tuple[float, int]]:
    """Collapse consecutive near-identical hashes and cap alternating repetitions per video."""
    kept: list[tuple[float, int]] = []
    counts: dict[int, int] = defaultdict(int)
    for timestamp, fingerprint in timeline:
        if kept and (fingerprint ^ kept[-1][1]).bit_count() <= 2:
            continue
        if counts[fingerprint] >= MAX_SAME_HASH_PER_VIDEO:
            continue
        counts[fingerprint] += 1
        kept.append((timestamp, fingerprint))
    return kept


def _mih_hamming_neighbors(
    values,
    weights,
    *,
    max_dist: int = VERSION_CANDIDATE_HAMMING_LIMIT,
    max_degree_weight: int = VERSION_CANDIDATE_MAX_VIDEOS,
    query_indices: Sequence[int] | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[int, list[int]], list[int]]:
    """Exact Multi-Index Hashing for 64-bit Hamming search (<= 10 bits).

    Returns ``(neighbors_by_idx, degree_weights_list)`` where ``neighbors_by_idx[r]``
    contains sorted neighbor key indices ``c`` (ordered by ``(dist, c)``) only for
    keys whose total matched distinct-video degree weight is ``<= max_degree_weight``.
    """
    import numpy as np

    n_keys = len(values)
    if n_keys == 0:
        return {}, []
    popcounts = _popcount64(values).astype(np.int16)

    # Build 6 compact CSR-like bucket tables (one per disjoint 10/11-bit block).
    tables: list[list[object]] = []
    for shift, width, mask in _MIH_BLOCKS:
        sub = ((values >> np.uint64(shift)) & np.uint64(mask)).astype(np.int32)
        order = np.argsort(sub, kind="mergesort")
        sorted_sub = sub[order]
        n_buckets = 1 << width
        starts = np.searchsorted(sorted_sub, np.arange(n_buckets, dtype=np.int32), side="left")
        ends = np.searchsorted(sorted_sub, np.arange(n_buckets, dtype=np.int32), side="right")
        order32 = order.astype(np.int32)
        table: list[object] = [None] * n_buckets
        for b in range(n_buckets):
            s_i, e_i = int(starts[b]), int(ends[b])
            if e_i > s_i:
                table[b] = order32[s_i:e_i]
        tables.append(table)

    full_scan = query_indices is None
    queries = range(n_keys) if full_scan else sorted(set(query_indices))
    total_queries = len(queries)
    degree_weights = np.zeros(n_keys, dtype=np.int64)
    # Compact neighbor storage: only keep keys that have not exceeded the generic cap.
    upper_cols: list[object] = [None] * n_keys
    upper_dists: list[object] = [None] * n_keys
    # Incremental query mode records directed neighbors for queried keys directly.
    direct_neighbors: dict[int, list[tuple[int, int]]] = {}

    progress_step = max(256, total_queries // 40) if total_queries else 1
    for q_pos, r in enumerate(queries):
        if (q_pos & 255) == 0 and cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        if progress and q_pos and (q_pos % progress_step == 0):
            progress(q_pos, total_queries)

        val_r = values[r]
        val_r_int = int(val_r)
        pc_r = int(popcounts[r])
        parts = []
        for table, (shift, width, mask) in zip(tables, _MIH_BLOCKS, strict=True):
            sub_r = (val_r_int >> shift) & mask
            bucket = table[sub_r]
            if bucket is not None:
                parts.append(bucket)
            for bit in range(width):
                nb = table[sub_r ^ (1 << bit)]
                if nb is not None:
                    parts.append(nb)
        if not parts:
            continue
        cands = np.concatenate(parts)
        if full_scan:
            cands = cands[cands >= r]
        if not cands.size:
            continue
        # Fast popcount-band prefilter before XOR + popcount.
        band = np.abs(popcounts[cands] - pc_r) <= max_dist
        if not np.any(band):
            continue
        cands = cands[band]
        dists = _popcount64(values[cands] ^ val_r)
        hit_mask = dists <= max_dist
        if not np.any(hit_mask):
            continue
        hit_cols = cands[hit_mask]
        hit_dists = dists[hit_mask]
        # Deduplicate hits found across multiple MIH blocks (only on small hit set!).
        if hit_cols.size > 1:
            uniq_cols, first_idx = np.unique(hit_cols, return_index=True)
            hit_cols = uniq_cols
            hit_dists = hit_dists[first_idx]

        if full_scan:
            w_r = int(weights[r])
            hit_weight = int(weights[hit_cols].sum())
            degree_weights[r] += hit_weight
            off_diag = hit_cols > r
            if np.any(off_diag):
                np.add.at(degree_weights, hit_cols[off_diag], w_r)
            # Prune generic high-degree hubs BEFORE storing tuples in RAM.
            if int(degree_weights[r]) <= max_degree_weight:
                upper_cols[r] = hit_cols.astype(np.int32, copy=False)
                upper_dists[r] = hit_dists.astype(np.uint8, copy=False)
            elif np.any(off_diag):
                # Key r itself is generic, but an off-diagonal neighbor c might still
                # have degree <= max_degree_weight unless r alone exceeds the cap.
                if w_r <= max_degree_weight and int(hit_cols.size) <= max_degree_weight * 4:
                    upper_cols[r] = hit_cols[off_diag].astype(np.int32, copy=False)
                    upper_dists[r] = hit_dists[off_diag].astype(np.uint8, copy=False)
        else:
            hit_weight = int(weights[hit_cols].sum())
            degree_weights[r] = hit_weight
            if hit_weight <= max_degree_weight:
                direct_neighbors[r] = sorted(
                    zip(hit_dists.tolist(), hit_cols.tolist(), strict=True)
                )

    if progress and total_queries:
        progress(total_queries, total_queries)

    if not full_scan:
        out = {
            r: [c_idx for _d, c_idx in pairs]
            for r, pairs in direct_neighbors.items()
        }
        return out, degree_weights.tolist()

    neighbor_dists: list[list[tuple[int, int]]] = [[] for _ in range(n_keys)]
    for r in range(n_keys):
        cols = upper_cols[r]
        if cols is None:
            continue
        dists_r = upper_dists[r]
        r_ok = int(degree_weights[r]) <= max_degree_weight
        for c_idx, dist_val in zip(cols.tolist(), dists_r.tolist(), strict=True):
            if r_ok:
                neighbor_dists[r].append((dist_val, c_idx))
            if c_idx != r and int(degree_weights[c_idx]) <= max_degree_weight:
                neighbor_dists[c_idx].append((dist_val, r))

    neighbors_by_idx: dict[int, list[int]] = {}
    for r, items in enumerate(neighbor_dists):
        if items:
            items.sort(key=lambda pair: (pair[0], pair[1]))
            neighbors_by_idx[r] = [c_idx for _dist, c_idx in items]
    return neighbors_by_idx, degree_weights.tolist()


def _consistent_offset_votes(
    votes: list[tuple[float, float]],
    bin_step: float,
    *,
    duration_a: float | None = None,
    duration_b: float | None = None,
) -> list[tuple[float, float]]:
    """Keep only votes belonging to temporally consistent offset clusters (>= 3 distinct ta/tb)."""
    if len(votes) < 3:
        return []
    bins: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for ta, tb in votes:
        bins[round((tb - ta) / bin_step)].append((ta, tb))
    good_bins: set[int] = set()
    for bin_id in bins:
        # Include immediate neighbor bins so off-grid cuts straddling a bin boundary stay intact.
        window: list[tuple[float, float]] = []
        for adj in (bin_id - 1, bin_id, bin_id + 1):
            if adj in bins:
                window.extend(bins[adj])
        if len(window) < 3:
            continue
        if len({ta for ta, _tb in window}) < 3 or len({tb for _ta, tb in window}) < 3:
            continue
        if duration_a and duration_b and duration_a > 0 and duration_b > 0:
            offsets_w = sorted(tb - ta for ta, tb in window)
            med_offset = offsets_w[len(offsets_w) // 2]
            start = max(0.0, -med_offset)
            end = min(duration_a, duration_b - med_offset)
            overlap = end - start
            min_allowed = min(VERSION_MIN_SECONDS, min(duration_a, duration_b)) * 0.75
            if overlap < min_allowed or overlap < min(duration_a, duration_b) * 0.65:
                continue
        good_bins.add(bin_id)
    if not good_bins:
        return []
    consistent: list[tuple[float, float]] = []
    for bin_id in good_bins:
        consistent.extend(bins[bin_id])
    return consistent


def _version_candidate_pairs(
    samples: list[list[tuple[float, int]]],
    cancelled: Callable[[], bool] | None = None,
    *,
    durations: Sequence[float | None] | None = None,
    arrival_indices: set[int] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """Index frame hashes with exact MIH and prune temporally inconsistent pairs."""
    import numpy as np

    deduped = [_dedupe_timeline_samples(timeline) for timeline in samples]
    occurrences: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for index, timeline in enumerate(deduped):
        for timestamp, fingerprint in timeline:
            occurrences[fingerprint].append((index, timestamp))
    if not occurrences:
        return {}
    keys = sorted(occurrences)
    key_to_idx = {key: idx for idx, key in enumerate(keys)}
    values = np.array(keys, dtype=np.uint64)
    # Count distinct videos carrying each hash so repeated frames in slow/tripod
    # recordings never trip the library-wide generic-frame cap.
    weights = np.array(
        [len({idx for idx, _ in occurrences[key]}) for key in keys],
        dtype=np.int64,
    )

    query_key_indices: list[int] | None = None
    if arrival_indices is not None:
        query_set: set[int] = set()
        for idx in arrival_indices:
            if 0 <= idx < len(deduped):
                for _ts, fp in deduped[idx]:
                    k_idx = key_to_idx.get(fp)
                    if k_idx is not None:
                        query_set.add(k_idx)
        if not query_set:
            return {}
        query_key_indices = sorted(query_set)

    neighbors_by_idx, _degree_weights = _mih_hamming_neighbors(
        values,
        weights,
        max_dist=VERSION_CANDIDATE_HAMMING_LIMIT,
        max_degree_weight=VERSION_CANDIDATE_MAX_VIDEOS,
        query_indices=query_key_indices,
        cancelled=cancelled,
        progress=progress,
    )
    near: dict[int, list[int]] = {
        keys[r]: [keys[c_idx] for c_idx in cols]
        for r, cols in neighbors_by_idx.items()
    }

    candidates: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    if arrival_indices is None:
        for i, timeline in enumerate(deduped):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            for ta, fingerprint in timeline:
                for matched in near.get(fingerprint, ()):
                    for j, tb in occurrences[matched]:
                        if j > i:
                            candidates[(i, j)].append((ta, tb))
    else:
        for i in sorted(arrival_indices):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            if not (0 <= i < len(deduped)):
                continue
            for ta, fingerprint in deduped[i]:
                for matched in near.get(fingerprint, ()):
                    for j, tb in occurrences[matched]:
                        if j == i:
                            continue
                        if j in arrival_indices and j < i:
                            continue
                        if i < j:
                            candidates[(i, j)].append((ta, tb))
                        else:
                            candidates[(j, i)].append((tb, ta))

    filtered: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for (i, j), votes in candidates.items():
        if len(votes) < 3:
            continue
        da = float(durations[i]) if durations is not None and durations[i] else None
        db = float(durations[j]) if durations is not None and durations[j] else None
        if da and db:
            bin_step = max(0.25, max(da, db) / VERSION_SAMPLE_LIMIT)
        else:
            max_t = max(
                deduped[i][-1][0] if deduped[i] else 0.0,
                deduped[j][-1][0] if deduped[j] else 0.0,
            )
            bin_step = max(0.25, max_t / VERSION_SAMPLE_LIMIT)
        consistent = _consistent_offset_votes(
            votes, bin_step, duration_a=da, duration_b=db
        )
        if consistent:
            filtered[(i, j)] = consistent
    return filtered


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


def _score_verified_frame_pair(
    ra: bytes,
    rb: bytes,
    *,
    flip_b: bool = False,
) -> tuple[int, int, bool, bool]:
    """Compare one aligned 32×32 frame pair -> ``(hash_a, distance, ok, non_blank)``."""
    import numpy as np

    aa = np.frombuffer(ra, np.uint8).astype(np.float64)
    ab = np.frombuffer(rb, np.uint8).astype(np.float64)
    if flip_b:
        ab = np.ascontiguousarray(ab.reshape(HASH_FRAME_SIZE, HASH_FRAME_SIZE)[:, ::-1]).reshape(-1)
    ha = _phash_raw_frame(ra, flip=False)
    hb = _phash_raw_frame(rb, flip=flip_b)
    dist = (ha ^ hb).bit_count()
    std_a, std_b = float(aa.std()), float(ab.std())
    non_blank = std_a >= MIN_FRAME_LUMA_STD and std_b >= MIN_FRAME_LUMA_STD
    if std_a < 2.0 and std_b < 2.0:
        ok = abs(float(aa.mean() - ab.mean())) <= 18.0
    elif std_a < 2.0 or std_b < 2.0:
        ok = False
    else:
        za = (aa - float(aa.mean())) / std_a
        zb = (ab - float(ab.mean())) / std_b
        ncc = float(np.mean(za * zb))
        z_mad = float(np.mean(np.abs(za - zb)))
        ok = ncc >= MIN_FRAME_NCC and z_mad <= MAX_FRAME_ZMAD
    return ha, dist, ok, non_blank


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
    da, db = float(a.duration or 0), float(b.duration or 0)
    start = max(0.0, -offset)
    end = min(da, db - offset)
    overlap = end - start
    if overlap < min_overlap or overlap < min(da, db) * 0.8:
        return None
    times = [start + overlap * fraction for fraction in (0.08, 0.22, 0.36, 0.5, 0.64, 0.78, 0.92)]
    right_times = [t + offset for t in times]

    def _fetch(path: str, ts: list[float], crop_vf: str | None) -> list[bytes]:
        if frame_cache is not None:
            return frame_cache.get(path, ts, cancelled, vf_prefix=crop_vf)
        return (
            _version_frame_batches(path, ts, cancelled, vf_prefix=crop_vf)
            if crop_vf is not None
            else _version_frame_batches(path, ts, cancelled)
        )

    # Exact 3-anchor early-reject when running real decoding: passing requires
    # >= 6 of 7 frames good AND mean distance <= 7 (sum <= 49), so failing >= 2
    # of the 3 middle anchors (or sum > 49) makes passing impossible while still
    # populating the exact 3 anchors that _refine_and_verify_offset reuses on A.
    if _version_frame_batches is _ORIGINAL_VERSION_FRAME_BATCHES:
        anchor_idxs = (1, 3, 5)
        rest_idxs = (0, 2, 4, 6)
        anchor_left = _fetch(a.path, [times[i] for i in anchor_idxs], crop_a)
        anchor_right = _fetch(b.path, [right_times[i] for i in anchor_idxs], crop_b)
        anchor_scored = [
            _score_verified_frame_pair(ra, rb, flip_b=flip_b)
            for ra, rb in zip(anchor_left, anchor_right, strict=True)
        ]
        anchor_good = sum(1 for _h, d, ok, _nb in anchor_scored if d <= 10 and ok)
        anchor_dist_sum = sum(d for _h, d, _ok, _nb in anchor_scored)
        if anchor_good < 2 or anchor_dist_sum > 49:
            return None
        rest_left = _fetch(a.path, [times[i] for i in rest_idxs], crop_a)
        rest_right = _fetch(b.path, [right_times[i] for i in rest_idxs], crop_b)
        rest_scored = [
            _score_verified_frame_pair(ra, rb, flip_b=flip_b)
            for ra, rb in zip(rest_left, rest_right, strict=True)
        ]
        scored = [None] * 7
        for pos, val in zip(anchor_idxs, anchor_scored, strict=True):
            scored[pos] = val
        for pos, val in zip(rest_idxs, rest_scored, strict=True):
            scored[pos] = val
    else:
        left = _fetch(a.path, times, crop_a)
        right = _fetch(b.path, right_times, crop_b)
        scored = [
            _score_verified_frame_pair(ra, rb, flip_b=flip_b)
            for ra, rb in zip(left, right, strict=True)
        ]

    hashes = [item[0] for item in scored]  # type: ignore[index]
    distances = [item[1] for item in scored]  # type: ignore[index]
    frame_oks = [item[2] for item in scored]  # type: ignore[index]
    non_blank = sum(1 for item in scored if item[3])  # type: ignore[index]

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
    skip_initial_verify: bool = False,
) -> dict | None:
    """Refine ``base_offset`` locally around the bin median before verifying."""
    import numpy as np

    if not skip_initial_verify:
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

    # Reuse the 3 middle anchor timestamps already decoded on A via per-frame _FrameBatchCache.
    anchors = [start + overlap * fraction for fraction in (0.22, 0.5, 0.78)]
    if frame_cache is not None:
        left_frames = frame_cache.get(a.path, anchors, cancelled, vf_prefix=crop_a)
    else:
        left_frames = (
            _version_frame_batches(a.path, anchors, cancelled, vf_prefix=crop_a)
            if crop_a is not None
            else _version_frame_batches(a.path, anchors, cancelled)
        )

    left_hashes = _phash_raw_frames_batch(left_frames, flip=False)
    left_norms: list[tuple[int, object, float]] = []
    for hv, raw in zip(left_hashes, left_frames, strict=True):
        arr = np.frombuffer(raw, np.uint8).astype(np.float64)
        std = float(arr.std())
        norm = (arr - float(arr.mean())) / std if std >= 2.0 else None
        left_norms.append((hv, norm, std))

    valid_deltas = [
        d for d in deltas if all(0.0 <= ta + base_offset + d < db for ta in anchors)
    ]
    if not valid_deltas:
        return None

    def _fetch_b(ts: list[float]) -> list[bytes]:
        if frame_cache is not None:
            return frame_cache.get(b.path, ts, cancelled, vf_prefix=crop_b)
        return (
            _version_frame_batches(b.path, ts, cancelled, vf_prefix=crop_b)
            if crop_b is not None
            else _version_frame_batches(b.path, ts, cancelled)
        )

    # Fast 1-anchor pre-screen across deltas (12 frames instead of 36) when a
    # non-blank anchor exists, then confirm top 3 candidate deltas with all 3 anchors.
    probe_deltas = valid_deltas
    if len(valid_deltas) > 3 and _version_frame_batches is _ORIGINAL_VERSION_FRAME_BATCHES:
        screen_idx = max(range(len(anchors)), key=lambda i: left_norms[i][2])
        screen_hv, _screen_norm, screen_std = left_norms[screen_idx]
        if screen_std >= MIN_FRAME_LUMA_STD:
            screen_ta = anchors[screen_idx]
            screen_times = [screen_ta + base_offset + d for d in valid_deltas]
            screen_frames = _fetch_b(screen_times)
            screen_hashes = _phash_raw_frames_batch(screen_frames, flip=flip_b)
            ranked = sorted(
                (
                    ((screen_hv ^ hb).bit_count(), abs(d), d)
                    for d, hb in zip(valid_deltas, screen_hashes, strict=True)
                )
            )
            if ranked and ranked[0][0] > 18:
                return None
            probe_deltas = [item[2] for item in ranked[:3]]

    probe_times = [ta + base_offset + d for d in probe_deltas for ta in anchors]
    probe_frames = _fetch_b(probe_times)
    probe_hashes = _phash_raw_frames_batch(probe_frames, flip=flip_b)

    best_delta: float | None = None
    best_score: tuple[float, float] = (65.0, 1.0)
    for idx_d, delta in enumerate(probe_deltas):
        base_i = idx_d * 3
        chunk = probe_frames[base_i : base_i + 3]
        chunk_hashes = probe_hashes[base_i : base_i + 3]
        dists = []
        nccs = []
        for (hv_a, norm_a, _std_a), rb, hv_b in zip(
            left_norms, chunk, chunk_hashes, strict=True
        ):
            dists.append((hv_a ^ hv_b).bit_count())
            if norm_a is not None:
                arr_b = np.frombuffer(rb, np.uint8).astype(np.float64)
                if flip_b:
                    arr_b = np.ascontiguousarray(
                        arr_b.reshape(HASH_FRAME_SIZE, HASH_FRAME_SIZE)[:, ::-1]
                    ).reshape(-1)
                std_b = float(arr_b.std())
                if std_b >= 2.0:
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


def _aligned_audio_ranges(
    keeper: FileRecord,
    member: FileRecord,
    cancelled: Callable[[], bool] | None,
    *,
    frame_cache: _FrameBatchCache | None = None,
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
    for offset in offsets:
        try:
            evidence = _verify_video_offset(
                keeper,
                member,
                offset,
                cancelled,
                frame_cache=frame_cache,
                min_overlap=min(5.0, min(da, db)),
            )
        except InterruptedError:
            raise
        except Exception:
            evidence = None
        if evidence and isinstance(evidence.get("ranges"), dict):
            return evidence["ranges"]
    return fallback


def _audio_stream_present(path: str, cancelled: Callable[[], bool] | None) -> bool:
    ffprobe = _resolve_binary("ffprobe")
    if not ffprobe:
        raise ValueError("ffprobe unavailable")
    result = _run_video_command(
        [
            ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index",
            "-of", "json", path,
        ],
        timeout=15,
        cancelled=cancelled,
    )
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
    result = _run_video_command(
        [
            _ffmpeg_bin(), "-nostdin", "-v", "error", "-ss", f"{start:.4f}", "-threads", "1",
            "-i", path, "-t", f"{duration:.4f}", "-vn", "-sn", "-dn", "-ac", "1",
            "-ar", str(sample_rate), "-f", "s16le", "pipe:1",
        ],
        timeout=30,
        cancelled=cancelled,
    )
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
        da_s = 0.25 * da[:-2] + 0.5 * da[1:-1] + 0.25 * da[2:]
        db_s = 0.25 * db[:-2] + 0.5 * db[1:-1] + 0.25 * db[2:]
    else:
        da_s, db_s = da, db

    t_len = min(da_s.shape[0], db_s.shape[0])
    max_lag = min(int(AUDIO_MAX_LAG_SECONDS * AUDIO_SAMPLE_RATE / AUDIO_HOP_SAMPLES), max(1, t_len // 2))
    min_overlap_frames = max(10, t_len // 3)
    a_mat = da_s[:t_len]
    b_mat = db_s[:t_len]
    n_fft = 1 << (2 * t_len - 1).bit_length()
    cc = np.fft.irfft(
        np.fft.rfft(a_mat, n=n_fft, axis=0) * np.conj(np.fft.rfft(b_mat, n=n_fft, axis=0)),
        n=n_fft,
        axis=0,
    ).sum(axis=1)
    sa = np.concatenate(([0.0], np.cumsum(a_mat.sum(axis=1))))
    sa2 = np.concatenate(([0.0], np.cumsum((a_mat * a_mat).sum(axis=1))))
    sb = np.concatenate(([0.0], np.cumsum(b_mat.sum(axis=1))))
    sb2 = np.concatenate(([0.0], np.cumsum((b_mat * b_mat).sum(axis=1))))
    width = a_mat.shape[1]
    best = -1.0
    for lag in range(-max_lag, max_lag + 1):
        n_ov = t_len - abs(lag)
        if n_ov < min_overlap_frames:
            continue
        m = n_ov * width
        if lag >= 0:
            dot = float(cc[lag])
            sum_a = float(sa[t_len] - sa[lag])
            sum_a2 = float(sa2[t_len] - sa2[lag])
            sum_b = float(sb[t_len - lag])
            sum_b2 = float(sb2[t_len - lag])
        else:
            dot = float(cc[n_fft + lag])
            sum_a = float(sa[t_len + lag])
            sum_a2 = float(sa2[t_len + lag])
            sum_b = float(sb[t_len] - sb[-lag])
            sum_b2 = float(sb2[t_len] - sb2[-lag])
        num = dot - (sum_a * sum_b) / m
        var_a = max(0.0, sum_a2 - (sum_a * sum_a) / m)
        var_b = max(0.0, sum_b2 - (sum_b * sum_b) / m)
        denom = math.sqrt(var_a * var_b)
        if denom > 1e-9:
            best = max(best, num / denom)
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
        while True:
            if presence_lock is not None:
                with presence_lock:
                    if path in presence_cache:
                        return presence_cache[path]
            elif path in presence_cache:
                return presence_cache[path]
            # Singleflight concurrent ffprobes for the same path.
            with _AUDIO_PRESENCE_INFLIGHT_LOCK:
                wait_ev = _AUDIO_PRESENCE_INFLIGHT.get(path)
                if wait_ev is None:
                    my_ev = threading.Event()
                    _AUDIO_PRESENCE_INFLIGHT[path] = my_ev
                    owner = True
                else:
                    owner = False
            if not owner:
                while not wait_ev.wait(timeout=0.05):
                    if cancelled and cancelled():
                        raise InterruptedError("scan cancelled")
                continue
            try:
                present = _audio_stream_present(path, cancelled)
                if presence_lock is not None:
                    with presence_lock:
                        presence_cache[path] = present
                else:
                    presence_cache[path] = present
                return present
            finally:
                with _AUDIO_PRESENCE_INFLIGHT_LOCK:
                    _AUDIO_PRESENCE_INFLIGHT.pop(path, None)
                my_ev.set()

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


def _prescore_offset_in_memory(
    samples_a: Sequence[tuple[float, int]],
    samples_b: Sequence[tuple[float, int]],
    duration_a: float,
    duration_b: float,
    offset: float,
    bin_step: float,
) -> bool:
    """Fast in-memory check of a candidate offset against already-decoded dense samples.

    Rejects only clear false-positive offsets that have plenty of overlapping
    dense samples with almost no hash agreement, avoiding unnecessary ffmpeg calls.
    """
    if not samples_a or not samples_b or duration_a <= 0 or duration_b <= 0:
        return True
    start = max(0.0, -offset)
    end = min(duration_a, duration_b - offset)
    overlap = end - start
    if overlap < VERSION_MIN_SECONDS or overlap < min(duration_a, duration_b) * 0.7:
        return False
    tol = max(bin_step * 1.5, 1.5)
    b_times = [tb for tb, _hb in samples_b]
    overlapping = 0
    matched = 0
    for ta, ha in samples_a:
        if ta < start or ta > end:
            continue
        target_b = ta + offset
        pos = bisect_right(b_times, target_b - tol)
        lo = max(0, pos - 1)
        checked = False
        best_dist = 65
        while lo < len(samples_b):
            tb, hb = samples_b[lo]
            if tb > target_b + tol:
                break
            if abs(tb - target_b) <= tol:
                checked = True
                best_dist = min(best_dist, (ha ^ hb).bit_count())
            lo += 1
        if checked:
            overlapping += 1
            if best_dist <= 14:
                matched += 1
    return not (overlapping >= 8 and matched < 3 and (matched / overlapping) < 0.2)


_VERSION_CACHE_FLUSH_BATCH = 32
_SQLITE_IN_CHUNK = 500


def find_video_version_matches(
    records: list[FileRecord],
    *,
    distinct: DistinctReviews | None = None,
    progress: ProgressCb | None = None,
    workers: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    cache_path: str | Path | None = None,
    errors: list[str] | None = None,
    use_cache: bool = True,
    arrival_paths: set[str] | None = None,
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
    to_store: list[tuple[str, str, int | None, int | None, int | None, int | None]] = []
    completed_lock = threading.Lock()
    completed_queue: list[
        tuple[int, float | None, int | None, int | None, list[tuple[float, int]], str | None]
    ] = []
    siblings_by_rep: dict[int, list[int]] = defaultdict(list)
    stat_meta: list[tuple[str, str, int, int, int, int] | None] = [None] * len(videos)

    def _flush_to_store() -> None:
        nonlocal connection
        if connection is None or not to_store:
            return
        batch = list(to_store)
        to_store.clear()
        try:
            connection.executemany(
                "INSERT OR REPLACE INTO samples("
                "identity, samples, device, inode, size, mtime_ns"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                batch,
            )
            connection.commit()
        except sqlite3.Error as exc:
            cache_errors.append(str(exc))
            try:
                connection.close()
            except sqlite3.Error:
                pass
            connection = None

    def _drain_completed_into_store() -> None:
        with completed_lock:
            drained = list(completed_queue)
            completed_queue.clear()
        if not drained:
            return
        for rep_idx, dur, w, h, extracted, err in drained:
            if err is not None:
                continue
            if not (dur and math.isfinite(dur) and dur > 0):
                continue
            for idx in (rep_idx, *siblings_by_rep.get(rep_idx, ())):
                meta = stat_meta[idx]
                if meta is None:
                    continue
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

    try:
        if use_cache:
            try:
                Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(str(cache_path), timeout=30.0)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA temp_store=MEMORY")
                connection.execute("PRAGMA cache_size=-32000")
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
        uncached_indices: list[int] = []
        done_count = 0
        rekey_rows: list[tuple[str, str, int, int, int, int]] = []

        # Batch-hydrate cache rows instead of issuing 1-2 sequential SELECTs per video.
        valid_indices: list[int] = []
        for i, record in enumerate(videos):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            if connection is not None:
                try:
                    stat_meta[i] = _version_cache_stat(record)
                    valid_indices.append(i)
                except OSError as exc:
                    if errors is not None:
                        errors.append(f"video version analysis failed: {record.path}: {exc}")
                    done_count += 1
                    if progress:
                        progress(
                            "video-versions",
                            done_count,
                            len(videos),
                            f"Finding video excerpts ({done_count}/{len(videos)})",
                        )
            else:
                uncached_indices.append(i)

        if connection is not None and valid_indices:
            cached_by_key: dict[str, str] = {}
            try:
                keys_to_query = [stat_meta[i][0] for i in valid_indices]  # type: ignore[index]
                for chunk_start in range(0, len(keys_to_query), _SQLITE_IN_CHUNK):
                    if cancelled and cancelled():
                        raise InterruptedError("scan cancelled")
                    key_chunk = keys_to_query[chunk_start : chunk_start + _SQLITE_IN_CHUNK]
                    placeholders = ",".join("?" for _ in key_chunk)
                    for row_key, row_payload in connection.execute(
                        f"SELECT identity, samples FROM samples WHERE identity IN ({placeholders})",
                        key_chunk,
                    ).fetchall():
                        cached_by_key[row_key] = row_payload
            except sqlite3.Error as exc:
                cache_errors.append(str(exc))
                connection.close()
                connection = None
                cached_by_key.clear()

            # Batch device+inode fallback lookup for videos that missed by exact identity.
            missed_dev_ino: list[tuple[int, int]] = []
            for i in valid_indices:
                meta = stat_meta[i]
                assert meta is not None
                key = meta[0]
                if key not in cached_by_key and meta[3]:
                    missed_dev_ino.append((meta[2], meta[3]))

            fallback_by_dev_ino: dict[tuple[int, int, int, int], list[str]] = defaultdict(list)
            if connection is not None and missed_dev_ino:
                unique_inodes = list({ino for _dev, ino in missed_dev_ino})
                try:
                    for chunk_start in range(0, len(unique_inodes), _SQLITE_IN_CHUNK):
                        if cancelled and cancelled():
                            raise InterruptedError("scan cancelled")
                        ino_chunk = unique_inodes[chunk_start : chunk_start + _SQLITE_IN_CHUNK]
                        placeholders = ",".join("?" for _ in ino_chunk)
                        for dev_r, ino_r, sz_r, mt_r, payload_r in connection.execute(
                            "SELECT device, inode, size, mtime_ns, samples FROM samples "
                            f"WHERE inode IN ({placeholders}) ORDER BY rowid DESC",
                            ino_chunk,
                        ).fetchall():
                            fallback_by_dev_ino[(dev_r, ino_r, sz_r, mt_r)].append(payload_r)
                except sqlite3.Error as exc:
                    cache_errors.append(str(exc))
                    connection.close()
                    connection = None
                    fallback_by_dev_ino.clear()

            for i in valid_indices:
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                record = videos[i]
                meta = stat_meta[i]
                assert meta is not None
                key, resolved_path, dev, ino, size, mtime_ns = meta
                raw_payload = cached_by_key.get(key)
                parsed = _parse_cached_version_payload(raw_payload) if raw_payload else None
                if parsed is None and ino:
                    for fb_payload in fallback_by_dev_ino.get((dev, ino, size, mtime_ns), ()):
                        parsed = _parse_cached_version_payload(
                            fb_payload,
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
                            break
                if parsed is not None:
                    duration, cached_w, cached_h, cached_samples = parsed
                    record.duration = duration
                    if cached_w:
                        record.width = cached_w
                    if cached_h:
                        record.height = cached_h
                    samples[i] = cached_samples
                    done_count += 1
                    if progress:
                        progress(
                            "video-versions",
                            done_count,
                            len(videos),
                            f"Finding video excerpts ({done_count}/{len(videos)})",
                        )
                else:
                    uncached_indices.append(i)

        sha_by_idx: dict[int, str] = {
            i: r.sha256 for i, r in enumerate(videos) if r.sha256
        }

        to_store.extend(rekey_rows)

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
                        progress(
                            "video-versions",
                            done_count,
                            len(videos),
                            f"Finding video excerpts ({done_count}/{len(videos)})",
                        )
                elif key_sha is not None and key_sha in rep_by_sha:
                    siblings_by_rep[rep_by_sha[key_sha]].append(i)
                else:
                    if key_sha is not None:
                        rep_by_sha[key_sha] = i
                    representatives.append(i)

            if representatives:
                base_done = done_count
                remaining_total = len(videos) - base_done

                def _extract_and_record(idx: int):
                    res = _extract_version_samples_uncached(videos[idx], cancelled)
                    dur_r, w_r, h_r, ext_r, err_r = res
                    if connection is not None and err_r is None:
                        with completed_lock:
                            completed_queue.append((idx, dur_r, w_r, h_r, ext_r, err_r))
                    return res

                def sample_progress(done: int, rep_total: int) -> None:
                    # Runs on the calling thread: incrementally flush completed samples
                    # to SQLite so cancelling mid-stage preserves completed work.
                    if connection is not None:
                        _drain_completed_into_store()
                        if len(to_store) >= _VERSION_CACHE_FLUSH_BATCH:
                            _flush_to_store()
                    if progress and rep_total > 0:
                        reported = base_done + (done * remaining_total) // rep_total
                        progress(
                            "video-versions",
                            reported,
                            len(videos),
                            f"Finding video excerpts ({reported}/{len(videos)})",
                        )

                extracted_results = _run_on_video_lanes(
                    _extract_and_record,
                    representatives,
                    workers=workers,
                    progress=sample_progress,
                    cancelled=cancelled,
                )

                _drain_completed_into_store()
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

                if progress:
                    progress(
                        "video-versions",
                        len(videos),
                        len(videos),
                        f"Finding video excerpts ({len(videos)}/{len(videos)})",
                    )

        # Flush all remaining cache entries and close SQLite BEFORE candidate matching
        # and verification so the DB connection is never held open during verification.
        _drain_completed_into_store()
        _flush_to_store()
        if connection is not None:
            connection.close()
            connection = None

        effective_arrivals = arrival_paths
        if (
            effective_arrivals is None
            and distinct is not None
            and getattr(distinct, "filter_active", False)
        ):
            effective_arrivals = getattr(distinct, "arrivals", None)
        arrival_indices: set[int] | None = None
        if effective_arrivals is not None:
            arrival_indices = {
                idx for idx, rec in enumerate(videos) if rec.path in effective_arrivals
            }
            if not arrival_indices:
                return []

        def index_progress(done: int, total_keys: int) -> None:
            if progress and total_keys > 0:
                progress(
                    "video-versions-index",
                    done,
                    total_keys,
                    f"Indexing video excerpts ({done}/{total_keys})",
                )

        pairs = _version_candidate_pairs(
            samples,
            cancelled,
            durations=[r.duration for r in videos],
            arrival_indices=arrival_indices,
            progress=index_progress,
        )
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
            da = float(a.duration or 0)
            db = float(b.duration or 0)
            step = max(da, db) / VERSION_SAMPLE_LIMIT
            bin_step = max(0.25, step)
            offsets = [
                off
                for off in _offset_candidates(votes, bin_step)
                if _prescore_offset_in_memory(samples[i], samples[j], da, db, off, bin_step)
            ]
            if not offsets:
                return None
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
                            a,
                            b,
                            offset,
                            bin_step,
                            cancelled,
                            frame_cache=frame_cache,
                            skip_initial_verify=True,
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

        def verify_progress(done: int, total_items: int) -> None:
            if progress and total_items > 0:
                progress(
                    "video-versions-verify",
                    done,
                    total_items,
                    f"Checking video excerpts ({done}/{total_items})",
                )

        if progress:
            progress(
                "video-versions-verify",
                0,
                len(candidate_items),
                f"Checking video excerpts (0/{len(candidate_items)})",
            )

        verified = _run_on_video_lanes(
            _verify_candidate,
            candidate_items,
            workers=workers,
            progress=verify_progress,
            cancelled=cancelled,
        )
        return [match for match in verified if match is not None]
    finally:
        # Persist any completed dense samples even when cancelled mid-stage.
        if connection is not None:
            _drain_completed_into_store()
            _flush_to_store()
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if cache_errors and errors is not None:
            errors.append(f"video version cache unavailable; continued uncached: {cache_errors[0]}")
