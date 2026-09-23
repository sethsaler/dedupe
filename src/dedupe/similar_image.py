"""Perceptual near-duplicate detection for images and GIFs.

Uses global pHash/dHash for candidate finding, then a regional tile pHash
check to reject "same scene, different pose" false positives, then a dense
detail check to reject burst-like pairs whose difference is concentrated in
one spot (a blink, a shifted hand) while still matching true duplicates at
different resolutions/quality.

Memory-conscious: images are drafted/thumbnail-scaled before hashing so a
12MP phone photo never becomes a full-res RGB buffer in the worker pool.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

from .cache import DistinctReviews
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
# each RGB channel's mean absolute difference (0–255) as well: a loose 32-level
# allowance retains re-exports/moderate exposure edits, not different colors.
DEFAULT_DENSE_COLOR_MEAN_MAX = 32.0
# RGB thumbnails use up to 64 KB each in Pillow's storage; cap near 64 MB.
DENSE_CACHE_SIZE = 1024

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
    phash, dhash, width, height, tiles, _orientations = compute_image_fingerprint(
        path, with_tiles=with_tiles, with_orientations=False
    )
    return phash, dhash, width, height, tiles


def compute_image_fingerprint(
    path: str | Path, *, with_tiles: bool = True, with_orientations: bool = True
) -> tuple[
    str | None, str | None, int | None, int | None, tuple[str, ...] | None, tuple[str, ...] | None
]:
    """Return (phash, dhash, width, height, tile_phashes, orientation_phashes).

    ``orientation_phashes`` holds the pHash of each ``ORIENTATIONS`` entry for
    still images, and is None for animations.
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

        orientations: tuple[str, ...] | None = None
        if with_orientations and not animated:
            orientations = _orientation_phashes(frames[0])

        return phash, dhash, width, height, tiles, orientations


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
    values = tuple(stored[len(ORIENTATION_HASH_VERSION) + 1 :].split(","))
    return values if len(values) == len(ORIENTATIONS) and all(values) else None


def _orient(img, orientation: str | None):
    if orientation is None:
        return img
    from PIL import Image as PILImage

    return img.transpose(PILImage.Transpose[orientation])


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
    str,
    str | None,
    str | None,
    int | None,
    int | None,
    tuple[str, ...] | None,
    tuple[str, ...] | None,
    str | None,
]:
    """Worker: (path, phash, dhash, width, height, tile_phashes, orientation_phashes, error)."""
    try:
        ph, dh, w, h, tiles, orientations = compute_image_fingerprint(path)
        return path, ph, dh, w, h, tiles, orientations, None
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


@lru_cache(maxsize=TILE_CACHE_SIZE)
def _tile_phashes_for_path(
    path: str, comparison_side: int = TILE_NORMALIZE, orientation: str | None = None
) -> tuple[str, ...] | None:
    """Run-cached tile hashes for records that carry no stored tile hashes.

    ``orientation`` turns every frame first, for verifying a rotated or
    mirrored pair; those hashes are never stored on the record.
    """
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
            tiles = [
                tile for frame in _hash_frames(img)
                for tile in _tile_phashes_from_image(_orient(frame, orientation), comparison_side)
            ]
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
    dimensions_a: tuple[int | None, int | None] | None = None,
    dimensions_b: tuple[int | None, int | None] | None = None,
    orientation_a: str | None = None,
) -> bool:
    """
    True if regional structure matches (same image / scale / quality variants).
    False for same-person different-pose shots that can still pass global pHash.

    ``orientation_a`` compares path_a turned that way; stored ``tiles_a`` are
    for the file as decoded, so they are ignored for a turned comparison.
    """
    if orientation_a is not None:
        tiles_a = _tile_phashes_for_path(path_a, TILE_NORMALIZE, orientation_a)
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
    if all(dimensions_a) and all(dimensions_b):
        side_a = min(TILE_NORMALIZE, max(dimensions_a))
        side_b = min(TILE_NORMALIZE, max(dimensions_b))
        if side_a != side_b:
            # thumbnail never enlarges small downloads. Compare at the smaller
            # copy's scale so both hashes see the same content and padding.
            # Keep pair-specific hashes out of the persistent per-file cache.
            side = min(side_a, side_b)
            if side_a > side:
                tiles_a = _tile_phashes_for_path(path_a, side, orientation_a)
            if side_b > side:
                tiles_b = _tile_phashes_for_path(path_b, side)
            if not tiles_a or not tiles_b or len(tiles_a) != len(tiles_b):
                return False
    dists = [(int(a, 16) ^ int(b, 16)).bit_count() for a, b in zip(tiles_a, tiles_b, strict=True)]
    if max(dists) > tile_max:
        return False
    return not sum(dists) / len(dists) > tile_mean


