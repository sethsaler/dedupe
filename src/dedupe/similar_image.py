"""Perceptual near-duplicate detection for images and GIFs.

Uses global pHash/dHash for candidate finding, then regional, color and
fine-detail checks to reject "same scene, different moment" false positives.
Overlapping detail windows retain small expression/pose changes even when
compression or exposure changes add differences across the whole frame.

Memory-conscious: images are drafted/thumbnail-scaled before hashing so a
12MP phone photo never becomes a full-res RGB buffer in the worker pool.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import threading
import time
from bisect import bisect_right
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from .cache import DistinctReviews
from .grouping import cluster_around_best
from .image_frames import is_animated_image
from .models import FileRecord, MediaType
from .parallel import DEFAULT_IMAGE_WORKERS_CAP, map_parallel, stage_workers

ProgressCb = Callable[[str, int, int], None]

# Near-identical default (strict). Hamming distance on 64-bit pHash.
DEFAULT_THRESHOLD = 6

# Error-kind prefix; engine diagnostics classify on it.
ERROR_IMAGE_HASH_FAILED = "image hash failed"
# dHash is secondary; slightly looser than pHash.
DHASH_THRESHOLD = 10
# Regional tiles: same-image/different-res pairs score ~0–2; pose changes ~12–22.
# Require every region (4 quads + center) within this max Hamming distance.
DEFAULT_TILE_MAX = 8
# And average tile distance under this (catches spread-out pose diffs).
DEFAULT_TILE_MEAN = 5.0
# Dense detail check: grayscale thumbnails compared block by block. A pair is
# rejected only when one block differs a lot AND the difference is
# concentrated there (max/mean ratio high). Global resampling (recompression,
# rescaling, sub-degree rotation, exposure shifts) spreads small differences
# everywhere, so it passes; a burst-like local change spikes one block.
# Calibrated on tests/fixtures/astronaut.png: true re-exports score max ≤ 1.5
# at ratio ≤ 2, burst-like 60px shifts score max ≥ 4.9 at ratio ≥ 20.
DENSE_THUMB_SIZE = 128
DENSE_GRID = 8
DEFAULT_DENSE_LOCAL_MAX = 3.0
DEFAULT_DENSE_CONCENTRATION = 8.0
# Grayscale hashes ignore hue and largely ignore uniform brightness. Bound
# each color/alpha channel's mean absolute difference (0–255) as well: a loose
# 32-level allowance retains re-exports/moderate exposure edits, not different
# colors or substantially different transparency.
DEFAULT_DENSE_COLOR_MEAN_MAX = 32.0
# Channel means within this many levels of their average: a grayscale image.
MONOCHROME_BALANCE_MAX = 3.0
# A cached animation contains eight RGB thumbnails, up to 64 KB each in
# Pillow's storage; keep the worst-case thumbnail cache near 64 MB.
DENSE_CACHE_SIZE = 128
# Fine verification is lazy: only hash candidates decode these frames. The
# bounded grayscale cache stays light in RAM while avoiding cross-thread thrash.
DETAIL_MAX_SIDE = 768
DETAIL_CACHE_SIZE = 32

# pHash/dHash only need ~32×32 DCT input; anything larger is wasted decode/RAM.
# 512 keeps edge structure for rescaled/quality variants without loading full HEIC/JPEG.
HASH_MAX_SIDE = 512
# Normalize to this size before tiling so different resolutions compare fairly.
TILE_NORMALIZE = 256
# Only records without stored tile hashes reach the path cache; keep it large
# enough that a big scan never re-decodes the same file twice.
TILE_CACHE_SIZE = 65536
# Animation tiles retain ordered, time-aligned samples. Older first-frame-only
# tiles cannot verify animations and must be recomputed. t4: JPEGs are decoded
# at reduced size, which moves every hash of a file by a bit or two, so hashes
# from the full-size decode are computed again rather than compared with new ones.
TILE_HASH_VERSION = "t4"
# Formats whose decoder can produce a smaller image directly (JPEG's scaled
# inverse transform). The reduced image keeps both sides at or above
# HASH_MAX_SIDE, so the downscale that follows still only shrinks.
REDUCED_DECODE_FORMATS = frozenset({"JPEG", "MPO"})
ANIMATION_HASH_FRAMES = 8
MIN_ASPECT_RATIO_SIMILARITY = 0.95
# Rotated and mirrored copies (pixels turned by an app that ignored or dropped
# the EXIF orientation tag, or a mirrored front-camera export) share no global
# pHash with their source. Still images also store the pHash of each of the
# seven non-identity orientations so candidate lookup can find them; the pair
# is then verified with tiles and dense detail in the matched orientation, so
# the bar for "same photo" is unchanged. Order is fixed: the stored hashes are
# positional. Values are PIL Image.Transpose member names.
ORIENTATIONS = (
    "ROTATE_90",
    "ROTATE_180",
    "ROTATE_270",
    "FLIP_LEFT_RIGHT",
    "FLIP_TOP_BOTTOM",
    "TRANSPOSE",
    "TRANSVERSE",
)
# Orientations that swap width and height.
QUARTER_TURN_ORIENTATIONS = frozenset({"ROTATE_90", "ROTATE_270", "TRANSPOSE", "TRANSVERSE"})
ORIENTATION_HASH_VERSION = "o1"
#: Stored in ``orientation_phashes`` on animations so cached scans do not
#: continuously backfill still-image turns (optionally followed by ``,<mid_phash>``).
ANIMATION_ORIENTATION_SENTINEL = f"{ORIENTATION_HASH_VERSION}:anim"
# EXIF Orientation tag and the values whose transpose swaps width and height.
EXIF_ORIENTATION = 0x0112
QUARTER_TURN_EXIF_ORIENTATIONS = frozenset({5, 6, 7, 8})
# Formats whose header orientation is verified to equal exif_transpose's
# (tests/test_similar_image.py); others take the decoding path.
HEADER_ORIENTATION_FORMATS = frozenset({"JPEG", "MPO", "PNG", "WEBP", "HEIF", "GIF"})
# Verification decodes a file once per scan at DETAIL_MAX_SIDE and derives
# every orientation, comparison size, tile, and dense thumbnail from it.
SAMPLE_CACHE_SIZE = 16
# A process pool sidesteps GIL contention in the pHash/dHash/tile math (small
# numpy/scipy ops hold the GIL even though Pillow's decoder releases it), but
# costs an interpreter spawn plus Pillow/imagehash imports per worker. Only
# batches at least this large amortize that startup; frozen macOS app bundles
# pay a higher spawn cost so they keep threads for larger batches.
PROCESS_POOL_MIN_IMAGES = 2048 if getattr(sys, "frozen", False) else 512
# Max candidate pairs packed into a single worker task so skewed hash buckets
# do not stall one straggler thread at 99%.
MAX_VERIFY_RUN_SIZE = 32
VERIFY_PROGRESS_MIN_INTERVAL = 0.1
JPEG_BITSTREAM_CHUNK_SIZE = 64 * 1024
CAPTURE_METADATA_CACHE_MAX = 65536

_HEIF_LOCK = threading.Lock()
_HEIF_REGISTERED = False
_LAST_DENSE_FAILURE = threading.local()
_CAPTURE_METADATA_LOCK = threading.Lock()
_CAPTURE_METADATA_CACHE: dict[tuple[str, int, int, int], bool] = {}


def _ensure_image_deps() -> None:
    try:
        import imagehash  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "Image similar detection requires Pillow and ImageHash. "
            "Install with: pip install Pillow ImageHash pillow-heif"
        ) from exc


def ensure_heif_registered() -> None:
    """Register pillow-heif opener once, safely across worker threads."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    with _HEIF_LOCK:
        if _HEIF_REGISTERED:
            return
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except Exception:
            pass
        _HEIF_REGISTERED = True


_register_heif = ensure_heif_registered


def _has_alpha_channel(img) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in getattr(img, "info", {})
    )


def _convert_icc_to_srgb(frame, icc_bytes: bytes | None):
    if not icc_bytes:
        return frame
    try:
        from PIL import ImageCms

        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
        srgb_profile = ImageCms.createProfile("sRGB")
        mode = "RGBA" if frame.mode == "RGBA" else "RGB"
        converted = frame if frame.mode == mode else frame.convert(mode)
        return ImageCms.profileToProfile(converted, src_profile, srgb_profile, outputMode=mode)
    except Exception:
        return frame


def _composite_neutral_matte(frame):
    """Composite alpha onto neutral mid-gray (128, 128, 128) before hashing."""
    from PIL import Image as PILImage

    if frame.mode != "RGBA":
        return frame.convert("RGB")
    canvas = PILImage.new("RGB", frame.size, (128, 128, 128))
    canvas.paste(frame, mask=frame.getchannel("A"))
    return canvas


def _downscale_for_hash(img, max_side: int = HASH_MAX_SIDE, *, preserve_alpha: bool = False):
    """Return ICC-normalized RGB(A) scaled so longest side <= max_side."""
    from PIL import Image as PILImage

    icc = getattr(img, "info", {}).get("icc_profile")
    has_alpha = _has_alpha_channel(img)
    frame = img.convert("RGBA" if (preserve_alpha or has_alpha) else "RGB")
    if max(frame.size) > max_side:
        # Bilinear is enough pre-pHash and much cheaper than LANCZOS on 12MP shots.
        frame.thumbnail((max_side, max_side), PILImage.Resampling.BILINEAR)
    frame = _convert_icc_to_srgb(frame, icc)
    if preserve_alpha:
        return frame if frame.mode == "RGBA" else frame.convert("RGBA")
    return _composite_neutral_matte(frame) if has_alpha or frame.mode == "RGBA" else frame.convert("RGB")


