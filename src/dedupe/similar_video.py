"""Near-duplicate video detection via ffmpeg frame sampling + perceptual hashing.

Resource-conscious: at most a few concurrent ffmpeg jobs, each limited to a
single decoder thread, with one-pass frame extraction and small frame size.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from .models import FileRecord, MediaType
from .parallel import DEFAULT_VIDEO_WORKERS_CAP, map_parallel, resolve_workers

ProgressCb = Callable[[str, int, int], None]

DEFAULT_THRESHOLD = 8  # Hamming on combined 64-bit fingerprint
MAX_FRAMES = 8  # enough signal; fewer seeks/decodes than 12
FRAME_WIDTH = 320


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


def _extract_frames(path: str | Path, out_dir: Path, max_frames: int = MAX_FRAMES) -> list[Path]:
    """
    One ffmpeg pass: evenly spaced low-res JPEGs.

    Uses a single decode with fps sampling instead of N independent seeks
    (each seek can re-decode from a keyframe).
    """
    duration, _, _ = probe_video(path)
    pattern = out_dir / "frame_%03d.jpg"
    n = max(3, max_frames)
    if duration and duration > 0:
        n = min(max_frames, max(3, min(max_frames, int(duration) + 1)))
        # Spread ~n frames across the whole video.
        fps = n / duration
        vf = f"fps={fps:.8f},scale={FRAME_WIDTH}:-1"
    else:
        n = max_frames
        vf = f"fps=1,scale={FRAME_WIDTH}:-1"

    cmd = [
        "ffmpeg",
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
    subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    return sorted(out_dir.glob("frame_*.jpg"))


def compute_video_fingerprint(path: str | Path) -> tuple[str | None, int | None, int | None, float | None]:
    """
    Return (fingerprint_hex, width, height, duration).
    Fingerprint is XOR of pHashes of sampled frames (64-bit hex).
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg/ffprobe not available")

    import imagehash
    from PIL import Image

    path = Path(path)
    duration, width, height = probe_video(path)

    with tempfile.TemporaryDirectory(prefix="dedupe-vid-") as tmp:
        frames = _extract_frames(path, Path(tmp))
        if not frames:
            return None, width, height, duration

        hashes = []
        for fp in frames:
            try:
                with Image.open(fp) as img:
                    hashes.append(imagehash.phash(img.convert("RGB")))
            except Exception:
                continue

        if not hashes:
            return None, width, height, duration

        # XOR all frame hashes → single 64-bit fingerprint
        combined = hashes[0]
        for h in hashes[1:]:
            combined = imagehash.ImageHash(combined.hash ^ h.hash)

        # Mix mid-frame for temporal midpoint sensitivity
        avg = hashes[len(hashes) // 2]
        combined = imagehash.ImageHash(combined.hash ^ avg.hash)

        return str(combined), width, height, duration


def _video_fingerprint_job(
    path: str,
) -> tuple[str, str | None, int | None, int | None, float | None, str | None]:
    """Worker: (path, fingerprint, width, height, duration, error)."""
    try:
        fp, w, h, dur = compute_video_fingerprint(path)
        return path, fp, w, h, dur, None
    except Exception as exc:
        return path, None, None, None, None, f"video fingerprint failed: {exc}"


def find_similar_video_groups(
    records: list[FileRecord],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    progress: ProgressCb | None = None,
    workers: int | None = None,
) -> list[list[FileRecord]]:
    """Cluster near-identical videos by fingerprint Hamming distance."""
    videos = [r for r in records if r.media_type == MediaType.VIDEO]
    if len(videos) < 2:
        return []

    if not ffmpeg_available():
        if progress:
            progress("video-hash", 0, 0)
        return []

    video_workers = resolve_workers(workers, cap=DEFAULT_VIDEO_WORKERS_CAP)

    total = len(videos)
    need = [r for r in videos if not r.video_fingerprint]
    cached = total - len(need)

    if need:
        by_path = {r.path: r for r in need}

        def hash_progress(done: int, _total: int) -> None:
            if progress:
                progress("video-hash", cached + done, total)

        results = map_parallel(
            _video_fingerprint_job,
            [r.path for r in need],
            workers=video_workers,
            # ffmpeg spawns real OS processes → threads buy true parallelism.
            backend="thread",
            progress=hash_progress,
            progress_every=1,
        )
        for path, fp, w, h, dur, err in results:
            rec = by_path[path]
            if err:
                rec.error = err
                continue
            rec.video_fingerprint = fp
            if w:
                rec.width = w
            if h:
                rec.height = h
            if dur is not None:
                rec.duration = dur

    if progress:
        progress("video-hash", total, total)

    hashed = [r for r in videos if r.video_fingerprint]
    if len(hashed) < 2:
        return []

    import imagehash

    parent = {r.path: r.path for r in hashed}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(hashed):
        ha = imagehash.hex_to_hash(a.video_fingerprint)  # type: ignore[arg-type]
        for b in hashed[i + 1 :]:
            hb = imagehash.hex_to_hash(b.video_fingerprint)  # type: ignore[arg-type]
            if (ha - hb) > threshold:
                continue
            # Near-identical: durations should be close when both known
            if a.duration and b.duration and a.duration > 0 and b.duration > 0:
                ratio = min(a.duration, b.duration) / max(a.duration, b.duration)
                if ratio < 0.9:
                    continue
            union(a.path, b.path)
        if progress and (i + 1) % 5 == 0:
            progress("video-cluster", i + 1, len(hashed))

    if progress:
        progress("video-cluster", len(hashed), len(hashed))

    clusters: dict[str, list[FileRecord]] = {}
    by_path = {r.path: r for r in hashed}
    for rec in hashed:
        root = find(rec.path)
        clusters.setdefault(root, []).append(by_path[rec.path])

    return [m for m in clusters.values() if len(m) >= 2]