@lru_cache(maxsize=DENSE_CACHE_SIZE)
def _dense_thumb_for_path(path: str, orientation: str | None = None):
    """Run-cached RGB thumbnail for color and dense detail verification.

    Same decode ladder as the tile path so stills and animation first frames
    compare under identical normalization. Resizing (not letterboxing) keeps
    tiny images comparable without padding mismatches. A turned thumbnail is
    the upright one transposed, so verifying a rotated pair never re-decodes.
    """
    if orientation is not None:
        upright = _dense_thumb_for_path(path)
        return None if upright is None else _orient(upright, orientation)
    _ensure_image_deps()
    _register_heif()
    from PIL import Image, ImageOps

    try:
        with Image.open(path) as img:
            if not getattr(img, "is_animated", False):
                img = ImageOps.exif_transpose(img)
            try:
                img.draft("RGB", (HASH_MAX_SIDE, HASH_MAX_SIDE))
            except Exception:
                pass
            if getattr(img, "is_animated", False):
                frame = _hash_frames(img)[0]
            else:
                frame = _downscale_for_hash(img)
            return frame.resize(
                (DENSE_THUMB_SIZE, DENSE_THUMB_SIZE), Image.Resampling.BILINEAR
            )
    except Exception:
        return None


def dense_difference(
    path_a: str, path_b: str, *, orientation_a: str | None = None
) -> tuple[float, float] | None:
    """Mean and worst-block mean abs difference of grayscale thumbnails.

    None if either thumbnail fails to load.
    """
    import numpy as np

    ta = _dense_thumb_for_path(path_a, orientation_a)
    tb = _dense_thumb_for_path(path_b)
    if ta is None or tb is None:
        return None
    diff = np.abs(
        np.asarray(ta.convert("L"), dtype=np.int16) - np.asarray(tb.convert("L"), dtype=np.int16)
    )
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


def is_dense_match(
    path_a: str,
    path_b: str,
    *,
    local_max: float = DEFAULT_DENSE_LOCAL_MAX,
    concentration: float = DEFAULT_DENSE_CONCENTRATION,
    orientation_a: str | None = None,
) -> bool:
    """Require similar colors and no concentrated, burst-like local change.

    The local check rejects when the worst block exceeds local_max AND
    dominates the overall mean by concentration. The color check also rejects
    large, spread-out differences invisible to grayscale hashes. Missing
    thumbnails cannot verify a match, even when cached tile hashes agree.
    """
    import numpy as np

    ta = _dense_thumb_for_path(path_a, orientation_a)
    tb = _dense_thumb_for_path(path_b)
    if ta is None or tb is None:
        return False
    color_diff = np.abs(
        np.asarray(ta.convert("RGB"), dtype=np.int16) - np.asarray(tb.convert("RGB"), dtype=np.int16)
    )
    if float(color_diff.reshape(-1, 3).mean(axis=0).max()) > DEFAULT_DENSE_COLOR_MEAN_MAX:
        return False
    result = dense_difference(path_a, path_b, orientation_a=orientation_a)
    if result is None:
        return False
    mean, worst = result
    if worst <= local_max or mean <= 0:
        return True
    return not worst / mean > concentration


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
    a: FileRecord, b: FileRecord, orientation_a: str | None = None
) -> bool:
    if not (a.width and a.height and b.width and b.height):
        return True
    a_width, a_height = a.width, a.height
    if orientation_a in QUARTER_TURN_ORIENTATIONS:
        a_width, a_height = a_height, a_width
    left = a_width * b.height
    right = b.width * a_height
    return min(left, right) / max(left, right) >= MIN_ASPECT_RATIO_SIMILARITY


