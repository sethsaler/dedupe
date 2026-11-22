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

# One extension table: the scanner's. A second copy here rotted (`.avif` was
# already listed as browser-safe below while the scanner never inventoried
# the format), so thumbnails follow whatever the scan considers video.
from ..models import VIDEO_EXTS as VIDEO_EXTENSIONS

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
_prune_state = {"writes": 0, "last_run": 0.0, "running": False}

_HEIF_LOCK = threading.Lock()
_HEIF_REGISTERED = False

VIDEO_FAIL_TTL_SECONDS = 30.0
VIDEO_FAIL_CACHE_MAX = 512
_video_fail_lock = threading.Lock()
_video_fail_cache: dict[tuple[str, int | None, int | None, str], float] = {}


def _ensure_heif_registered() -> None:
    """Register pillow-heif opener once per process, preferring the shared helper."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        from ..similar_image import ensure_heif_registered

        ensure_heif_registered()
        _HEIF_REGISTERED = True
        return
    except Exception:
        pass
    with _HEIF_LOCK:
        if _HEIF_REGISTERED:
            return
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except Exception:
            pass
        _HEIF_REGISTERED = True


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
        return img if img.mode == "RGB" else img.convert("RGB")
    rgba = img.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


# Grid cards render at small card sizes (≤512 px); standard card thumbs render
# up to ~800 CSS px wide (~1600 physical px on Retina); the lightbox preview
# variant targets large displays at 2560 px; "full" keeps the original
# resolution for Quick Look-exact transcodes of formats browsers cannot render
# natively (HEIC, TIFF, …).
VARIANT_MAX_SIDE = {"grid": 512, "thumb": 1600, "preview": 2560, "full": None}
VARIANT_QUALITY = {"grid": 82, "thumb": 85, "preview": 88, "full": 95}


def image_thumbnail_bytes(path: Path, *, variant: str = "thumb") -> bytes:
    from PIL import Image, ImageOps

    _ensure_heif_registered()

    max_side = VARIANT_MAX_SIDE.get(variant, VARIANT_MAX_SIDE["thumb"])
    quality = VARIANT_QUALITY.get(variant, VARIANT_QUALITY["thumb"])
    with Image.open(path) as img:
        icc_profile = img.info.get("icc_profile")
        if max_side is not None:
            # Decode JPEGs at a reduced DCT scale instead of full resolution;
            # PIL ignores draft() for formats that do not support it.
            img.draft("RGB", (max_side, max_side))
        img = ImageOps.exif_transpose(img)
        if max_side is not None:
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS, reducing_gap=3.0)
        # Composite resized pixels to avoid full-resolution RGB/RGBA copies.
        img = _flatten_to_rgb(img)
        output = BytesIO()
        save_kwargs: dict[str, object] = {"format": "JPEG", "quality": quality}
        if icc_profile:
            save_kwargs["icc_profile"] = icc_profile
        img.save(output, **save_kwargs)
        return output.getvalue()


def _video_fail_key(path: Path, variant: str) -> tuple[str, int | None, int | None, str]:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size, variant)
    except OSError:
        return (str(path), None, None, variant)


def _is_video_negatively_cached(key: tuple[str, int | None, int | None, str]) -> bool:
    now = time.monotonic()
    with _video_fail_lock:
        expires = _video_fail_cache.get(key)
        if expires is None:
            return False
        if now < expires:
            return True
        _video_fail_cache.pop(key, None)
        return False


def _record_video_failure(key: tuple[str, int | None, int | None, str]) -> None:
    now = time.monotonic()
    with _video_fail_lock:
        expired = [k for k, exp in _video_fail_cache.items() if exp <= now]
        for k in expired:
            _video_fail_cache.pop(k, None)
        _video_fail_cache.pop(key, None)
        while len(_video_fail_cache) >= VIDEO_FAIL_CACHE_MAX:
            _video_fail_cache.pop(next(iter(_video_fail_cache)))
        _video_fail_cache[key] = now + VIDEO_FAIL_TTL_SECONDS


def _clear_video_failure(key: tuple[str, int | None, int | None, str]) -> None:
    with _video_fail_lock:
        _video_fail_cache.pop(key, None)


def _run_ffmpeg_frame(
    path: Path, output_path: Path, *, seek: str, vf: str | None
) -> bytes:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "1",
        "-ss",
        seek,
        "-i",
        str(path),
        "-threads",
        "1",
        "-frames:v",
        "1",
        "-an",
        "-sn",
        "-dn",
    ]
    if vf:
        cmd.extend(["-vf", vf])
    cmd.extend(["-q:v", "2", "-y", str(output_path)])
    subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    return output_path.read_bytes()


def video_thumbnail_bytes(path: Path, *, variant: str = "thumb") -> bytes | None:
    if not shutil.which("ffmpeg"):
        return None
    fail_key = _video_fail_key(path, variant)
    if _is_video_negatively_cached(fail_key):
        return None

    max_side = VARIANT_MAX_SIDE.get(variant, VARIANT_MAX_SIDE["thumb"])
    vf = (
        f"scale=w='min({max_side},iw)':h='min({max_side},ih)'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
        if max_side is not None
        else None
    )
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        output_path = Path(tmp.name)
    try:
        data = _run_ffmpeg_frame(path, output_path, seek="1", vf=vf)
        if not data:
            # Clips shorter than 1s (e.g. Live Photos) produce 0 bytes at -ss 1.
            data = _run_ffmpeg_frame(path, output_path, seek="0", vf=vf)
        if data:
            _clear_video_failure(fail_key)
            return data
        _record_video_failure(fail_key)
        return None
    except Exception:
        _record_video_failure(fail_key)
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
        return video_thumbnail_bytes(path, variant=variant)
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
        staged = Path(tmp.name)
    try:
        os.replace(staged, destination)
    except OSError:
        staged.unlink(missing_ok=True)
        raise


def _stored_thumbnail(destination: Path, key: str) -> tuple[Path, str] | None:
    if destination.is_file():
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

    stored = False
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
        stored = True
        return destination, key
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
            event.set()
        if stored:
            maybe_prune_thumbnail_cache(cache_dir=cache_dir)


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


def _run_background_prune(cache_dir: Path | None) -> None:
    try:
        prune_thumbnail_cache(cache_dir=cache_dir)
    except OSError:
        pass
    finally:
        with _prune_lock:
            _prune_state["running"] = False


def maybe_prune_thumbnail_cache(*, cache_dir: Path | None = None) -> None:
    """Trigger an async background prune occasionally without blocking request threads."""
    now = time.monotonic()
    with _prune_lock:
        _prune_state["writes"] += 1
        due = _prune_state["writes"] >= PRUNE_EVERY_N_WRITES
        cooled = now - _prune_state["last_run"] >= PRUNE_MIN_INTERVAL_SECONDS
        if not (due and cooled) or _prune_state["running"]:
            return
        _prune_state["writes"] = 0
        _prune_state["last_run"] = now
        _prune_state["running"] = True
    thread = threading.Thread(
        target=_run_background_prune,
        args=(cache_dir,),
        name="dedupe-thumb-prune",
        daemon=True,
    )
    thread.start()
