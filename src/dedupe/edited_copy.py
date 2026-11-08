"""Conservative geometric verification of cropped/filtered still-image copies.

Optional OpenCV is used only after semantic candidate discovery. Failed or
ambiguous alignment returns no verdict and never authorizes a file operation.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .image_detail import aligned_detail_matches
from .image_frames import is_animated_image

# Mean per-pixel spread between the strongest and weakest color channel
# below which an image is black and white (JPEG chroma noise stays under 3).
MONOCHROME_SPREAD_MAX = 6.0
# Brightness levels with fewer overlap pixels are interpolated, not fitted.
TONE_CURVE_MIN_SAMPLES = 20


def is_aligned_image_copy(left_path: str, right_path: str) -> bool:
    """Verify ordinary/rescaled/turned copies when feature alignment is unavailable.

    This uses the same regional and fine-detail gates as perceptual matching,
    so an embedding or a language-model verdict cannot bypass local changes.
    The caller must clear the run-scoped comparison caches between scans.
    """
    from PIL import Image

    from .similar_image import (
        MIN_ASPECT_RATIO_SIMILARITY,
        ORIENTATIONS,
        QUARTER_TURN_ORIENTATIONS,
        is_dense_match,
        is_near_identical,
        probe_image_dimensions,
    )

    try:
        dimensions_a = probe_image_dimensions(left_path)
        dimensions_b = probe_image_dimensions(right_path)
        with Image.open(left_path) as left, Image.open(right_path) as right:
            animated = (is_animated_image(left)
                        or is_animated_image(right))
        orientations = (None,) if animated else (None, *ORIENTATIONS)
        for orientation in orientations:
            width_a, height_a = dimensions_a
            width_b, height_b = dimensions_b
            if not all((width_a, height_a, width_b, height_b)):
                return False
            if orientation in QUARTER_TURN_ORIENTATIONS:
                width_a, height_a = height_a, width_a
            aspect_a, aspect_b = width_a * height_b, width_b * height_a
            if min(aspect_a, aspect_b) / max(aspect_a, aspect_b) < MIN_ASPECT_RATIO_SIMILARITY:
                continue
            if is_near_identical(
                left_path, right_path,
                dimensions_a=dimensions_a, dimensions_b=dimensions_b,
                orientation_a=orientation,
            ) and is_dense_match(left_path, right_path, orientation_a=orientation):
                return True
    except Exception:
        # Unsupported formats and decoder errors leave a candidate unverified.
        pass
    return False


@lru_cache(maxsize=32)
def _features(path: str, size: int, mtime_ns: int, ctime_ns: int):
    # Identity keys invalidate cached pixels after a source changes. Keep at
    # most 32 downscaled frames/descriptors, rather than a whole photo library.
    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as source:
        if is_animated_image(source):
            return None, [], None, False
        source.draft("RGB", (768, 768))
        frame = ImageOps.exif_transpose(source).convert("RGB")
        frame.thumbnail((768, 768))
        monochrome = _is_monochrome(np.asarray(frame))
        pixels = np.asarray(frame.convert("L"))
    keypoints, descriptors = cv2.ORB_create(nfeatures=1800).detectAndCompute(pixels, None)
    return pixels, keypoints, descriptors, monochrome


@lru_cache(maxsize=4)
def _color_frame(path: str, size: int, mtime_ns: int, ctime_ns: int):
    """The RGB frame behind ``_features`` pixels, at the same size.

    Only a pair with one monochrome side reads it, so it is decoded lazily.
    """
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as source:
        source.draft("RGB", (768, 768))
        frame = ImageOps.exif_transpose(source).convert("RGB")
        frame.thumbnail((768, 768))
        return np.asarray(frame)


def _color_frame_for(path: str):
    stat = Path(path).stat()
    return _color_frame(path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _is_monochrome(rgb) -> bool:
    import numpy as np

    channels = rgb.reshape(-1, 3)[::7].astype(np.int16)
    spread = channels.max(axis=1) - channels.min(axis=1)
    return float(spread.mean()) < MONOCHROME_SPREAD_MAX


def _match_monochrome(left_path, right_path, warped, right, transform, overlap,
                      left_mono, right_mono):
    """Render the color side through the monochrome side's channel mix.

    Black-and-white edits weight red, green and blue differently from the
    standard grayscale conversion, so skin, sky and clothing shift tone
    unevenly. Three channel weights and an offset reproduce any such mix,
    but cannot add or remove a local change, so fine detail still decides.
    Pairs that are both color or both monochrome are returned unchanged.
    """
    import cv2
    import numpy as np

    if left_mono == right_mono:
        return warped, right
    height, width = right.shape
    if right_mono:
        left_rgb = _color_frame_for(left_path)
        if left_rgb.shape[:2] != _gray_shape(left_path):
            return warped, right
        color = cv2.warpPerspective(left_rgb, transform, (width, height))
        target = right
    else:
        color, target = _color_frame_for(right_path), warped
        if color.shape[:2] != right.shape:
            return warped, right
    samples = color[overlap].astype(np.float64)
    design = np.column_stack([samples, np.ones(len(samples))])
    weights, *_ = np.linalg.lstsq(design, target[overlap].astype(np.float64), rcond=None)
    mixed = np.clip(color.astype(np.float64) @ weights[:3] + weights[3], 0, 255)
    mixed = _tone_curve(mixed.astype(np.uint8), target, overlap)
    return (mixed, right) if right_mono else (warped, mixed)


def _tone_curve(source, target, valid):
    """Map ``source`` levels onto ``target`` with one monotone curve.

    Black-and-white presets usually add contrast. A single increasing curve
    over brightness levels cannot move, add or remove anything in the frame.
    """
    import numpy as np

    levels = source[valid].astype(np.int64)
    values = target[valid].astype(np.float64)
    order = np.argsort(levels, kind="stable")
    levels, values = levels[order], values[order]
    starts = np.searchsorted(levels, np.arange(257))
    curve = np.full(256, np.nan)
    for level in range(256):
        chunk = values[starts[level]:starts[level + 1]]
        if len(chunk) >= TONE_CURVE_MIN_SAMPLES:
            curve[level] = np.median(chunk)
    known = ~np.isnan(curve)
    if known.sum() < 2:
        return source
    curve = np.interp(np.arange(256), np.flatnonzero(known), curve[known])
    curve = np.maximum.accumulate(curve)
    return np.clip(curve[source], 0, 255).astype(np.uint8)


def _gray_shape(path: str):
    stat = Path(path).stat()
    pixels = _features(path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)[0]
    return None if pixels is None else pixels.shape


def edited_copy_verdict(left_path: str, right_path: str) -> bool | None:
    """True when either image, aligned onto the other, shows the same exposure.

    Warping resamples one side: enlarging a crop blurs it, while shrinking
    the larger image can alias fine texture. Either artifact can read as
    lost detail, so each direction is tried, and each applies the full
    fine-detail check. The verdict no longer depends on argument order.
    """
    verdicts = []
    for left, right in ((left_path, right_path), (right_path, left_path)):
        verdict = _aligned_verdict(left, right)
        if verdict is True:
            return True
        verdicts.append(verdict)
    return False if False in verdicts else None


def _aligned_verdict(left_path: str, right_path: str) -> bool | None:
    try:
        import cv2
        import numpy as np

        def features(path):
            stat = Path(path).stat()
            return _features(path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

        left, kp_a, desc_a, left_mono = features(left_path)
        right, kp_b, desc_b, right_mono = features(right_path)
        if left is None or right is None or min(*left.shape, *right.shape) < 64:
            return None
        if desc_a is None or desc_b is None:
            return None
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(desc_a, desc_b, k=2)
        matches = [pair[0] for pair in pairs if len(pair) == 2
                   and pair[0].distance < 0.7 * pair[1].distance]
        if len(matches) < 24:
            return None
        points_a = np.float32([kp_a[m.queryIdx].pt for m in matches])
        points_b = np.float32([kp_b[m.trainIdx].pt for m in matches])
        transform, inliers = cv2.findHomography(points_a, points_b, cv2.RANSAC, 2.5)
        if transform is None or inliers is None:
            return None
        mask = inliers.ravel().astype(bool)
        if int(mask.sum()) < 24 or float(mask.mean()) < 0.75:
            return None
        # Reject matches confined to a logo or a small shared background patch.
        for points, frame in ((points_a[mask], left), (points_b[mask], right)):
            hull_area = cv2.contourArea(cv2.convexHull(points))
            if hull_area / frame.size < 0.2:
                return None
        height, width = right.shape
        warped = cv2.warpPerspective(left, transform, (width, height))
        overlap = cv2.warpPerspective(np.ones_like(left), transform, (width, height)) > 0
        overlap = cv2.erode(overlap.astype("uint8"), np.ones((7, 7), "uint8")) > 0
        if overlap.mean() < 0.45:
            return None
        warped, right = _match_monochrome(
            left_path, right_path, warped, right, transform, overlap, left_mono, right_mono
        )
        # Fit a brightness/contrast change, then inspect every substantial tile.
        # Local pose changes should fail even with many shared background points.
        a = warped[overlap].astype(float) / 255
        b = right[overlap].astype(float) / 255
        if a.std() < 0.04 or b.std() < 0.04:
            return None
        scale = float(np.clip(np.cov(a, b)[0, 1] / a.var(), 0.4, 2.5))
        bias = float(b.mean() - scale * a.mean())
        residual = np.abs((warped.astype(float) / 255) * scale + bias - right / 255)
        tile_errors = []
        for y in range(0, height, max(1, height // 8)):
            for x in range(0, width, max(1, width // 8)):
                valid = overlap[y:y + height // 8, x:x + width // 8]
                if valid.size and valid.mean() > 0.8:
                    tile_errors.append(float(residual[y:y + height // 8,
                                                      x:x + width // 8][valid].mean()))
        if not (len(tile_errors) >= 16 and max(tile_errors) <= 0.075
                and np.mean(tile_errors) <= 0.035):
            return False
        # A shared background can align perfectly while a mouth, eye, or lock
        # of hair has moved. Coarse tile means dilute those changes; require
        # the fine-scale check on the aligned overlap as well.
        return aligned_detail_matches(warped, right, valid=overlap)
    except (ImportError, OSError, ValueError):
        return None
    except Exception:
        # Corrupt media and optional backend errors are an unverified candidate.
        return None


def verify_edited_copy(left_path: str, right_path: str) -> bool:
    return edited_copy_verdict(left_path, right_path) is True
