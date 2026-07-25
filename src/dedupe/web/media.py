"""Thumbnail generation and media type helpers for the web UI."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".m2ts",
    ".wmv", ".flv", ".3gp",
}

THUMBNAIL_CACHE_VERSION = "dedupe-thumbs-v1"
DEFAULT_THUMBNAIL_BUDGET_BYTES = 512 * 1024 * 1024
PRUNE_MIN_INTERVAL_SECONDS = 120.0
PRUNE_EVERY_N_WRITES = 32

_prune_lock = threading.Lock()
_prune_state = {"writes": 0, "last_run": 0.0}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def media_mimetype(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def image_thumbnail_bytes(path: Path, *, full: bool = False) -> bytes:
    from PIL import Image

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass

    max_edge = 1600 if full else 320
    quality = 88 if full else 80
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge))
        output = BytesIO()
        img.save(output, format="JPEG", quality=quality)
        return output.getvalue()


def video_thumbnail_bytes(path: Path) -> bytes | None:
    if not shutil.which("ffmpeg"):
        return None
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        output_path = Path(tmp.name)
    try:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i",
                str(path), "-frames:v", "1", "-vf", "scale=320:-1", "-y",
                str(output_path),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        data = output_path.read_bytes()
        return data if data else None
    except Exception:
        return None
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass


def default_thumbnail_cache_dir() -> Path:
    """Resolve the on-disk thumbnail cache, mirroring the hash cache location."""
    override = os.environ.get("DEDUPE_THUMBNAIL_CACHE_DIR")
    base = Path(override) if override else Path.home() / ".cache" / "dedupe" / "thumbnails"
    base.mkdir(parents=True, exist_ok=True)
    return base


def thumbnail_budget_bytes() -> int:
    raw = os.environ.get("DEDUPE_THUMBNAIL_CACHE_BUDGET")
    try:
        budget = int(raw) if raw else DEFAULT_THUMBNAIL_BUDGET_BYTES
    except ValueError:
        budget = DEFAULT_THUMBNAIL_BUDGET_BYTES
    return max(0, budget)


def thumbnail_cache_key(path: Path, *, variant: str) -> str:
    """Identity of a rendered thumbnail: source path, mtime, size and variant."""
    stat = path.stat()
    material = "\0".join(
        [
            THUMBNAIL_CACHE_VERSION,
            str(path.resolve()),
            str(stat.st_mtime_ns),
            str(stat.st_size),
            variant,
        ]
    )
    return hashlib.sha256(material.encode("utf-8", "surrogateescape")).hexdigest()


def thumbnail_cache_file(key: str, *, cache_dir: Path | None = None) -> Path:
    base = cache_dir or default_thumbnail_cache_dir()
    return base / key[:2] / f"{key[2:]}.jpg"


def generate_thumbnail_bytes(path: Path, *, variant: str) -> bytes | None:
    if is_video(path):
        return video_thumbnail_bytes(path)
    try:
        return image_thumbnail_bytes(path, full=variant == "full")
    except Exception:
        return None


def store_thumbnail(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=str(destination.parent), suffix=".tmp", delete=False
    ) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        staged = Path(tmp.name)
    try:
        os.replace(staged, destination)
    except OSError:
        staged.unlink(missing_ok=True)
        raise


def cached_thumbnail(
    path: Path, *, variant: str = "thumb", cache_dir: Path | None = None
) -> tuple[Path, str] | None:
    """Return the cached thumbnail file and its key, generating it when missing."""
    try:
        key = thumbnail_cache_key(path, variant=variant)
    except OSError:
        return None
    destination = thumbnail_cache_file(key, cache_dir=cache_dir)
    if destination.is_file():
        try:
            os.utime(destination, None)
        except OSError:
            pass
        return destination, key
    data = generate_thumbnail_bytes(path, variant=variant)
    if not data:
        return None
    try:
        store_thumbnail(destination, data)
    except OSError:
        return None
    maybe_prune_thumbnail_cache(cache_dir=cache_dir)
    return destination, key


def prune_thumbnail_cache(
    *, cache_dir: Path | None = None, budget: int | None = None
) -> int:
    """Drop least recently used thumbnails until the cache fits its budget."""
    base = cache_dir or default_thumbnail_cache_dir()
    limit = thumbnail_budget_bytes() if budget is None else max(0, budget)
    entries = []
    total = 0
    for candidate in base.rglob("*.jpg"):
        try:
            stat = candidate.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, candidate))
        total += stat.st_size
    if total <= limit:
        return 0
    removed = 0
    entries.sort()
    for _, size, candidate in entries:
        if total <= limit:
            break
        try:
            candidate.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
    return removed


def maybe_prune_thumbnail_cache(*, cache_dir: Path | None = None) -> None:
    """Prune occasionally so the hot path never pays for a full cache walk."""
    now = time.monotonic()
    with _prune_lock:
        _prune_state["writes"] += 1
        due = _prune_state["writes"] >= PRUNE_EVERY_N_WRITES
        cooled = now - _prune_state["last_run"] >= PRUNE_MIN_INTERVAL_SECONDS
        if not (due and cooled):
            return
        _prune_state["writes"] = 0
        _prune_state["last_run"] = now
    try:
        prune_thumbnail_cache(cache_dir=cache_dir)
    except OSError:
        pass