def probe_image_dimensions(path: str | Path) -> tuple[int | None, int | None]:
    """Read display-oriented image/GIF dimensions without computing hashes."""
    _ensure_image_deps()
    ensure_heif_registered()

    from PIL import Image, ImageOps

    with Image.open(path) as img:
        if is_animated_image(img):
            width, height = img.size
        elif img.format in HEADER_ORIENTATION_FORMATS:
            # The header and EXIF orientation give the size exif_transpose
            # would produce, without its full-resolution decode. (Pillow's
            # getexif() loads by itself where EXIF follows the pixels: PNG.)
            width, height = img.size
            if img.getexif().get(EXIF_ORIENTATION, 1) in QUARTER_TURN_EXIF_ORIENTATIONS:
                width, height = height, width
        else:
            # Elsewhere the orientation can depend on decoding (TIFF reads it
            # from tags at load), so match the hashing pass exactly.
            width, height = ImageOps.exif_transpose(img).size
        return int(width), int(height)


def _upright_hash_source(img, max_side: int = HASH_MAX_SIDE):
    """``(upright image, display width, display height, animated)`` for hashing.

    The size is the file's own, read before any reduced decode. JPEGs are
    asked for a reduced decode first: turning the image loads it, and a
    request made after that has no effect.
    """
    from PIL import ImageOps

    animated = is_animated_image(img)
    width, height = img.size
    if animated:
        return img, width, height, True
    if img.format in REDUCED_DECODE_FORMATS:
        quarter_turn = img.getexif().get(EXIF_ORIENTATION, 1) in QUARTER_TURN_EXIF_ORIENTATIONS
        try:
            img.draft("RGB", (max_side, max_side))
        except Exception:
            pass
        img = ImageOps.exif_transpose(img)
        if quarter_turn:
            width, height = height, width
    else:
        img = ImageOps.exif_transpose(img)
        width, height = img.size
    return img, width, height, False


def compute_image_hashes(path: str | Path) -> tuple[str | None, str | None, int | None, int | None]:
    """Return (phash_hex, dhash_hex, width, height).

    Width/height are the *original* dimensions. Hashing works on a downscaled
    copy so large photos stay cheap in RAM/CPU.
    """
    phash, dhash, width, height, _tiles = compute_image_hashes_with_tiles(
        path, with_tiles=False
    )
    return phash, dhash, width, height


def compute_image_hashes_with_tiles(
    path: str | Path, *, with_tiles: bool = True
) -> tuple[str | None, str | None, int | None, int | None, tuple[str, ...] | None]:
    """Return (phash_hex, dhash_hex, width, height, tile_phashes).

    Regional tile hashes are derived from the frame decoded for pHash/dHash so
    verification never has to open the file a second time.
    """
    phash, dhash, width, height, tiles, _orientations = compute_image_fingerprint(
        path, with_tiles=with_tiles, with_orientations=False
    )
    return phash, dhash, width, height, tiles


def _extract_capture_metadata(img) -> bool:
    """Return True when ``img`` carries EXIF DateTimeOriginal + camera Make/Model."""
    try:
        exif = img.getexif()
        capture = exif.get(36867)
        if not capture and exif.get(34665):
            capture = exif.get_ifd(34665).get(36867)
        return bool(capture and (exif.get(271) or exif.get(272)))
    except Exception:
        return False


def _store_capture_metadata(path: Path, has_meta: bool) -> None:
    key = _stat_key(str(path))
    if key is None:
        return
    with _CAPTURE_METADATA_LOCK:
        if len(_CAPTURE_METADATA_CACHE) >= CAPTURE_METADATA_CACHE_MAX:
            _CAPTURE_METADATA_CACHE.clear()
        _CAPTURE_METADATA_CACHE[key] = has_meta


def get_cached_capture_metadata(record: FileRecord) -> bool | None:
    """Look up EXIF capture-metadata flag captured during image hashing, if valid."""
    if (
        record.mtime_ns is not None
        and record.ctime_ns is not None
    ):
        key = (record.path, int(record.size), int(record.mtime_ns), int(record.ctime_ns))
    else:
        key = _stat_key(record.path)
        if key is None:
            return None
    with _CAPTURE_METADATA_LOCK:
        return _CAPTURE_METADATA_CACHE.get(key)


