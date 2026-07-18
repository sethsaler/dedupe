"""Perceptual near-duplicate detection for images and GIFs.

Uses global pHash/dHash for candidate finding, then a regional tile pHash
check to reject "same scene, different pose" false positives while still
matching true duplicates at different resolutions/quality.

Memory-conscious: images are drafted/thumbnail-scaled before hashing so a
12MP phone photo never becomes a full-res RGB buffer in the worker pool.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from .grouping import cluster_around_best
from .models import FileRecord, MediaType
from .parallel import DEFAULT_IMAGE_WORKERS_CAP, map_parallel, resolve_workers

ProgressCb = Callable[[str, int, int], None]

# Near-identical default (strict). Hamming distance on 64-bit pHash.
DEFAULT_THRESHOLD = 6
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


def compute_image_hashes(path: str | Path) -> tuple[str | None, str | None, int | None, int | None]:
    """Return (phash_hex, dhash_hex, width, height).

    Width/height are the *original* dimensions. Hashing works on a downscaled
    copy so large photos stay cheap in RAM/CPU.
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

        # For animated GIF: sample first / mid / last frames.
        frames = []
        if animated and path.suffix.lower() == ".gif":
            n = getattr(img, "n_frames", 1) or 1
            indices = sorted({0, n // 2, max(0, n - 1)})
            for i in indices:
                img.seek(i)
                frames.append(_downscale_for_hash(img))
        else:
            frames = [_downscale_for_hash(img)]

        # Primary fingerprint from first frame (near-identical intent).
        # For multi-frame GIFs, XOR sampled frame hashes so re-encoded clones still match.
        phashes = [imagehash.phash(f) for f in frames]
        dhashes = [imagehash.dhash(f) for f in frames]

        if len(phashes) == 1:
            phash = str(phashes[0])
            dhash = str(dhashes[0])
        else:
            combined_p = phashes[0]
            combined_d = dhashes[0]
            for h in phashes[1:]:
                combined_p = imagehash.ImageHash(combined_p.hash ^ h.hash)
            for h in dhashes[1:]:
                combined_d = imagehash.ImageHash(combined_d.hash ^ h.hash)
            phash = str(combined_p)
            dhash = str(combined_d)

        return phash, dhash, width, height


def _image_hash_job(
    path: str,
) -> tuple[str, str | None, str | None, int | None, int | None, str | None]:
    """Worker: (path, phash, dhash, width, height, error)."""
    try:
        ph, dh, w, h = compute_image_hashes(path)
        return path, ph, dh, w, h, None
    except Exception as exc:
        return path, None, None, None, None, f"image hash failed: {exc}"


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


@lru_cache(maxsize=2048)
def _tile_phashes_for_path(path: str) -> tuple[str, ...] | None:
    """Cached tile hashes as hex strings for a path."""
    _ensure_image_deps()
    _register_heif()
    from PIL import Image, ImageOps

    try:
        with Image.open(path) as img:
            if not getattr(img, "is_animated", False):
                img = ImageOps.exif_transpose(img)
            try:
                img.draft("RGB", (TILE_NORMALIZE, TILE_NORMALIZE))
            except Exception:
                pass
            tiles = _tile_phashes_from_image(img)
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
) -> bool:
    """
    True if regional structure matches (same image / scale / quality variants).
    False for same-person different-pose shots that can still pass global pHash.
    """
    dists = tile_distances(path_a, path_b)
    if dists is None:
        # Fail open only when we cannot verify — safer for rare decode errors
        # is fail closed for "similar"; require tiles when possible.
        return False
    if max(dists) > tile_max:
        return False
    if (sum(dists) / len(dists)) > tile_mean:
        return False
    return True


def find_similar_image_groups(
    records: list[FileRecord],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    dhash_threshold: int = DHASH_THRESHOLD,
    tile_max: int = DEFAULT_TILE_MAX,
    tile_mean: float = DEFAULT_TILE_MEAN,
    skip_paths: set[str] | None = None,
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
    need = [r for r in media if not (r.phash and r.dhash)]
    cached = total - len(need)
    if need:
        by_path = {r.path: r for r in need}

        def hash_progress(done: int, _total: int) -> None:
            if progress:
                progress("image-hash", cached + done, total)

        results = map_parallel(
            _image_hash_job,
            [r.path for r in need],
            workers=n_workers,
            # Threads: I/O + Pillow C decode release the GIL often enough,
            # and avoid process-spawn / pickle cost on every file.
            backend="thread",
            progress=hash_progress,
            progress_every=5,
            cancelled=cancelled,
        )
        for path, ph, dh, w, h, err in results:
            rec = by_path[path]
            if err:
                rec.error = err
                continue
            rec.phash = ph
            rec.dhash = dh
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
        import imagehash
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
        )

    def distance(a: FileRecord, b: FileRecord) -> int:
        ha = imagehash.hex_to_hash(a.phash)  # type: ignore[arg-type]
        hb = imagehash.hex_to_hash(b.phash)  # type: ignore[arg-type]
        return ha - hb

    tree = pybktree.BKTree(distance, hashed)

    adjacency: dict[str, set[str]] = {record.path: set() for record in hashed}

    verified = 0
    for i, rec in enumerate(hashed):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        matches = tree.find(rec, threshold)
        for dist, other in matches:
            if other.path == rec.path:
                continue
            # Secondary dHash check to reduce false positives
            if rec.dhash and other.dhash:
                try:
                    dh_a = imagehash.hex_to_hash(rec.dhash)
                    dh_b = imagehash.hex_to_hash(other.dhash)
                    if (dh_a - dh_b) > dhash_threshold:
                        continue
                except Exception:
                    pass
            # Near-identical: also prefer similar aspect ratio
            if rec.width and rec.height and other.width and other.height:
                ar_a = rec.width / max(rec.height, 1)
                ar_b = other.width / max(other.height, 1)
                if abs(ar_a - ar_b) > 0.15:
                    continue
            # Regional structure: reject different pose / composition
            if not is_near_identical(
                rec.path,
                other.path,
                tile_max=tile_max,
                tile_mean=tile_mean,
            ):
                continue
            adjacency[rec.path].add(other.path)
            adjacency[other.path].add(rec.path)
            verified += 1
        if progress and (i + 1) % 20 == 0:
            progress("image-cluster", i + 1, len(hashed))

    if progress:
        progress("image-cluster", len(hashed), len(hashed))

    # Drop cache entries for this run so memory does not grow unbounded across scans
    _tile_phashes_for_path.cache_clear()

    return cluster_around_best(hashed, adjacency)


def _bruteforce_groups(
    hashed: list[FileRecord],
    threshold: int,
    dhash_threshold: int,
    tile_max: int,
    tile_mean: float,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[list[FileRecord]]:
    import imagehash

    adjacency: dict[str, set[str]] = {record.path: set() for record in hashed}

    for i, a in enumerate(hashed):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        ha = imagehash.hex_to_hash(a.phash)  # type: ignore[arg-type]
        for b in hashed[i + 1 :]:
            hb = imagehash.hex_to_hash(b.phash)  # type: ignore[arg-type]
            if (ha - hb) > threshold:
                continue
            if a.dhash and b.dhash:
                if (imagehash.hex_to_hash(a.dhash) - imagehash.hex_to_hash(b.dhash)) > dhash_threshold:
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

    return cluster_around_best(hashed, adjacency)
