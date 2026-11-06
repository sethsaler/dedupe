"""Conservative geometric verification of cropped/filtered still-image copies.

Optional OpenCV is used only after semantic candidate discovery. Failed or
ambiguous alignment returns no verdict and never authorizes a file operation.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .image_detail import aligned_detail_matches


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
            animated = (getattr(left, "is_animated", False)
                        or getattr(right, "is_animated", False))
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
        if getattr(source, "is_animated", False):
            return None, [], None
        source.draft("RGB", (768, 768))
        frame = ImageOps.exif_transpose(source).convert("L")
        frame.thumbnail((768, 768))
        pixels = np.asarray(frame)
    keypoints, descriptors = cv2.ORB_create(nfeatures=1800).detectAndCompute(pixels, None)
    return pixels, keypoints, descriptors


def edited_copy_verdict(left_path: str, right_path: str) -> bool | None:
    try:
        import cv2
        import numpy as np

        def features(path):
            stat = Path(path).stat()
            return _features(path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

        left, kp_a, desc_a = features(left_path)
        right, kp_b, desc_b = features(right_path)
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