def _compute_image_fingerprint_full(
    path: str | Path, *, with_tiles: bool = True, with_orientations: bool = True
) -> tuple[
    str | None,
    str | None,
    int | None,
    int | None,
    tuple[str, ...] | None,
    tuple[str, ...] | None,
    bool,
    str | None,
]:
    _ensure_image_deps()
    ensure_heif_registered()

    import imagehash
    from PIL import Image

    path = Path(path)
    with Image.open(path) as img:
        has_capture_meta = False if is_animated_image(img) else _extract_capture_metadata(img)
        _store_capture_metadata(path, has_capture_meta)
        img, width, height, animated = _upright_hash_source(img)

        frames = _hash_frames(img)
        # Candidate lookup uses the first frame. Verification below preserves
        # frame order; XOR used to erase order and cancel repeated frame hashes.
        phash = str(imagehash.phash(frames[0]))
        dhash = str(imagehash.dhash(frames[0]))
        mid_phash = (
            str(imagehash.phash(frames[len(frames) // 2]))
            if animated and len(frames) > 1
            else None
        )

        tiles: tuple[str, ...] | None = None
        if with_tiles:
            try:
                tiles = tuple(str(t) for frame in frames for t in _tile_phashes_from_image(frame))
            except Exception:
                tiles = None

        orientations: tuple[str, ...] | None = None
        if with_orientations and not animated:
            orientations = _orientation_phashes(frames[0])

        return phash, dhash, width, height, tiles, orientations, animated, mid_phash


def compute_image_fingerprint(
    path: str | Path, *, with_tiles: bool = True, with_orientations: bool = True
) -> tuple[
    str | None, str | None, int | None, int | None, tuple[str, ...] | None, tuple[str, ...] | None
]:
    """Return (phash, dhash, width, height, tile_phashes, orientation_phashes).

    ``orientation_phashes`` holds the pHash of each ``ORIENTATIONS`` entry for
    still images, and is None for animations.
    """
    ph, dh, w, h, tiles, orientations, _animated, _mid = _compute_image_fingerprint_full(
        path, with_tiles=with_tiles, with_orientations=with_orientations
    )
    return ph, dh, w, h, tiles, orientations


def is_animation_orientation_sentinel(stored: str | None) -> bool:
    """Whether ``orientation_phashes`` marks a known animated image."""
    if not stored:
        return False
    return stored == ANIMATION_ORIENTATION_SENTINEL or stored.startswith(
        f"{ANIMATION_ORIENTATION_SENTINEL},"
    )


def decode_animation_midpoint_phash(stored: str | None) -> int | None:
    """Return stored midpoint pHash for an animated image if present."""
    prefix = f"{ANIMATION_ORIENTATION_SENTINEL},"
    if not stored or not stored.startswith(prefix):
        return None
    hex_hash = stored[len(prefix) :]
    try:
        return int(hex_hash, 16) if hex_hash else None
    except ValueError:
        return None


def _orientation_phashes(frame) -> tuple[str, ...]:
    """pHash of each non-identity orientation of a hashing frame.

    pHash reduces its input to a 32×32 grayscale grid before the DCT. Turning
    that grid is exact, so one resize serves all seven orientations.
    """
    import imagehash
    from PIL import Image as PILImage

    side = 32  # imagehash.phash: hash_size 8 × highfreq_factor 4
    grid = frame.convert("L").resize((side, side), PILImage.Resampling.LANCZOS)
    return tuple(
        str(imagehash.phash(grid.transpose(PILImage.Transpose[name])))
        for name in ORIENTATIONS
    )


def encode_orientation_phashes(values: tuple[str, ...]) -> str:
    return f"{ORIENTATION_HASH_VERSION}:" + ",".join(values)


def decode_orientation_phashes(stored: str | None) -> tuple[str, ...] | None:
    """Parse stored orientation hashes; anything else must be recomputed."""
    if not stored or not stored.startswith(f"{ORIENTATION_HASH_VERSION}:"):
        return None
    if is_animation_orientation_sentinel(stored):
        return None
    values = tuple(stored[len(ORIENTATION_HASH_VERSION) + 1 :].split(","))
    return values if len(values) == len(ORIENTATIONS) and all(values) else None


def _orient(img, orientation: str | None):
    if orientation is None:
        return img
    from PIL import Image as PILImage

    return img.transpose(PILImage.Transpose[orientation])


def _hash_frames(img, *, preserve_alpha: bool = False, max_side: int = HASH_MAX_SIDE) -> list:
    """Sample animations in playback order and time, independent of frame rate."""
    if not is_animated_image(img):
        return [_downscale_for_hash(img, max_side, preserve_alpha=preserve_alpha)]
    starts = []
    duration = 0.0
    for index in range(img.n_frames):
        img.seek(index)
        starts.append(duration)
        duration += max(10.0, float(img.info.get("duration", 100) or 100))
    frames = []
    for sample in range(ANIMATION_HASH_FRAMES):
        timestamp = sample * (duration - 0.001) / (ANIMATION_HASH_FRAMES - 1)
        img.seek(max(0, bisect_right(starts, timestamp) - 1))
        frames.append(_downscale_for_hash(img, max_side, preserve_alpha=preserve_alpha))
    return frames


def _image_hash_job(
    path: str,
) -> tuple[
    str,
    str | None,
    str | None,
    int | None,
    int | None,
    tuple[str, ...] | None,
    tuple[str, ...] | str | None,
    str | None,
]:
    """Worker: (path, phash, dhash, width, height, tile_phashes, orientation_phashes, error)."""
    try:
        ph, dh, w, h, tiles, orientations, animated, mid_phash = _compute_image_fingerprint_full(
            path
        )
        encoded_orientations: tuple[str, ...] | str | None = orientations
        if animated:
            encoded_orientations = (
                f"{ANIMATION_ORIENTATION_SENTINEL},{mid_phash}"
                if mid_phash
                else ANIMATION_ORIENTATION_SENTINEL
            )
        return path, ph, dh, w, h, tiles, encoded_orientations, None
    except Exception as exc:
        return path, None, None, None, None, None, None, f"{ERROR_IMAGE_HASH_FAILED}: {exc}"


def _tile_phashes_from_image(img, comparison_side: int = TILE_NORMALIZE) -> list:
    """pHash of 4 quadrants + center crop after size normalization."""
    import imagehash
    from PIL import Image as PILImage

    # Letterbox into a fixed square so aspect ratio is preserved and scales match.
    canvas = PILImage.new("RGB", (TILE_NORMALIZE, TILE_NORMALIZE), (0, 0, 0))
    src = img.convert("RGB")
    src.thumbnail((comparison_side, comparison_side), PILImage.Resampling.BILINEAR)
    ox = (TILE_NORMALIZE - src.width) // 2
    oy = (TILE_NORMALIZE - src.height) // 2
    canvas.paste(src, (ox, oy))

    w = h = TILE_NORMALIZE
    regions = [
        (0, 0, w // 2, h // 2),
        (w // 2, 0, w, h // 2),
        (0, h // 2, w // 2, h),
        (w // 2, h // 2, w, h),
        (w // 4, h // 4, w - w // 4, h - h // 4),  # center
    ]
    return [imagehash.phash(canvas.crop(box)) for box in regions]


def _stat_key(path: str) -> tuple[str, int, int, int] | None:
    try:
        stat = Path(path).stat()
        return path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


def _parse_border_crop(
    value: str | None,
    expected_dims: tuple[int | None, int | None] | None = None,
) -> tuple[int, int, int, int, int, int] | None:
    """Parse ``left,top,right,bottom,width,height[,chrome]`` when valid."""
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
    if not (
        0 <= left < right <= width
        and 0 <= top < bottom <= height
        and (left, top, right, bottom) != (0, 0, width, height)
    ):
        return None
    if expected_dims and all(expected_dims) and (width, height) != expected_dims:
        return None
    return left, top, right, bottom, width, height


def _crop_frame(frame, crop: tuple[int, int, int, int] | None, orig_w: int, orig_h: int):
    if crop is None or not orig_w or not orig_h:
        return frame
    left, top, right, bottom = crop
    fw, fh = frame.size
    sx, sy = fw / orig_w, fh / orig_h
    box = (
        max(0, min(fw - 1, round(left * sx))),
        max(0, min(fh - 1, round(top * sy))),
        max(1, min(fw, round(right * sx))),
        max(1, min(fh, round(bottom * sy))),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return frame
    return frame.crop(box)


@lru_cache(maxsize=DETAIL_CACHE_SIZE)
def _verification_frames_cached(path: str, size: int, mtime_ns: int, ctime_ns: int):
    """Unified stat-keyed 768px decode cache: ``(rgb_frames, rgba_frames, orig_w, orig_h)``.

    One decode per file serves tile pHashes, 128px dense/color checks, and
    768px fine-detail verification, and invalidates immediately on any file
    replacement (including preserved-size/mtime edits via ``ctime_ns``).
    """
    _ensure_image_deps()
    ensure_heif_registered()
    from PIL import Image

    try:
        with Image.open(path) as img:
            img, width, height, _animated = _upright_hash_source(img, max_side=DETAIL_MAX_SIDE)
            has_alpha = _has_alpha_channel(img)
            try:
                rgb = tuple(_hash_frames(img, max_side=DETAIL_MAX_SIDE))
            except Exception:
                rgb = None
            try:
                if not has_alpha and rgb is not None:
                    rgba = tuple(frame.convert("RGBA") for frame in rgb)
                else:
                    rgba = tuple(_hash_frames(img, preserve_alpha=True, max_side=DETAIL_MAX_SIDE))
            except Exception:
                rgba = None
            return rgb, rgba, width, height
    except Exception:
        return None, None, None, None


@lru_cache(maxsize=TILE_CACHE_SIZE)
def _tile_phashes_cached(
    path: str,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    comparison_side: int = TILE_NORMALIZE,
    orientation: str | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[str, ...] | None:
    frames, _rgba, orig_w, orig_h = _verification_frames_cached(path, size, mtime_ns, ctime_ns)
    if frames is None:
        return None
    try:
        tiles = [
            tile
            for frame in frames
            for tile in _tile_phashes_from_image(
                _orient(_crop_frame(frame, crop, orig_w, orig_h), orientation),
                comparison_side,
            )
        ]
        return tuple(str(t) for t in tiles)
    except Exception:
        return None


def _tile_phashes_for_path(
    path: str,
    comparison_side: int = TILE_NORMALIZE,
    orientation: str | None = None,
    *,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[str, ...] | None:
    """Stat-keyed tile hashes for records without stored tiles or with orientations/crops."""
    key = _stat_key(path)
    if key is None:
        return None
    return _tile_phashes_cached(*key, comparison_side, orientation, crop)


def tile_distances(path_a: str, path_b: str) -> list[int] | None:
    """Per-tile Hamming distances between two images. None if either fails to load."""
    import imagehash

    ta = _tile_phashes_for_path(path_a)
    tb = _tile_phashes_for_path(path_b)
    if not ta or not tb or len(ta) != len(tb):
        return None
    return [
        int(imagehash.hex_to_hash(ta[i]) - imagehash.hex_to_hash(tb[i]))
        for i in range(len(ta))
    ]


def _tiles_agree(
    tiles_a: tuple[str, ...],
    tiles_b: tuple[str, ...],
    tile_max: int,
    tile_mean: float,
) -> bool:
    dists = [(int(a, 16) ^ int(b, 16)).bit_count() for a, b in zip(tiles_a, tiles_b, strict=True)]
    if max(dists) <= tile_max and (sum(dists) / len(dists)) <= tile_mean:
        return True
    # Symmetric +-1-frame temporal tolerance for animations (e.g. FPS-decimated GIFs).
    n_frames = len(tiles_a) // 5
    if n_frames <= 1 or len(tiles_a) % 5:
        return False

    def frame_ok(fa: tuple[str, ...], fb: tuple[str, ...]) -> bool:
        fd = [(int(a, 16) ^ int(b, 16)).bit_count() for a, b in zip(fa, fb, strict=True)]
        return max(fd) <= tile_max and (sum(fd) / 5.0) <= tile_mean

    frames_a = [tiles_a[i * 5 : (i + 1) * 5] for i in range(n_frames)]
    frames_b = [tiles_b[i * 5 : (i + 1) * 5] for i in range(n_frames)]
    for k in range(n_frames):
        lo, hi = max(0, k - 1), min(n_frames, k + 2)
        if not any(frame_ok(frames_a[k], frames_b[j]) for j in range(lo, hi)):
            return False
        if not any(frame_ok(frames_b[k], frames_a[j]) for j in range(lo, hi)):
            return False
    return True


def is_near_identical(
    path_a: str,
    path_b: str,
    *,
    tile_max: int = DEFAULT_TILE_MAX,
    tile_mean: float = DEFAULT_TILE_MEAN,
    tiles_a: tuple[str, ...] | None = None,
    tiles_b: tuple[str, ...] | None = None,
    dimensions_a: tuple[int | None, int | None] | None = None,
    dimensions_b: tuple[int | None, int | None] | None = None,
    orientation_a: str | None = None,
    crop_a: tuple[int, int, int, int] | None = None,
    crop_b: tuple[int, int, int, int] | None = None,
) -> bool:
    """
    True if regional structure matches (same image / scale / quality variants).
    False for same-person different-pose shots that can still pass global pHash.

    ``orientation_a`` compares path_a turned that way; stored ``tiles_a`` are
    for the file as decoded, so they are ignored for a turned or cropped comparison.
    """
    if orientation_a is not None or crop_a is not None:
        tiles_a = _tile_phashes_for_path(path_a, TILE_NORMALIZE, orientation_a, crop=crop_a)
    if crop_b is not None:
        tiles_b = _tile_phashes_for_path(path_b, TILE_NORMALIZE, None, crop=crop_b)
    if tiles_a is None:
        tiles_a = _tile_phashes_for_path(path_a)
    if tiles_b is None:
        tiles_b = _tile_phashes_for_path(path_b)
    if not tiles_a or not tiles_b or len(tiles_a) != len(tiles_b):
        # Missing evidence or a still/animation mismatch cannot establish similarity.
        return False
    try:
        dimensions_a = dimensions_a or probe_image_dimensions(path_a)
        dimensions_b = dimensions_b or probe_image_dimensions(path_b)
    except (OSError, ValueError):
        return False
    if crop_a is not None:
        dimensions_a = (crop_a[2] - crop_a[0], crop_a[3] - crop_a[1])
    if crop_b is not None:
        dimensions_b = (crop_b[2] - crop_b[0], crop_b[3] - crop_b[1])
    if all(dimensions_a) and all(dimensions_b):
        side_a = min(TILE_NORMALIZE, max(dimensions_a))
        side_b = min(TILE_NORMALIZE, max(dimensions_b))
        if side_a != side_b:
            # thumbnail never enlarges small downloads. Compare at the smaller
            # copy's scale so both hashes see the same content and padding.
            # Keep pair-specific hashes out of the persistent per-file cache.
            side = min(side_a, side_b)
            if side_a > side:
                tiles_a = _tile_phashes_for_path(path_a, side, orientation_a, crop=crop_a)
            if side_b > side:
                tiles_b = _tile_phashes_for_path(path_b, side, crop=crop_b)
            if not tiles_a or not tiles_b or len(tiles_a) != len(tiles_b):
                return False
    return _tiles_agree(tiles_a, tiles_b, tile_max, tile_mean)


@lru_cache(maxsize=DENSE_CACHE_SIZE)
def _dense_thumbs_cached(
    path: str,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    orientation: str | None = None,
    crop: tuple[int, int, int, int] | None = None,
):
    from PIL import Image

    if orientation is not None:
        upright = _dense_thumbs_cached(path, size, mtime_ns, ctime_ns, None, crop)
        return None if upright is None else tuple(_orient(frame, orientation) for frame in upright)
    _rgb, frames, orig_w, orig_h = _verification_frames_cached(path, size, mtime_ns, ctime_ns)
    if frames is None:
        return None
    try:
        return tuple(
            _crop_frame(frame, crop, orig_w, orig_h).resize(
                (DENSE_THUMB_SIZE, DENSE_THUMB_SIZE), Image.Resampling.BILINEAR
            )
            for frame in frames
        )
    except Exception:
        return None


def _dense_thumbs_for_path(
    path: str,
    orientation: str | None = None,
    *,
    crop: tuple[int, int, int, int] | None = None,
):
    """Stat-keyed RGBA thumbnails for color and dense detail verification."""
    key = _stat_key(path)
    if key is None:
        return None
    return _dense_thumbs_cached(*key, orientation, crop)


def _block_means(diff) -> tuple[float, float]:
    height, width = diff.shape
    rows = [j * height // DENSE_GRID for j in range(DENSE_GRID + 1)]
    cols = [i * width // DENSE_GRID for i in range(DENSE_GRID + 1)]
    means = [
        float(block.mean())
        for i in range(DENSE_GRID)
        for j in range(DENSE_GRID)
        if (block := diff[rows[j] : rows[j + 1], cols[i] : cols[i + 1]]).size
    ]
    if not means:
        return 0.0, 0.0
    return sum(means) / len(means), max(means)


def _dense_frame_difference(ta, tb) -> tuple[float, float]:
    """Mean and worst-block grayscale difference for one aligned frame pair."""
    import numpy as np

    diff = np.abs(
        np.asarray(ta.convert("L"), dtype=np.int16) - np.asarray(tb.convert("L"), dtype=np.int16)
    )
    return _block_means(diff)


def _dense_chroma_difference(ta, tb) -> tuple[float, float]:
    """Mean and worst-block Cb/Cr chroma difference for one aligned frame pair."""
    import numpy as np

    ya = np.asarray(ta.convert("YCbCr"), dtype=np.int16)[:, :, 1:]
    yb = np.asarray(tb.convert("YCbCr"), dtype=np.int16)[:, :, 1:]
    diff = np.abs(ya - yb).max(axis=2)
    return _block_means(diff)


def _detail_frames_for_path(path: str, crop: tuple[int, int, int, int] | None = None):
    key = _stat_key(path)
    if key is None:
        return None
    rgb, _rgba, orig_w, orig_h = _verification_frames_cached(*key)
    if rgb is None:
        return None
    return tuple(_crop_frame(frame, crop, orig_w, orig_h).convert("L") for frame in rgb)


def _fine_detail_matches(
    path_a: str,
    path_b: str,
    orientation_a: str | None,
    *,
    crop_a: tuple[int, int, int, int] | None = None,
    crop_b: tuple[int, int, int, int] | None = None,
) -> bool:
    from PIL import Image

    from .image_detail import aligned_detail_matches

    left = _detail_frames_for_path(path_a, crop=crop_a)
    right = _detail_frames_for_path(path_b, crop=crop_b)
    if not left or not right or len(left) != len(right):
        return False

    def pair_matches(a_frame, b_frame) -> bool:
        a_turned = _orient(a_frame, orientation_a)
        # Never invent detail by enlarging a small download. Both images get
        # the same sampling grid at the lower available resolution.
        size = min(a_turned.width, b_frame.width), min(a_turned.height, b_frame.height)
        return aligned_detail_matches(
            a_turned.resize(size, Image.Resampling.LANCZOS),
            b_frame.resize(size, Image.Resampling.LANCZOS),
        )

    if all(pair_matches(a, b) for a, b in zip(left, right, strict=True)):
        return True
    n_frames = len(left)
    if n_frames <= 1:
        return False
    # Symmetric +-1-frame temporal tolerance for animations (e.g. FPS-decimated GIFs).
    for k in range(n_frames):
        lo, hi = max(0, k - 1), min(n_frames, k + 2)
        if not any(pair_matches(left[k], right[j]) for j in range(lo, hi)):
            return False
        if not any(pair_matches(left[j], right[k]) for j in range(lo, hi)):
            return False
    return True


def dense_difference(
    path_a: str,
    path_b: str,
    *,
    orientation_a: str | None = None,
    crop_a: tuple[int, int, int, int] | None = None,
    crop_b: tuple[int, int, int, int] | None = None,
) -> tuple[float, float] | None:
    """Mean and worst-block mean abs difference of aligned grayscale samples.

    None if thumbnails fail to load or the still/animation sample counts differ.
    """
    frames_a = _dense_thumbs_for_path(path_a, orientation_a, crop=crop_a)
    frames_b = _dense_thumbs_for_path(path_b, crop=crop_b)
    if not frames_a or not frames_b or len(frames_a) != len(frames_b):
        return None
    differences = [
        _dense_frame_difference(ta, tb) for ta, tb in zip(frames_a, frames_b, strict=True)
    ]
    return (
        sum(mean for mean, _worst in differences) / len(differences),
        max(worst for _mean, worst in differences),
    )


def _dense_frame_pair_ok(
    ta,
    tb,
    local_max: float,
    concentration: float,
    *,
    chroma_only_flag: list[bool] | None = None,
) -> bool:
    import numpy as np

    color_diff = np.abs(
        np.asarray(ta, dtype=np.int16) - np.asarray(tb, dtype=np.int16)
    )
    if float(color_diff.reshape(-1, 4).mean(axis=0).max()) > DEFAULT_DENSE_COLOR_MEAN_MAX:
        return False
    mean, worst = _dense_frame_difference(ta, tb)
    if worst > local_max and mean > 0 and worst / mean > concentration:
        return False
    # Alpha-only edits can reveal/hide a local subject without changing RGB.
    if np.any(color_diff[:, :, 3]):
        mean, worst = _dense_frame_difference(ta.getchannel("A"), tb.getchannel("A"))
        if worst > local_max and mean > 0 and worst / mean > concentration:
            return False
    # Isoluminant local patches keep grayscale identical while shifting Cb/Cr.
    c_mean, c_worst = _dense_chroma_difference(ta, tb)
    chroma_limit = max(local_max * 2.0, 6.0)
    if c_worst > DEFAULT_DENSE_COLOR_MEAN_MAX * 2.0 or (
        c_worst > chroma_limit and c_mean > 0 and c_worst / c_mean > concentration
    ):
        if chroma_only_flag is not None:
            chroma_only_flag[0] = True
        return False
    return True


def is_dense_match(
    path_a: str,
    path_b: str,
    *,
    local_max: float = DEFAULT_DENSE_LOCAL_MAX,
    concentration: float = DEFAULT_DENSE_CONCENTRATION,
    orientation_a: str | None = None,
    crop_a: tuple[int, int, int, int] | None = None,
    crop_b: tuple[int, int, int, int] | None = None,
) -> bool:
    """Require similar colors/transparency and no concentrated local change.

    The local check rejects when the worst block exceeds local_max AND
    dominates the overall mean by concentration. The color check also rejects
    large, spread-out differences invisible to grayscale hashes. Missing
    thumbnails cannot verify a match, even when cached tile hashes agree.
    """
    _LAST_DENSE_FAILURE.aligned_local_reject = False
    frames_a = _dense_thumbs_for_path(path_a, orientation_a, crop=crop_a)
    frames_b = _dense_thumbs_for_path(path_b, crop=crop_b)
    if not frames_a or not frames_b or len(frames_a) != len(frames_b):
        return False

    def all_frames_ok() -> bool:
        chroma_flag = [False]
        if all(
            _dense_frame_pair_ok(
                ta, tb, local_max, concentration, chroma_only_flag=chroma_flag
            )
            for ta, tb in zip(frames_a, frames_b, strict=True)
        ):
            return True
        n_frames = len(frames_a)
        if n_frames > 1:
            for k in range(n_frames):
                lo, hi = max(0, k - 1), min(n_frames, k + 2)
                if not any(
                    _dense_frame_pair_ok(frames_a[k], frames_b[j], local_max, concentration)
                    for j in range(lo, hi)
                ):
                    return False
                if not any(
                    _dense_frame_pair_ok(frames_a[j], frames_b[k], local_max, concentration)
                    for j in range(lo, hi)
                ):
                    return False
            return True
        if chroma_flag[0]:
            _LAST_DENSE_FAILURE.aligned_local_reject = True
        return False

    if not all_frames_ok():
        return False
    if _fine_detail_matches(path_a, path_b, orientation_a, crop_a=crop_a, crop_b=crop_b):
        return True
    _LAST_DENSE_FAILURE.aligned_local_reject = True
    return False


def _color_balance_agrees(
    path_a: str,
    path_b: str,
    *,
    crop_a: tuple[int, int, int, int] | None = None,
    crop_b: tuple[int, int, int, int] | None = None,
) -> bool:
    """Whole-image color balance, independent of crop and alignment.

    Feature alignment compares grayscale, so it cannot see a recolored
    subject. Each channel's mean is taken relative to the image's own overall
    mean, so exposure changes cancel. A monochrome side passes: black-and-white
    conversion is an edit of the same photo.
    """
    import numpy as np

    balances = []
    for path, crop in ((path_a, crop_a), (path_b, crop_b)):
        frames = _dense_thumbs_for_path(path, crop=crop)
        if not frames:
            return False
        means = np.asarray(frames[0], dtype=np.float32)[:, :, :3].reshape(-1, 3).mean(axis=0)
        balance = means - means.mean()
        if float(np.abs(balance).max()) < MONOCHROME_BALANCE_MAX:
            return True
        balances.append(balance)
    return float(np.abs(balances[0] - balances[1]).max()) <= DEFAULT_DENSE_COLOR_MEAN_MAX


def encode_tile_phashes(values: tuple[str, ...]) -> str:
    """Serialize tile hashes with the version that produced them."""
    return f"{TILE_HASH_VERSION}:" + ",".join(values)


def decode_tile_phashes(stored: str | None) -> tuple[str, ...] | None:
    """Parse stored tile hashes, ignoring values from an older tiling pipeline."""
    if not stored or not stored.startswith(f"{TILE_HASH_VERSION}:"):
        return None
    values = tuple(part for part in stored[len(TILE_HASH_VERSION) + 1 :].split(",") if part)
    return values or None


def _record_tile_phashes(record: FileRecord) -> tuple[str, ...] | None:
    """Load regional hashes once, retaining them on the record for the disk cache."""
    values = decode_tile_phashes(record.tile_phashes)
    if values:
        return values
    values = _tile_phashes_for_path(record.path)
    if values:
        record.tile_phashes = encode_tile_phashes(values)
    return values


def _compatible_aspect_ratios(
    a: FileRecord,
    b: FileRecord,
    orientation_a: str | None = None,
    *,
    crop_a: tuple[int, int, int, int] | None = None,
    crop_b: tuple[int, int, int, int] | None = None,
) -> bool:
    a_width = (crop_a[2] - crop_a[0]) if crop_a is not None else a.width
    a_height = (crop_a[3] - crop_a[1]) if crop_a is not None else a.height
    b_width = (crop_b[2] - crop_b[0]) if crop_b is not None else b.width
    b_height = (crop_b[3] - crop_b[1]) if crop_b is not None else b.height
    if not (a_width and a_height and b_width and b_height):
        return True
    if orientation_a in QUARTER_TURN_ORIENTATIONS:
        a_width, a_height = a_height, a_width
    left = a_width * b_height
    right = b_width * a_height
    return min(left, right) / max(left, right) >= MIN_ASPECT_RATIO_SIMILARITY


@lru_cache(maxsize=1)
def _mih_probe_masks():
    import numpy as np

    m0 = np.array([0], dtype=np.uint16)
    m1 = np.array([0] + [1 << b for b in range(16)], dtype=np.uint16)
    m2 = np.array(
        [0]
        + [1 << b for b in range(16)]
        + [(1 << i) | (1 << j) for i in range(16) for j in range(i + 1, 16)],
        dtype=np.uint16,
    )
    return m0, m1, m2


class HammingIndex:
    """All stored 64-bit hashes within a radius via 4x16-bit Multi-Index Hashing.

    Each 64-bit hash is indexed into four 16-bit CSR sub-tables. Using Norouzi's
    exact pigeonhole split with 0-, 1-, and 2-bit probes per sub-table keeps all
    radii up to 11 indexed (with O(1) slice lookups) instead of falling back to
    an O(N) full scan at r >= 8. Wider radii fall back to vectorized popcount.
    """

    MIH_CHUNKS = 4
    MIH_BITS = 16
    MAX_INDEXED_RADIUS = 11

    def __init__(self, hashes, radius: int) -> None:
        import numpy as np

        self._np = np
        self.keys = list(hashes)
        self.radius = radius
        self.values = np.array(self.keys, dtype=np.uint64)
        self._tables: list[tuple] = []
        if not self.keys:
            return
        for chunk_idx in range(self.MIH_CHUNKS):
            shift = np.uint64(chunk_idx * self.MIH_BITS)
            chunks = ((self.values >> shift) & np.uint64(0xFFFF)).astype(np.uint16)
            counts = np.bincount(chunks, minlength=65536)
            offsets = np.empty(65537, dtype=np.int32)
            offsets[0] = 0
            np.cumsum(counts, out=offsets[1:])
            order = np.argsort(chunks, kind="stable").astype(np.int32)
            self._tables.append((offsets, order))

    def _popcount(self, values):
        np = self._np
        if hasattr(np, "bitwise_count"):
            return np.bitwise_count(values)
        as_bytes = values.view(np.uint8).reshape(-1, 8)
        return np.unpackbits(as_bytes, axis=1).sum(axis=1)

    @staticmethod
    def _probe_plan(radius: int) -> list[tuple[int, int]]:
        q, rem = divmod(radius, 4)
        plan = []
        for chunk_idx in range(4):
            max_err = q if chunk_idx <= rem else q - 1
            if max_err >= 0:
                plan.append((chunk_idx, max_err))
        return plan

    def find(self, query: int, radius: int | None = None) -> list[tuple[int, int]]:
        """(distance, hash) for every stored hash within radius of query."""
        np = self._np
        radius = self.radius if radius is None else radius
        if not self.keys or radius < 0:
            return []
        if self._tables and radius <= self.MAX_INDEXED_RADIUS:
            masks = _mih_probe_masks()
            hits = []
            for chunk_idx, max_err in self._probe_plan(radius):
                offsets, order = self._tables[chunk_idx]
                chunk = (query >> (chunk_idx * self.MIH_BITS)) & 0xFFFF
                probes = (np.uint16(chunk) ^ masks[max_err]).astype(np.int32)
                starts = offsets[probes]
                ends = offsets[probes + 1]
                for start, end in zip(starts.tolist(), ends.tolist(), strict=True):
                    if end > start:
                        hits.append(order[start:end])
            if not hits:
                return []
            indexes = np.unique(np.concatenate(hits))
        else:
            indexes = np.arange(len(self.keys))
        distances = self._popcount(self.values[indexes] ^ np.uint64(query))
        near = np.flatnonzero(distances <= radius)
        return [(int(distances[i]), self.keys[int(indexes[i])]) for i in near]

    # Candidate pairs examined per batch of find_many: bounds its working
    # arrays to tens of megabytes however large the library is.
    BATCH_PAIRS = 1 << 21

    def find_many(self, queries, radius: int | None = None) -> list[tuple[int, int, object]]:
        """(query position, distance, hash) for every match of every query."""
        np = self._np
        radius = self.radius if radius is None else radius
        queries = np.asarray(queries, dtype=np.uint64)
        if not self.keys or not len(queries) or radius < 0:
            return []
        indexed = bool(self._tables) and radius <= self.MAX_INDEXED_RADIUS
        if indexed:
            masks = _mih_probe_masks()
            plan = self._probe_plan(radius)
            total_probes = sum(len(masks[err]) for _chunk, err in plan)
            per_query = max(1.0, total_probes * max(1.0, len(self.keys) / 65536.0))
        else:
            masks = None
            plan = []
            per_query = float(len(self.keys))
        step = max(1, int(self.BATCH_PAIRS // max(1.0, per_query)))
        found: list[tuple[int, int, object]] = []
        n_keys = len(self.keys)
        keys = self.keys
        for begin in range(0, len(queries), step):
            batch = queries[begin : begin + step]
            if indexed:
                matches = []
                assert masks is not None
                for chunk_idx, max_err in plan:
                    offsets, order = self._tables[chunk_idx]
                    probe_masks = masks[max_err]
                    chunks = ((batch >> np.uint64(chunk_idx * self.MIH_BITS)) & np.uint64(0xFFFF)).astype(
                        np.uint16
                    )
                    probed = (
                        chunks[:, None] ^ probe_masks[None, :]
                    ).astype(np.int32)
                    starts = offsets[probed]
                    sizes = offsets[probed + 1] - starts
                    hit_row, hit_probe = np.nonzero(sizes)
                    if not len(hit_row):
                        continue
                    hit_starts = starts[hit_row, hit_probe]
                    hit_sizes = sizes[hit_row, hit_probe]
                    ends = np.cumsum(hit_sizes)
                    within = np.arange(int(ends[-1]), dtype=np.int32) - np.repeat(
                        ends - hit_sizes, hit_sizes
                    )
                    row = np.repeat(hit_row, hit_sizes)
                    column = order[np.repeat(hit_starts, hit_sizes) + within]
                    near = self._popcount(self.values[column] ^ batch[row]) <= radius
                    if np.any(near):
                        matches.append(
                            row[near].astype(np.int64) * np.int64(n_keys) + column[near]
                        )
                if not matches:
                    continue
                pairs = np.unique(np.concatenate(matches))
                row, column = np.divmod(pairs, n_keys)
                distances = self._popcount(self.values[column] ^ batch[row])
            else:
                row = np.repeat(np.arange(len(batch)), n_keys)
                column = np.tile(np.arange(n_keys), len(batch))
                distances = self._popcount(self.values[column] ^ batch[row])
                near = distances <= radius
                row, distances, column = row[near], distances[near], column[near]
            found.extend(
                (begin + r, d, keys[c])
                for r, d, c in zip(
                    row.tolist(), distances.tolist(), column.tolist(), strict=True
                )
            )
        return found


_INVERSE_ORIENTATION = {
    "ROTATE_90": "ROTATE_270",
    "ROTATE_270": "ROTATE_90",
    "ROTATE_180": "ROTATE_180",
    "FLIP_LEFT_RIGHT": "FLIP_LEFT_RIGHT",
    "FLIP_TOP_BOTTOM": "FLIP_TOP_BOTTOM",
    "TRANSPOSE": "TRANSPOSE",
    "TRANSVERSE": "TRANSVERSE",
}


def _cropped_roi_hashes(
    path: str, crop: tuple[int, int, int, int]
) -> tuple[int, tuple[str, ...]] | None:
    """Compute cropped-ROI pHash and 7 turned pHashes from the unified 768px cache."""
    import imagehash

    key = _stat_key(path)
    if key is None:
        return None
    rgb, _rgba, orig_w, orig_h = _verification_frames_cached(*key)
    if not rgb:
        return None
    roi = _crop_frame(rgb[0], crop, orig_w, orig_h)
    return int(str(imagehash.phash(roi)), 16), _orientation_phashes(roi)


def _group_runs_by_first(
    items: list,
    key_fn: Callable,
    *,
    max_run_size: int = MAX_VERIFY_RUN_SIZE,
) -> list[list]:
    """Group consecutive items sharing ``key_fn`` into bounded chunks for work balance."""
    limit = max(1, int(max_run_size))
    runs: list[list] = []
    for item in items:
        key = key_fn(item)
        if runs and key_fn(runs[-1][0]) == key and len(runs[-1]) < limit:
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def _is_byte_identical_pair(a: FileRecord, b: FileRecord) -> bool:
    """True when both records already carry matching non-empty SHA-256 & size."""
    return bool(
        a.size > 0
        and a.size == b.size
        and a.sha256
        and a.sha256 == b.sha256
        and a.media_type == b.media_type
    )


def find_similar_image_groups(
    records: list[FileRecord],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    dhash_threshold: int = DHASH_THRESHOLD,
    tile_max: int = DEFAULT_TILE_MAX,
    tile_mean: float = DEFAULT_TILE_MEAN,
    dense_local_max: float = DEFAULT_DENSE_LOCAL_MAX,
    dense_concentration: float = DEFAULT_DENSE_CONCENTRATION,
    skip_paths: set[str] | None = None,
    distinct: DistinctReviews | None = None,
    progress: ProgressCb | None = None,
    workers: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[list[FileRecord]]:
    """
    Cluster near-identical images/GIFs.

    1. Global pHash via a 4x16-bit multi-index Hamming lookup (fast candidates),
       also with still rotated/mirrored pHashes, animation midpoint pHashes,
       and border-cropped ROI secondary pHashes when ``border_crop`` is set
    2. Secondary dHash + aspect-ratio filter
    3. Regional tile pHash (reject pose / composition changes)
    4. Dense, chroma, and fine detail checks (reject small local subject changes)
    """
    skip_paths = skip_paths or set()
    distinct = distinct or DistinctReviews.empty()
    n_workers = stage_workers(workers, cap=DEFAULT_IMAGE_WORKERS_CAP)
    media = [
        r
        for r in records
        if r.media_type in (MediaType.IMAGE, MediaType.GIF) and r.path not in skip_paths
    ]
    total = len(media)
    if total < 2:
        return []

    # Compute hashes (parallel when uncached)
    need = []
    backfilling = set()
    for record in media:
        # Tiles carry the version of the decode that produced every hash of
        # the file. Discard obsolete hashes before trying the decode again.
        if not (record.phash and record.dhash and decode_tile_phashes(record.tile_phashes)):
            record.phash = record.dhash = record.tile_phashes = None
            record.orientation_phashes = None
            need.append(record)
        elif (
            record.media_type == MediaType.IMAGE
            and not is_animation_orientation_sentinel(record.orientation_phashes)
            and not decode_orientation_phashes(record.orientation_phashes)
        ):
            # Only the turns are missing. Keep upright hashes if backfill fails.
            backfilling.add(record.path)
            need.append(record)
    cached = total - len(need)
    if need:
        by_path = {r.path: r for r in need}

        def hash_progress(done: int, _total: int) -> None:
            if progress:
                progress("image-hash", cached + done, total)

        # Threads for small batches (I/O + Pillow C decode release the GIL
        # often enough, and there is no process-spawn cost); a process pool
        # once the uncached batch is large enough to amortize worker startup.
        backend = (
            "process"
            if n_workers > 1 and len(need) >= PROCESS_POOL_MIN_IMAGES
            else "thread"
        )
        hash_chunk = (
            min(16, max(1, len(need) // max(1, n_workers))) if n_workers > 1 else 1
        )
        results = map_parallel(
            _image_hash_job,
            [r.path for r in need],
            workers=n_workers,
            backend=backend,
            progress=hash_progress,
            progress_every=8,
            cancelled=cancelled,
            chunk_size=hash_chunk,
        )
        for path, ph, dh, w, h, tiles, orientations, err in results:
            rec = by_path[path]
            if err:
                if path not in backfilling:
                    rec.error = err
                continue
            rec.phash = ph
            rec.dhash = dh
            if tiles:
                rec.tile_phashes = encode_tile_phashes(tiles)
            if isinstance(orientations, str):
                rec.orientation_phashes = orientations
            elif orientations:
                rec.orientation_phashes = encode_orientation_phashes(orientations)
            if w:
                rec.width = w
            if h:
                rec.height = h
            if rec.capture_metadata is None:
                cached_meta = get_cached_capture_metadata(rec)
                if cached_meta is not None:
                    rec.capture_metadata = cached_meta

    if progress:
        progress("image-hash", total, total)

    hashed = [r for r in media if r.phash]
    if len(hashed) < 2:
        return []

    dhashes = {
        record.path: int(record.dhash, 16)
        for record in hashed
        if record.dhash
    }
    crop_rects: dict[str, tuple[int, int, int, int]] = {}
    crop_orientations: dict[int, tuple[str, ...]] = {}
    # Each pHash maps to (record_idx, is_cropped, is_midpoint)
    by_phash: dict[int, list[tuple[int, bool, bool]]] = {}
    crop_targets: list[tuple[int, str, tuple[int, int, int, int]]] = []
    for idx, record in enumerate(hashed):
        by_phash.setdefault(int(record.phash or "0", 16), []).append((idx, False, False))
        mid_phash = decode_animation_midpoint_phash(record.orientation_phashes)
        if mid_phash is not None:
            by_phash.setdefault(mid_phash, []).append((idx, False, True))
        if record.media_type == MediaType.IMAGE:
            parsed_crop = _parse_border_crop(record.border_crop, (record.width, record.height))
            if parsed_crop is not None:
                crop_targets.append((idx, record.path, parsed_crop[:4]))

    if crop_targets:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")

        def _crop_roi_job(
            item: tuple[int, str, tuple[int, int, int, int]],
        ) -> tuple[int, str, tuple[int, int, int, int], tuple[int, tuple[str, ...]] | None]:
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            idx, path, crop_rect = item
            return idx, path, crop_rect, _cropped_roi_hashes(path, crop_rect)

        def _crop_roi_progress(done: int, roi_total: int) -> None:
            if progress:
                progress("image-crop-hash", done, roi_total)

        roi_results = map_parallel(
            _crop_roi_job,
            crop_targets,
            workers=n_workers,
            backend="thread",
            progress=_crop_roi_progress,
            progress_every=4,
            cancelled=cancelled,
        )
        for idx, path, crop_rect, roi_hashes in roi_results:
            if roi_hashes is not None:
                crop_phash, crop_turns = roi_hashes
                crop_rects[path] = crop_rect
                crop_orientations[idx] = crop_turns
                by_phash.setdefault(crop_phash, []).append((idx, True, False))

    tree = HammingIndex(by_phash, threshold)
    adjacency: dict[str, set[str]] = {record.path: set() for record in hashed}
    near_misses: list[tuple[FileRecord, FileRecord, tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]] = []

    def verified_mode(
        rec: FileRecord,
        other: FileRecord,
        orientation: str | None,
        crop_a: tuple[int, int, int, int] | None,
        crop_b: tuple[int, int, int, int] | None,
        *,
        skip_dhash: bool = False,
    ) -> str:
        """Return ``matched``, ``detail_rejected``, or ``near_miss``."""
        if not _compatible_aspect_ratios(rec, other, orientation, crop_a=crop_a, crop_b=crop_b):
            return "near_miss"
        use_stored_tiles = orientation is None and crop_a is None and crop_b is None
        if use_stored_tiles:
            if (
                not skip_dhash
                and rec.path in dhashes
                and other.path in dhashes
                and (dhashes[rec.path] ^ dhashes[other.path]).bit_count() > dhash_threshold
            ):
                return "near_miss"
            tiles_a = _record_tile_phashes(rec)
            tiles_b = _record_tile_phashes(other)
            if not tiles_a or not tiles_b:
                return "near_miss"
        else:
            tiles_a = None
            tiles_b = _record_tile_phashes(other) if crop_b is None else None
        if not is_near_identical(
            rec.path,
            other.path,
            tile_max=tile_max,
            tile_mean=tile_mean,
            tiles_a=tiles_a,
            tiles_b=tiles_b,
            dimensions_a=(rec.width, rec.height),
            dimensions_b=(other.width, other.height),
            orientation_a=orientation,
            crop_a=crop_a,
            crop_b=crop_b,
        ):
            return "near_miss"
        _LAST_DENSE_FAILURE.aligned_local_reject = False
        if is_dense_match(
            rec.path,
            other.path,
            local_max=dense_local_max,
            concentration=dense_concentration,
            orientation_a=orientation,
            crop_a=crop_a,
            crop_b=crop_b,
        ):
            return "matched"
        if (
            orientation is None
            and crop_a is None
            and crop_b is None
            and getattr(_LAST_DENSE_FAILURE, "aligned_local_reject", False)
            and rec.path not in crop_rects
            and other.path not in crop_rects
        ):
            return "detail_rejected"
        return "near_miss"

    # Candidate lookup is index arithmetic; verification decodes both files.
    # Deduplicate (q_hash, matched_hash) pairs so symmetric edges are checked once.
    upright: dict[tuple[int, int], bool] = {}  # (i, j) -> skip_dhash
    cropped_upright: set[tuple[int, int]] = set()
    distinct_hashes = list(by_phash)
    for q_idx, _distance, matched_hash in tree.find_many(distinct_hashes, threshold):
        q_hash = distinct_hashes[q_idx]
        if matched_hash < q_hash:
            continue
        left_entries = by_phash[q_hash]
        right_entries = by_phash[matched_hash]
        same_hash = q_hash == matched_hash
        for a_idx, a_crop, a_mid in left_entries:
            for b_idx, b_crop, b_mid in right_entries:
                if same_hash and b_idx <= a_idx:
                    continue
                if a_idx == b_idx:
                    continue
                pair = (a_idx, b_idx) if a_idx < b_idx else (b_idx, a_idx)
                if a_crop or b_crop:
                    cropped_upright.add(pair)
                else:
                    skip_dh = a_mid or b_mid
                    upright[pair] = upright.get(pair, False) or skip_dh
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")

    turns: dict[tuple[int, int], list[tuple[int, str, bool]]] = {}
    turned_queries: list[int] = []
    turned_source: list[tuple[int, int, bool]] = []
    for i, record in enumerate(hashed):
        if record.media_type != MediaType.IMAGE:
            continue
        turned = decode_orientation_phashes(record.orientation_phashes)
        if turned:
            for turn, value in enumerate(turned):
                turned_queries.append(int(value, 16))
                turned_source.append((i, turn, False))
        if i in crop_orientations:
            for turn, value in enumerate(crop_orientations[i]):
                turned_queries.append(int(value, 16))
                turned_source.append((i, turn, True))
    for query, distance, matched_hash in tree.find_many(turned_queries, threshold):
        i, turn, i_crop = turned_source[query]
        orientation_name = ORIENTATIONS[turn]
        for j, j_crop, _j_mid in by_phash[matched_hash]:
            if j == i or hashed[j].media_type != MediaType.IMAGE:
                continue
            if i < j:
                turns.setdefault((i, j), []).append((distance, orientation_name, i_crop or j_crop))
            else:
                inv = _INVERSE_ORIENTATION[orientation_name]
                turns.setdefault((j, i), []).append((distance, inv, i_crop or j_crop))
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")

    jobs: list[tuple[int, FileRecord, FileRecord, list[tuple[str | None, bool, bool]]]] = []
    all_pairs = sorted(upright.keys() | cropped_upright | turns.keys())
    for i, j in all_pairs:
        rec, other = hashed[i], hashed[j]
        if distinct and distinct.is_distinct(rec, other):
            continue
        # List of (orientation_name, use_crop, skip_dhash)
        modes: list[tuple[str | None, bool, bool]] = []
        seen_modes: set[tuple[str | None, bool]] = set()
        if (i, j) in upright:
            modes.append((None, False, upright[(i, j)]))
            seen_modes.add((None, False))
        if (i, j) in cropped_upright:
            modes.append((None, True, True))
            seen_modes.add((None, True))
        for _distance, orient_name, is_crop_mode in sorted(turns.get((i, j), ())):
            mode_key = (orient_name, is_crop_mode)
            if mode_key not in seen_modes:
                seen_modes.add(mode_key)
                modes.append((orient_name, is_crop_mode, True))
        jobs.append((i, rec, other, modes))

    def verify_pair(job) -> tuple[bool, bool]:
        _i, rec, other, modes = job
        if _is_byte_identical_pair(rec, other):
            return True, False
        crop_a = crop_rects.get(rec.path)
        crop_b = crop_rects.get(other.path)
        near_miss = False
        detail_rejected = False
        for orientation, use_crop, skip_dh in modes:
            ca = crop_a if use_crop else None
            cb = crop_b if use_crop else None
            outcome = verified_mode(rec, other, orientation, ca, cb, skip_dhash=skip_dh)
            if outcome == "matched":
                return True, False
            if outcome == "detail_rejected":
                detail_rejected = True
            elif (
                (orientation is None or use_crop)
                and rec.media_type == MediaType.IMAGE
                and other.media_type == MediaType.IMAGE
            ):
                near_miss = True
        if detail_rejected and not near_miss and crop_a is None and crop_b is None:
            return False, False
        return False, near_miss

    def verify_run(run: list) -> list[tuple[bool, bool]]:
        out = []
        for job in run:
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            out.append(verify_pair(job))
        return out

    job_runs = _group_runs_by_first(jobs, lambda job: job[0])
    last_cluster_emit = [0.0]

    def cluster_progress(done: int, run_total: int, *, second_pass: bool = False) -> None:
        if not (progress and run_total):
            return
        now = time.monotonic()
        if (
            done == run_total
            or done == 1
            or done % 4 == 0
            or now - last_cluster_emit[0] >= VERIFY_PROGRESS_MIN_INTERVAL
        ):
            last_cluster_emit[0] = now
            half = len(hashed) // 2
            position = half * done // run_total + (half if second_pass else 0)
            progress("image-cluster", min(position, len(hashed) - 1), len(hashed))

    try:
        run_outcomes = map_parallel(
            verify_run,
            job_runs,
            workers=n_workers,
            backend="thread",
            progress=cluster_progress,
            cancelled=cancelled,
        )
        for run, outcomes in zip(job_runs, run_outcomes, strict=True):
            for (_i, rec, other, _modes), (matched, near_miss) in zip(run, outcomes, strict=True):
                if matched:
                    adjacency[rec.path].add(other.path)
                    adjacency[other.path].add(rec.path)
                elif near_miss and not _is_byte_identical_pair(rec, other):
                    near_misses.append(
                        (rec, other, crop_rects.get(rec.path), crop_rects.get(other.path))
                    )

        # A crop, border, or color edit misaligns the regional checks above
        # even when the global hash agrees. Feature alignment verifies those
        # copies with the same fine-detail bar, so poses stay separate.
        pending = near_misses
        if pending:
            from .edited_copy import verify_edited_copy

            def verify_edited_run(run: list) -> list[bool]:
                out = []
                for rec, other, ca, cb in run:
                    if cancelled and cancelled():
                        raise InterruptedError("scan cancelled")
                    out.append(
                        verify_edited_copy(rec.path, other.path, crop_a=ca, crop_b=cb)
                        and _color_balance_agrees(rec.path, other.path, crop_a=ca, crop_b=cb)
                    )
                return out

            pending_runs = _group_runs_by_first(pending, lambda item: item[0].path)
            run_verdicts = map_parallel(
                verify_edited_run,
                pending_runs,
                workers=n_workers,
                backend="thread",
                progress=lambda done, run_total: cluster_progress(
                    done, run_total, second_pass=True
                ),
                cancelled=cancelled,
            )
            for run, verdicts in zip(pending_runs, run_verdicts, strict=True):
                for (rec, other, _ca, _cb), same in zip(run, verdicts, strict=True):
                    if same:
                        adjacency[rec.path].add(other.path)
                        adjacency[other.path].add(rec.path)

        if progress:
            progress("image-cluster", len(hashed), len(hashed))
    finally:
        # Records retain hashes for this run and the disk cache; free path cache.
        clear_image_comparison_cache()

    return cluster_around_best(hashed, adjacency, distinct, cancelled=cancelled)


def clear_image_comparison_cache() -> None:
    """Release comparison pixels and stat-keyed verification caches at a scan boundary."""
    _verification_frames_cached.cache_clear()
    _tile_phashes_cached.cache_clear()
    _dense_thumbs_cached.cache_clear()
    with _CAPTURE_METADATA_LOCK:
        _CAPTURE_METADATA_CACHE.clear()
    from .edited_copy import clear_edited_copy_cache

    clear_edited_copy_cache()


def _bruteforce_groups(
    hashed: list[FileRecord],
    threshold: int,
    dhash_threshold: int,
    tile_max: int,
    tile_mean: float,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
    distinct: DistinctReviews | None = None,
    dense_local_max: float = DEFAULT_DENSE_LOCAL_MAX,
    dense_concentration: float = DEFAULT_DENSE_CONCENTRATION,
) -> list[list[FileRecord]]:
    import imagehash

    distinct = distinct or DistinctReviews.empty()
    adjacency: dict[str, set[str]] = {record.path: set() for record in hashed}

    for i, a in enumerate(hashed):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        ha = imagehash.hex_to_hash(a.phash)  # type: ignore[arg-type]
        for b in hashed[i + 1 :]:
            if distinct and distinct.is_distinct(a, b):
                continue
            if not _compatible_aspect_ratios(a, b):
                continue
            hb = imagehash.hex_to_hash(b.phash)  # type: ignore[arg-type]
            if (ha - hb) > threshold:
                continue
            if a.dhash and b.dhash and (
                imagehash.hex_to_hash(a.dhash) - imagehash.hex_to_hash(b.dhash)
            ) > dhash_threshold:
                continue
            if not is_near_identical(
                a.path, b.path, tile_max=tile_max, tile_mean=tile_mean,
                dimensions_a=(a.width, a.height),
                dimensions_b=(b.width, b.height),
            ):
                continue
            if not is_dense_match(
                a.path,
                b.path,
                local_max=dense_local_max,
                concentration=dense_concentration,
            ):
                continue
            adjacency[a.path].add(b.path)
            adjacency[b.path].add(a.path)
        if progress and (i + 1) % 20 == 0:
            progress("image-cluster", i + 1, len(hashed))

    clear_image_comparison_cache()

    return cluster_around_best(hashed, adjacency, distinct)


def jpeg_bitstream_sha256(
    path: str | Path,
    *,
    cancelled: Callable[[], bool] | None = None,
    chunk_size: int = JPEG_BITSTREAM_CHUNK_SIZE,
) -> str | None:
    """SHA-256 of JPEG image bitstream (SOI..EOI), skipping APP0..APP15/COM/post-EOI.

    Streams the file in chunks instead of loading the entire JPEG into RAM.
    Returns ``None`` for non-JPEG files, unreadable paths, or malformed streams.
    """
    step = max(16, int(chunk_size))
    try:
        with open(path, "rb") as stream:
            return _jpeg_bitstream_sha256_stream(stream, step=step, cancelled=cancelled)
    except InterruptedError:
        raise
    except OSError:
        return None


def _jpeg_bitstream_sha256_stream(
    stream,
    *,
    step: int,
    cancelled: Callable[[], bool] | None,
) -> str | None:
    file_size = os.fstat(stream.fileno()).st_size
    if file_size < 4:
        return None

    buf = b""
    pos = 0

    def check_cancel() -> None:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")

    def refill() -> bool:
        nonlocal buf, pos
        check_cancel()
        chunk = stream.read(step)
        if not chunk:
            return False
        buf = chunk
        pos = 0
        return True

    def read_byte() -> int | None:
        nonlocal pos
        if pos >= len(buf) and not refill():
            return None
        val = buf[pos]
        pos += 1
        return val

    def read_exact(count: int) -> bytes | None:
        nonlocal buf, pos
        if count == 0:
            return b""
        avail = len(buf) - pos
        if avail >= count:
            out = buf[pos : pos + count]
            pos += count
            return out
        parts = [buf[pos:]] if avail else []
        need = count - avail
        buf = b""
        pos = 0
        while need > 0:
            check_cancel()
            chunk = stream.read(max(step, need))
            if not chunk:
                return None
            if len(chunk) >= need:
                parts.append(chunk[:need])
                buf = chunk[need:]
                pos = 0
                need = 0
            else:
                parts.append(chunk)
                need -= len(chunk)
        return b"".join(parts)

    def skip_bytes(count: int) -> bool:
        nonlocal buf, pos
        avail = len(buf) - pos
        if avail >= count:
            pos += count
            return True
        remaining = count - avail
        buf = b""
        pos = 0
        target = stream.tell() + remaining
        if target > file_size:
            return False
        stream.seek(target, os.SEEK_SET)
        return True

    def hash_bytes(hasher_obj, count: int) -> bool:
        nonlocal buf, pos
        remaining = count
        while remaining > 0:
            if pos >= len(buf) and not refill():
                return False
            take = min(remaining, len(buf) - pos)
            hasher_obj.update(buf[pos : pos + take])
            pos += take
            remaining -= take
        return True

    soi = read_exact(2)
    if soi != b"\xff\xd8":
        return None
    hasher = hashlib.sha256()
    hasher.update(b"\xff\xd8")
    saw_sos = False

    while True:
        check_cancel()
        lead = read_byte()
        if lead != 0xFF:
            return None
        marker = read_byte()
        while marker == 0xFF:
            marker = read_byte()
        if marker is None or marker in (0x00, 0xD8):
            return None
        if marker == 0xD9:  # EOI
            if not saw_sos:
                return None
            hasher.update(b"\xff\xd9")
            return hasher.hexdigest()
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # TEM or RST0..RST7
            hasher.update(bytes((0xFF, marker)))
            continue
        len_bytes = read_exact(2)
        if len_bytes is None:
            return None
        seg_len = int.from_bytes(len_bytes, "big")
        if seg_len < 2:
            return None
        payload_len = seg_len - 2
        if 0xE0 <= marker <= 0xEF or marker == 0xFE:  # APP0..APP15 or COM
            if not skip_bytes(payload_len):
                return None
            continue
        hasher.update(bytes((0xFF, marker)))
        hasher.update(len_bytes)
        if not hash_bytes(hasher, payload_len):
            return None
        if marker == 0xDA:  # SOS: scan entropy-coded data up to next marker
            saw_sos = True
            while True:
                if pos >= len(buf) and not refill():
                    return None
                idx = buf.find(b"\xff", pos)
                if idx == -1:
                    hasher.update(buf[pos:])
                    pos = len(buf)
                    continue
                if idx > pos:
                    hasher.update(buf[pos:idx])
                pos = idx + 1
                ff_count = 1
                nxt = read_byte()
                while nxt == 0xFF:
                    ff_count += 1
                    nxt = read_byte()
                if nxt is None:
                    return None
                if nxt == 0x00 or (0xD0 <= nxt <= 0xD7):
                    # Byte-stuffing or restart marker inside entropy stream.
                    if ff_count == 1:
                        hasher.update(bytes((0xFF, nxt)))
                    else:
                        hasher.update(b"\xff" * ff_count)
                        hasher.update(bytes((nxt,)))
                    continue
                # Push back the marker (0xFF + nxt) for the outer marker loop.
                buf = bytes((0xFF, nxt)) + buf[pos:]
                pos = 0
                break


def canonical_pixel_sha256(path: str | Path) -> str | None:
    """SHA-256 of canonical EXIF-transposed sRGB pixel bytes for still images."""
    ensure_heif_registered()
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            if is_animated_image(img):
                return None
            icc = getattr(img, "info", {}).get("icc_profile")
            has_alpha = _has_alpha_channel(img)
            upright = ImageOps.exif_transpose(img)
            mode = "RGBA" if has_alpha else "RGB"
            converted = upright.convert(mode)
            converted = _convert_icc_to_srgb(converted, icc)
            if converted.mode == "RGBA":
                min_alpha, max_alpha = converted.getchannel("A").getextrema()
                if min_alpha == 255 and max_alpha == 255:
                    converted = converted.convert("RGB")
            header = f"{converted.mode}:{converted.width}x{converted.height}:".encode("ascii")
            hasher = hashlib.sha256()
            hasher.update(header)
            hasher.update(converted.tobytes())
            return hasher.hexdigest()
    except Exception:
        return None
