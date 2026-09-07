"""Perceptual near-duplicate detection for images and GIFs.

Uses global pHash/dHash for candidate finding, then a regional tile pHash
check to reject "same scene, different pose" false positives while still
matching true duplicates at different resolutions/quality.

Memory-conscious: images are drafted/thumbnail-scaled before hashing so a
12MP phone photo never becomes a full-res RGB buffer in the worker pool.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from .grouping import cluster_around_best
from .models import FileRecord, MediaType
from .parallel import DEFAULT_IMAGE_WORKERS_CAP, map_parallel, resolve_workers

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

# pHash/dHash only need ~32×32 DCT input; anything larger is wasted decode/RAM.
# 512 keeps edge structure for rescaled/quality variants without loading full HEIC/JPEG.
HASH_MAX_SIDE = 512
# Normalize to this size before tiling so different resolutions compare fairly.
TILE_NORMALIZE = 256
# Only records without stored tile hashes reach the path cache; keep it large
# enough that a big scan never re-decodes the same file twice.
TILE_CACHE_SIZE = 65536
# Animation tiles retain ordered, time-aligned samples. Older first-frame-only
# tiles cannot verify animations and must be recomputed.
TILE_HASH_VERSION = "t3"
ANIMATION_HASH_FRAMES = 8
MIN_ASPECT_RATIO_SIMILARITY = 0.95
# A process pool sidesteps GIL contention in the pHash/dHash/tile math (small
# numpy/scipy ops hold the GIL even though Pillow's decoder releases it), but
# costs an interpreter spawn plus Pillow/imagehash imports per worker. Only
# batches at least this large amortize that startup; smaller ones use threads.
PROCESS_POOL_MIN_IMAGES = 512


def _ensure_image_deps() -> None:
    try:
        import imagehash  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "Image similar detection requires Pillow and ImageHash. "
            "Install with: pip install Pillow ImageHash pillow-heif"
        ) from exc


def _register_heif() -> None:
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass


def _downscale_for_hash(img, max_side: int = HASH_MAX_SIDE):
    """Return RGB image scaled so longest side ≤ max_side (in-place safe copy)."""
    from PIL import Image as PILImage

    rgb = img.convert("RGB")
    if max(rgb.size) > max_side:
        # Bilinear is enough pre-pHash and much cheaper than LANCZOS on 12MP shots.
        rgb.thumbnail((max_side, max_side), PILImage.Resampling.BILINEAR)
    return rgb


def probe_image_dimensions(path: str | Path) -> tuple[int | None, int | None]:
    """Read display-oriented image/GIF dimensions without computing hashes."""
    _ensure_image_deps()
    _register_heif()

    from PIL import Image, ImageOps

    with Image.open(path) as img:
        if not getattr(img, "is_animated", False):
            img = ImageOps.exif_transpose(img)
        width, height = img.size
        return int(width), int(height)


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
    _ensure_image_deps()
    _register_heif()

    import imagehash
    from PIL import Image, ImageOps

    path = Path(path)
    with Image.open(path) as img:
        animated = bool(getattr(img, "is_animated", False))
        if not animated:
            img = ImageOps.exif_transpose(img)
        # Capture original dimensions before draft (draft can shrink reported size).
        width, height = img.size

        # JPEG (and some formats): request a smaller decode where supported.
        try:
            img.draft("RGB", (HASH_MAX_SIDE, HASH_MAX_SIDE))
        except Exception:
            pass

        frames = _hash_frames(img)
        # Candidate lookup uses the first frame. Verification below preserves
        # frame order; XOR used to erase order and cancel repeated frame hashes.
        phash = str(imagehash.phash(frames[0]))
        dhash = str(imagehash.dhash(frames[0]))

        tiles: tuple[str, ...] | None = None
        if with_tiles:
            try:
                tiles = tuple(str(t) for frame in frames for t in _tile_phashes_from_image(frame))
            except Exception:
                tiles = None

        return phash, dhash, width, height, tiles


def _hash_frames(img) -> list:
    """Sample animations in playback order and time, independent of frame rate."""
    if not getattr(img, "is_animated", False):
        return [_downscale_for_hash(img)]
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
        frames.append(_downscale_for_hash(img))
    return frames


def _image_hash_job(
    path: str,
) -> tuple[
    str, str | None, str | None, int | None, int | None, tuple[str, ...] | None, str | None
]:
    """Worker: (path, phash, dhash, width, height, tile_phashes, error)."""
    try:
        ph, dh, w, h, tiles = compute_image_hashes_with_tiles(path)
        return path, ph, dh, w, h, tiles, None
    except Exception as exc:
        return path, None, None, None, None, None, f"{ERROR_IMAGE_HASH_FAILED}: {exc}"


def _tile_phashes_from_image(img) -> list:
    """pHash of 4 quadrants + center crop after size normalization."""
    import imagehash
    from PIL import Image as PILImage

    # Letterbox into a fixed square so aspect ratio is preserved and scales match.
    canvas = PILImage.new("RGB", (TILE_NORMALIZE, TILE_NORMALIZE), (0, 0, 0))
    src = img.convert("RGB")
    src.thumbnail((TILE_NORMALIZE, TILE_NORMALIZE), PILImage.Resampling.BILINEAR)
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


@lru_cache(maxsize=TILE_CACHE_SIZE)
def _tile_phashes_for_path(path: str) -> tuple[str, ...] | None:
    """Run-cached tile hashes for records that carry no stored tile hashes."""
    _ensure_image_deps()
    _register_heif()
    from PIL import Image, ImageOps

    try:
        with Image.open(path) as img:
            if not getattr(img, "is_animated", False):
                img = ImageOps.exif_transpose(img)
            # Same decode ladder as the hashing pass so lazily computed tiles
            # are identical to the ones stored during hashing.
            try:
                img.draft("RGB", (HASH_MAX_SIDE, HASH_MAX_SIDE))
            except Exception:
                pass
            tiles = [tile for frame in _hash_frames(img) for tile in _tile_phashes_from_image(frame)]
            return tuple(str(t) for t in tiles)
    except Exception:
        return None


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


def is_near_identical(
    path_a: str,
    path_b: str,
    *,
    tile_max: int = DEFAULT_TILE_MAX,
    tile_mean: float = DEFAULT_TILE_MEAN,
    tiles_a: tuple[str, ...] | None = None,
    tiles_b: tuple[str, ...] | None = None,
) -> bool:
    """
    True if regional structure matches (same image / scale / quality variants).
    False for same-person different-pose shots that can still pass global pHash.
    """
    if tiles_a is None:
        tiles_a = _tile_phashes_for_path(path_a)
    if tiles_b is None:
        tiles_b = _tile_phashes_for_path(path_b)
    if not tiles_a or not tiles_b or len(tiles_a) != len(tiles_b):
        # Missing evidence or a still/animation mismatch cannot establish similarity.
        return False
    dists = [(int(a, 16) ^ int(b, 16)).bit_count() for a, b in zip(tiles_a, tiles_b, strict=True)]
    if max(dists) > tile_max:
        return False
    return not sum(dists) / len(dists) > tile_mean


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


def _compatible_aspect_ratios(a: FileRecord, b: FileRecord) -> bool:
    if not (a.width and a.height and b.width and b.height):
        return True
    left = a.width * b.height
    right = b.width * a.height
    return min(left, right) / max(left, right) >= MIN_ASPECT_RATIO_SIMILARITY


def find_similar_image_groups(
    records: list[FileRecord],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    dhash_threshold: int = DHASH_THRESHOLD,
    tile_max: int = DEFAULT_TILE_MAX,
    tile_mean: float = DEFAULT_TILE_MEAN,
    skip_paths: set[str] | None = None,
    distinct_pairs: set[tuple[str, str]] | None = None,
    progress: ProgressCb | None = None,
    workers: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[list[FileRecord]]:
    """
    Cluster near-identical images/GIFs.

    1. Global pHash via BK-tree (fast candidates)
    2. Secondary dHash + aspect-ratio filter
    3. Regional tile pHash (reject pose / composition changes)
    """
    skip_paths = skip_paths or set()
    distinct_pairs = distinct_pairs or set()
    n_workers = resolve_workers(workers, cap=DEFAULT_IMAGE_WORKERS_CAP)
    media = [
        r
        for r in records
        if r.media_type in (MediaType.IMAGE, MediaType.GIF) and r.path not in skip_paths
    ]
    total = len(media)
    if total < 2:
        return []

    # Compute hashes (parallel when uncached)
    need = [
        r for r in media
        if not (r.phash and r.dhash)
        # Old GIF global hashes XORed frames. Never compare them to first-frame hashes.
        or (r.media_type == MediaType.GIF and not decode_tile_phashes(r.tile_phashes))
    ]
    cached = total - len(need)
    if need:
        by_path = {r.path: r for r in need}
        for record in need:
            record.phash = record.dhash = record.tile_phashes = None

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
        results = map_parallel(
            _image_hash_job,
            [r.path for r in need],
            workers=n_workers,
            backend=backend,
            progress=hash_progress,
            progress_every=1,
            cancelled=cancelled,
        )
        for path, ph, dh, w, h, tiles, err in results:
            rec = by_path[path]
            if err:
                rec.error = err
                continue
            rec.phash = ph
            rec.dhash = dh
            if tiles:
                rec.tile_phashes = encode_tile_phashes(tiles)
            if w:
                rec.width = w
            if h:
                rec.height = h

    if progress:
        progress("image-hash", total, total)

    hashed = [r for r in media if r.phash]
    if len(hashed) < 2:
        return []

    # BK-tree for fast lookup
    try:
        import pybktree
    except ImportError:
        return _bruteforce_groups(
            hashed,
            threshold,
            dhash_threshold,
            tile_max,
            tile_mean,
            progress,
            cancelled,
            distinct_pairs,
        )

    # Parse each hash once. The old FileRecord-based distance function converted
    # both hexadecimal hashes on every BK-tree comparison, which dominates at
    # tens of thousands of records. Keep duplicate hashes in a side mapping
    # because a BK-tree stores each integer key only once.
    by_phash: dict[int, list[FileRecord]] = {}
    for record in hashed:
        by_phash.setdefault(int(record.phash or "0", 16), []).append(record)
    tree = pybktree.BKTree(lambda a, b: (a ^ b).bit_count(), by_phash)
    dhashes = {
        record.path: int(record.dhash, 16)
        for record in hashed
        if record.dhash
    }
    positions = {record.path: i for i, record in enumerate(hashed)}

    adjacency: dict[str, set[str]] = {record.path: set() for record in hashed}

    try:
        for i, rec in enumerate(hashed):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            matches = tree.find(int(rec.phash or "0", 16), threshold)
            candidates = (
                other
                for _distance, matched_hash in matches
                for other in by_phash[matched_hash]
                # Every pair only needs regional verification once.
                if positions[other.path] > i
            )
            for other in candidates:
                if tuple(sorted((rec.path, other.path))) in distinct_pairs:
                    continue
                # Secondary dHash check to reduce false positives
                if rec.path in dhashes and other.path in dhashes:
                    dhash_distance = (
                        dhashes[rec.path] ^ dhashes[other.path]
                    ).bit_count()
                    if dhash_distance > dhash_threshold:
                        continue
                # Near-identical: also prefer similar aspect ratio
                if not _compatible_aspect_ratios(rec, other):
                    continue
                tiles_a = _record_tile_phashes(rec)
                tiles_b = _record_tile_phashes(other)
                if not tiles_a or not tiles_b:
                    continue
                # Regional structure: reject different pose / composition
                if not is_near_identical(
                    rec.path,
                    other.path,
                    tile_max=tile_max,
                    tile_mean=tile_mean,
                    tiles_a=tiles_a,
                    tiles_b=tiles_b,
                ):
                    continue
                adjacency[rec.path].add(other.path)
                adjacency[other.path].add(rec.path)
            if progress and (i + 1) % 20 == 0:
                progress("image-cluster", i + 1, len(hashed))

        if progress:
            progress("image-cluster", len(hashed), len(hashed))
    finally:
        # Records retain hashes for this run and the disk cache; free path cache.
        _tile_phashes_for_path.cache_clear()

    return cluster_around_best(hashed, adjacency, distinct_pairs)


def _bruteforce_groups(
    hashed: list[FileRecord],
    threshold: int,
    dhash_threshold: int,
    tile_max: int,
    tile_mean: float,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
    distinct_pairs: set[tuple[str, str]] | None = None,
) -> list[list[FileRecord]]:
    import imagehash

    distinct_pairs = distinct_pairs or set()
    adjacency: dict[str, set[str]] = {record.path: set() for record in hashed}

    for i, a in enumerate(hashed):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        ha = imagehash.hex_to_hash(a.phash)  # type: ignore[arg-type]
        for b in hashed[i + 1 :]:
            if tuple(sorted((a.path, b.path))) in distinct_pairs:
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
                a.path, b.path, tile_max=tile_max, tile_mean=tile_mean
            ):
                continue
            adjacency[a.path].add(b.path)
            adjacency[b.path].add(a.path)
        if progress and (i + 1) % 20 == 0:
            progress("image-cluster", i + 1, len(hashed))

    _tile_phashes_for_path.cache_clear()

    return cluster_around_best(hashed, adjacency, distinct_pairs)