class HammingIndex:
    """All stored 64-bit hashes within a radius of a query, by pigeonhole.

    Split into radius + 1 bit chunks, two hashes within the radius agree
    exactly on at least one chunk, so candidates come from radius + 1 dict
    lookups and one vectorized popcount. A BK-tree over near-uniform 64-bit
    pHashes visits a large share of its nodes at radius 6 (about 2.6 ms per
    query at 50,000 images); this stays well under a tenth of that. Radii too
    wide for useful chunks scan every hash, still vectorized.
    """

    MIN_CHUNK_BITS = 8

    def __init__(self, hashes, radius: int) -> None:
        import numpy as np

        self._np = np
        self.keys = list(hashes)
        self.radius = radius
        self.values = np.array(self.keys, dtype=np.uint64)
        pieces = radius + 1
        self.buckets: list[tuple[int, int, dict[int, object]]] = []
        if pieces * self.MIN_CHUNK_BITS > 64 or not self.keys:
            return
        edges = [round(i * 64 / pieces) for i in range(pieces + 1)]
        for lo, hi in pairwise(edges):
            mask = (1 << (hi - lo)) - 1
            chunks = (self.values >> np.uint64(lo)) & np.uint64(mask)
            order = np.argsort(chunks, kind="stable")
            ordered = chunks[order]
            starts = np.flatnonzero(np.r_[True, ordered[1:] != ordered[:-1]])
            groups = np.split(order, starts[1:])
            table = {int(ordered[start]): group for start, group in zip(starts, groups, strict=True)}
            self.buckets.append((lo, mask, table))

    def _popcount(self, values):
        np = self._np
        if hasattr(np, "bitwise_count"):
            return np.bitwise_count(values)
        as_bytes = values.view(np.uint8).reshape(-1, 8)
        return np.unpackbits(as_bytes, axis=1).sum(axis=1)

    def find(self, query: int, radius: int | None = None) -> list[tuple[int, int]]:
        """(distance, hash) for every stored hash within radius of query."""
        np = self._np
        radius = self.radius if radius is None else radius
        if not self.keys:
            return []
        if self.buckets and radius <= self.radius:
            hits = [
                table[chunk]
                for lo, mask, table in self.buckets
                if (chunk := (query >> lo) & mask) in table
            ]
            if not hits:
                return []
            indexes = np.unique(np.concatenate(hits))
        else:
            indexes = np.arange(len(self.keys))
        distances = self._popcount(self.values[indexes] ^ np.uint64(query))
        near = np.flatnonzero(distances <= radius)
        return [(int(distances[i]), self.keys[int(indexes[i])]) for i in near]


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

    1. Global pHash via a multi-index Hamming lookup (fast candidates), also
       with each still's rotated and mirrored pHashes
    2. Secondary dHash + aspect-ratio filter
    3. Regional tile pHash (reject pose / composition changes)
    4. Dense detail check (reject burst-like concentrated local changes)
    """
    skip_paths = skip_paths or set()
    distinct = distinct or DistinctReviews.empty()
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
    # Stills cached before orientation lookup existed are hashed once more to
    # add it. Their stored hashes stay valid, so they are kept if that fails:
    # such a file is still found upright, just not turned.
    backfill = [
        r for r in media
        if r.media_type == MediaType.IMAGE
        and r.phash
        and r.dhash
        and not decode_orientation_phashes(r.orientation_phashes)
    ]
    for record in need:
        record.phash = record.dhash = record.tile_phashes = None
        record.orientation_phashes = None
    need += backfill
    backfilling = {r.path for r in backfill}
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
        results = map_parallel(
            _image_hash_job,
            [r.path for r in need],
            workers=n_workers,
            backend=backend,
            progress=hash_progress,
            progress_every=1,
            cancelled=cancelled,
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
            if orientations:
                rec.orientation_phashes = encode_orientation_phashes(orientations)
            if w:
                rec.width = w
            if h:
                rec.height = h

    if progress:
        progress("image-hash", total, total)

    hashed = [r for r in media if r.phash]
    if len(hashed) < 2:
        return []

    # Parse each hash once and keep records sharing a hash in a side mapping,
    # so the index stores each distinct integer hash once.
    by_phash: dict[int, list[FileRecord]] = {}
    for record in hashed:
        by_phash.setdefault(int(record.phash or "0", 16), []).append(record)
    tree = HammingIndex(by_phash, threshold)
    dhashes = {
        record.path: int(record.dhash, 16)
        for record in hashed
        if record.dhash
    }
    positions = {record.path: i for i, record in enumerate(hashed)}

    adjacency: dict[str, set[str]] = {record.path: set() for record in hashed}

    def verified(rec: FileRecord, other: FileRecord, orientation: str | None) -> bool:
        """Regional + dense verification of rec (turned by orientation) against other."""
        if orientation is None:
            # Secondary dHash check to reduce false positives. dHash has no
            # stored orientations; turned pairs rely on the regional checks.
            if (
                rec.path in dhashes
                and other.path in dhashes
                and (dhashes[rec.path] ^ dhashes[other.path]).bit_count() > dhash_threshold
            ):
                return False
            tiles_a = _record_tile_phashes(rec)
            tiles_b = _record_tile_phashes(other)
            if not tiles_a or not tiles_b:
                return False
        else:
            tiles_a = None
            tiles_b = _record_tile_phashes(other)
        # Near-identical: also prefer similar aspect ratio
        if not _compatible_aspect_ratios(rec, other, orientation):
            return False
        # Regional structure: reject different pose / composition
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
        ):
            return False
        # Dense detail: reject burst-like concentrated local changes.
        return is_dense_match(
            rec.path,
            other.path,
            local_max=dense_local_max,
            concentration=dense_concentration,
            orientation_a=orientation,
        )

    try:
        for i, rec in enumerate(hashed):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            # Upright candidates first, then each remaining pair at most once
            # more, in the one orientation whose pHash agrees best. A
            # near-symmetric or near-blank image matches in several
            # orientations; verifying each would re-decode the pair per turn.
            candidates: list[tuple[FileRecord, str | None]] = [
                (other, None)
                for _distance, matched_hash in tree.find(int(rec.phash or "0", 16), threshold)
                for other in by_phash[matched_hash]
            ]
            turned = decode_orientation_phashes(rec.orientation_phashes)
            if turned and rec.media_type == MediaType.IMAGE:
                best_turn: dict[str, tuple[int, str, FileRecord]] = {}
                for name, value in zip(ORIENTATIONS, turned, strict=True):
                    for distance, matched_hash in tree.find(int(value, 16), threshold):
                        for other in by_phash[matched_hash]:
                            if other.media_type != MediaType.IMAGE:
                                continue
                            seen = best_turn.get(other.path)
                            if seen is None or distance < seen[0]:
                                best_turn[other.path] = (distance, name, other)
                candidates.extend((other, name) for _d, name, other in best_turn.values())
            for other, orientation in candidates:
                # Every pair only needs verification once.
                if positions[other.path] <= i or other.path in adjacency[rec.path]:
                    continue
                if distinct and distinct.is_distinct(rec, other):
                    continue
                if not verified(rec, other, orientation):
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
        _dense_thumb_for_path.cache_clear()

    return cluster_around_best(hashed, adjacency, distinct)


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

    _tile_phashes_for_path.cache_clear()
    _dense_thumb_for_path.cache_clear()

    return cluster_around_best(hashed, adjacency, distinct)
