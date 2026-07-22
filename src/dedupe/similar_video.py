"""Near-duplicate video detection via ffmpeg frame sampling + perceptual hashing.

Resource-conscious: at most a few concurrent ffmpeg jobs, each limited to a
single decoder thread, with one-pass frame extraction and small frame size.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from pathlib import Path

from .grouping import cluster_around_best
from .models import FileRecord, MediaType
from .parallel import DEFAULT_VIDEO_WORKERS_CAP, map_parallel, resolve_workers

ProgressCb = Callable[[str, int, int, str | None], None]

DEFAULT_THRESHOLD = 8  # Hamming on combined 64-bit fingerprint
MAX_FRAMES = 8  # enough signal; fewer seeks/decodes than 12
FRAME_WIDTH = 320
HASH_FRAME_SIZE = 32


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


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
                "format=duration:stream=width,height",
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
        dur_raw = (data.get("format") or {}).get("duration")
        duration = float(dur_raw) if dur_raw is not None else None
        streams = data.get("streams") or []
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


def probe_dimensions(path: str | Path) -> tuple[int | None, int | None]:
    _, w, h = probe_video(path)
    return w, h


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
    """Fast-seek to one frame and return a 32×32 grayscale image buffer."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
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
        f"scale={HASH_FRAME_SIZE}:{HASH_FRAME_SIZE}:flags=lanczos,format=gray",
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


def _extract_seek_frame_ppm(
    path: str | Path, timestamp: float, *, frame_width: int
) -> bytes | None:
    """Fast-seek to one aspect-preserving frame and return it as PPM bytes."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
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
        hashes = []
        for i, timestamp in enumerate(timestamps):
            raw = _extract_hash_frame(path, timestamp)
            if raw is None:
                return None, width, height, duration
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
                continue
            else:
                if on_frame:
                    on_frame(idx + 1, len(frames))

        if not hashes:
            return None, width, height, duration

        return "v3:" + ",".join(str(frame_hash) for frame_hash in hashes), width, height, duration


def video_fingerprint_distances(a: str, b: str) -> list[int] | None:
    """Compare ordered frame hashes at normalized positions."""
    import imagehash

    def parse(value: str) -> list[str]:
        if value.startswith(("v2:", "v3:")):
            return [part for part in value[3:].split(",") if part]
        # Backward-compatible parser; cache versioning prevents normal reuse.
        return [value] if value else []

    left = parse(a)
    right = parse(b)
    if not left or not right:
        return None
    count = min(len(left), len(right))
    if count == 1:
        left_indexes = right_indexes = [0]
    else:
        left_indexes = [round(i * (len(left) - 1) / (count - 1)) for i in range(count)]
        right_indexes = [round(i * (len(right) - 1) / (count - 1)) for i in range(count)]
    return [
        int(
            imagehash.hex_to_hash(left[left_index])
            - imagehash.hex_to_hash(right[right_index])
        )
        for left_index, right_index in zip(left_indexes, right_indexes, strict=True)
    ]


def _fingerprint_hashes(value: str) -> tuple[int, ...]:
    raw = value[3:] if value.startswith(("v2:", "v3:")) else value
    return tuple(int(part, 16) for part in raw.split(",") if part)


def _video_fingerprint_job(
    path: str,
    *,
    on_frame: Callable[[int, int], None] | None = None,
) -> tuple[str, str | None, int | None, int | None, float | None, str | None]:
    """Worker: (path, fingerprint, width, height, duration, error)."""
    try:
        fp, w, h, dur = compute_video_fingerprint(path, on_frame=on_frame)
        return path, fp, w, h, dur, None
    except Exception as exc:
        return path, None, None, None, None, f"video fingerprint failed: {exc}"


def find_similar_video_groups(
    records: list[FileRecord],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    distinct_pairs: set[tuple[str, str]] | None = None,
    progress: ProgressCb | None = None,
    workers: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[list[FileRecord]]:
    """Cluster near-identical videos by fingerprint Hamming distance."""
    distinct_pairs = distinct_pairs or set()
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
        if not r.video_fingerprint or not r.video_fingerprint.startswith("v3:")
    ]
    cached = total - len(need)
    videos_done = cached
    progress_lock = threading.Lock()

    if need:
        by_path = {r.path: r for r in need}

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
        for j in indexes:
            b = hashed[j]
            if tuple(sorted((a.path, b.path))) in distinct_pairs:
                continue
            left = fingerprints[i]
            right = fingerprints[j]
            # These aligned positions are necessarily under the existing maximum.
            if (
                (left[0] ^ right[0]).bit_count() > max_frame_distance
                or (left[-1] ^ right[-1]).bit_count() > max_frame_distance
            ):
                continue
            count = min(len(left), len(right))
            if count == 1:
                left_indexes = right_indexes = [0]
            else:
                left_indexes = [
                    round(k * (len(left) - 1) / (count - 1))
                    for k in range(count)
                ]
                right_indexes = [
                    round(k * (len(right) - 1) / (count - 1))
                    for k in range(count)
                ]
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

    return cluster_around_best(hashed, adjacency, distinct_pairs)
