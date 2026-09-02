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

BROWSER_SAFE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"}

THUMBNAIL_CACHE_VERSION = "dedupe-thumbs-v6"
DEFAULT_THUMBNAIL_BUDGET_BYTES = 512 * 1024 * 1024
PRUNE_MIN_INTERVAL_SECONDS = 120.0
PRUNE_EVERY_N_WRITES = 32

# Bound concurrent thumbnail generation: a page of video posters can otherwise
# spawn dozens of ffmpeg processes (and full-res HEIC decodes spike RAM).
GENERATE_WORKERS_CAP = 4
_generate_semaphore = threading.BoundedSemaphore(GENERATE_WORKERS_CAP)
# In-flight dedup: concurrent requests for the same uncached key share one
# generation instead of each decoding/encoding their own copy.
_inflight_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}

_prune_lock = threading.Lock()
_prune_state = {"writes": 0, "last_run": 0.0}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_browser_safe_image(path: Path) -> bool:
    """True when browsers can render the original file directly (no transcode)."""
    return path.suffix.lower() in BROWSER_SAFE_IMAGE_EXTENSIONS


def media_mimetype(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def _flatten_to_rgb(img):
    """Composite alpha onto white; a plain convert("RGB") turns transparent
    pixels black, which makes transparent PNG/GIF thumbs look wrong."""
    from PIL import Image

    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if not has_alpha:
        return img.convert("RGB")
    rgba = img.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


# Card thumbs render up to ~800 CSS px wide (~1600 physical px on Retina);
# the lightbox preview variant targets large displays at 2560 px, which is
# dramatically lighter than serving multi-MP originals; "full" keeps the
# original resolution for Quick Look-exact transcodes of formats browsers
# cannot render natively (HEIC, TIFF, …).
VARIANT_MAX_SIDE = {"thumb": 1600, "preview": 2560, "full": None}
VARIANT_QUALITY = {"thumb": 85, "preview": 88, "full": 95}


def image_thumbnail_bytes(path: Path, *, variant: str = "thumb") -> bytes:
    from PIL import Image, ImageOps

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass

    max_side = VARIANT_MAX_SIDE.get(variant, VARIANT_MAX_SIDE["thumb"])
    quality = VARIANT_QUALITY.get(variant, VARIANT_QUALITY["thumb"])
    with Image.open(path) as img:
        if max_side is not None:
            # Decode JPEGs at a reduced DCT scale instead of full resolution;
            # PIL ignores draft() for formats that do not support it.
            img.draft("RGB", (max_side, max_side))
        img = ImageOps.exif_transpose(img)
        img = _flatten_to_rgb(img)
        if max_side is not None:
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS, reducing_gap=3.0)
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
                str(path), "-frames:v", "1", "-vf", r"scale=min(1600\,iw):-1", "-q:v", "2", "-y",
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
        return image_thumbnail_bytes(path, variant=variant)
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


def _stored_thumbnail(destination: Path, key: str) -> tuple[Path, str] | None:
    if destination.is_file():
        try:
            os.utime(destination, None)
        except OSError:
            pass
        return destination, key
    return None


def cached_thumbnail(
    path: Path, *, variant: str = "thumb", cache_dir: Path | None = None
) -> tuple[Path, str] | None:
    """Return the cached thumbnail file and its key, generating it when missing.

    Generation is bounded (GENERATE_WORKERS_CAP) and deduplicated: concurrent
    requests for the same uncached thumbnail wait on the in-flight one rather
    than each spawning their own decode/ffmpeg job.
    """
    try:
        key = thumbnail_cache_key(path, variant=variant)
    except OSError:
        return None
    destination = thumbnail_cache_file(key, cache_dir=cache_dir)
    cached = _stored_thumbnail(destination, key)
    if cached is not None:
        return cached

    with _inflight_lock:
        event = _inflight.get(key)
        if event is None:
            event = threading.Event()
            _inflight[key] = event
            leader = True
        else:
            leader = False

    if not leader:
        # Another request is generating this thumbnail; use its result.
        event.wait(timeout=120)
        return _stored_thumbnail(destination, key)

    try:
        # Re-check after winning leadership: a just-finished generation may
        # have produced the file while we were registering.
        cached = _stored_thumbnail(destination, key)
        if cached is not None:
            return cached
        with _generate_semaphore:
            data = generate_thumbnail_bytes(path, variant=variant)
        if not data:
            return None
        try:
            store_thumbnail(destination, data)
        except OSError:
            return None
        maybe_prune_thumbnail_cache(cache_dir=cache_dir)
        return destination, key
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
            event.set()


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
