"""Conservative geometric verification of cropped/filtered still-image copies.

Optional OpenCV is used only after semantic candidate discovery. Failed or
ambiguous alignment returns no verdict and never authorizes a file operation.
"""
from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

from .image_detail import aligned_detail_matches
from .image_frames import is_animated_image

# Mean per-pixel spread between the strongest and weakest color channel
# below which an image is black and white (JPEG chroma noise stays under 3).
MONOCHROME_SPREAD_MAX = 6.0
# Brightness levels with fewer overlap pixels are interpolated, not fitted.
TONE_CURVE_MIN_SAMPLES = 20
CHROMA_THUMB_SIZE = 128
CHROMA_GRID = 8
MIRRORED_ORIENTATIONS = frozenset(
    {"FLIP_LEFT_RIGHT", "FLIP_TOP_BOTTOM", "TRANSPOSE", "TRANSVERSE"}
)

_CV2_LOCK = threading.Lock()
_CV2_CONFIGURED: int | None = None


def _cv2():
    global _CV2_CONFIGURED
    import cv2

    cv2_id = id(cv2)
    if _CV2_CONFIGURED != cv2_id:
        with _CV2_LOCK:
            if _CV2_CONFIGURED != cv2_id:
                set_threads = getattr(cv2, "setNumThreads", None)
                if callable(set_threads):
                    set_threads(1)
                _CV2_CONFIGURED = cv2_id
    return cv2


def aligned_image_copy_orientation(left_path: str, right_path: str) -> str | None:
    """Return matched orientation name (``"IDENTITY"`` when upright), or ``None``.

    Uses the same regional and fine-detail gates as perceptual matching, so an
    embedding or a language-model verdict cannot bypass local changes.
    """
    from PIL import Image

    from .similar_image import (
        MIN_ASPECT_RATIO_SIMILARITY,
        ORIENTATIONS,
        QUARTER_TURN_ORIENTATIONS,
        ensure_heif_registered,
        is_dense_match,
        is_near_identical,
        probe_image_dimensions,
    )

    ensure_heif_registered()
    try:
        dimensions_a = probe_image_dimensions(left_path)
        dimensions_b = probe_image_dimensions(right_path)
        with Image.open(left_path) as left, Image.open(right_path) as right:
            animated = is_animated_image(left) or is_animated_image(right)
        orientations = (None,) if animated else (None, *ORIENTATIONS)
        for orientation in orientations:
            width_a, height_a = dimensions_a
            width_b, height_b = dimensions_b
            if not all((width_a, height_a, width_b, height_b)):
                return None
            if orientation in QUARTER_TURN_ORIENTATIONS:
                width_a, height_a = height_a, width_a
            aspect_a, aspect_b = width_a * height_b, width_b * height_a
            if min(aspect_a, aspect_b) / max(aspect_a, aspect_b) < MIN_ASPECT_RATIO_SIMILARITY:
                continue
            if is_near_identical(
                left_path,
                right_path,
                dimensions_a=dimensions_a,
                dimensions_b=dimensions_b,
                orientation_a=orientation,
            ) and is_dense_match(left_path, right_path, orientation_a=orientation):
                return "IDENTITY" if orientation is None else orientation
    except Exception:
        # Unsupported formats and decoder errors leave a candidate unverified.
        pass
    return None


def is_aligned_image_copy(left_path: str, right_path: str) -> bool:
    """Verify ordinary/rescaled/turned copies when feature alignment is unavailable.

    This uses the same regional and fine-detail gates as perceptual matching,
    so an embedding or a language-model verdict cannot bypass local changes.
    The caller must clear the run-scoped comparison caches between scans.
    """
    return aligned_image_copy_orientation(left_path, right_path) is not None


def _prepare_rgb_frame(path: str, crop: tuple[int, int, int, int] | None):
    from PIL import Image, ImageOps

    from .similar_image import (
        EXIF_ORIENTATION,
        HEADER_ORIENTATION_FORMATS,
        QUARTER_TURN_EXIF_ORIENTATIONS,
        REDUCED_DECODE_FORMATS,
        _composite_neutral_matte,
        _convert_icc_to_srgb,
        _has_alpha_channel,
        ensure_heif_registered,
    )

    ensure_heif_registered()
    with Image.open(path) as source:
        if is_animated_image(source):
            return None
        icc = getattr(source, "info", {}).get("icc_profile")
        has_alpha = _has_alpha_channel(source)
        orig_w, orig_h = source.size
        if (
            source.format in HEADER_ORIENTATION_FORMATS
            and source.getexif().get(EXIF_ORIENTATION, 1) in QUARTER_TURN_EXIF_ORIENTATIONS
        ):
            orig_w, orig_h = orig_h, orig_w
        if crop is not None:
            crop_w = max(1, crop[2] - crop[0])
            crop_h = max(1, crop[3] - crop[1])
            draft_side = min(4096, max(768, round(768 * max(orig_w / crop_w, orig_h / crop_h))))
        else:
            draft_side = 768
        if source.format in REDUCED_DECODE_FORMATS:
            try:
                source.draft("RGB", (draft_side, draft_side))
            except Exception:
                pass
        frame = ImageOps.exif_transpose(source)
        if crop is not None:
            fw, fh = frame.size
            # Use post-transpose display dimensions when non-header format was decoded.
            ref_w, ref_h = (orig_w, orig_h) if source.format in HEADER_ORIENTATION_FORMATS else (fw, fh)
            if ref_w > 0 and ref_h > 0:
                sx, sy = fw / ref_w, fh / ref_h
                box = (
                    max(0, min(fw - 1, round(crop[0] * sx))),
                    max(0, min(fh - 1, round(crop[1] * sy))),
                    max(1, min(fw, round(crop[2] * sx))),
                    max(1, min(fh, round(crop[3] * sy))),
                )
                if box[2] > box[0] and box[3] > box[1]:
                    frame = frame.crop(box)
        frame = frame.convert("RGBA" if has_alpha else "RGB")
        frame.thumbnail((768, 768))
        frame = _convert_icc_to_srgb(frame, icc)
        return _composite_neutral_matte(frame) if has_alpha else frame.convert("RGB")


@lru_cache(maxsize=128)
def _features(
    path: str,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    crop: tuple[int, int, int, int] | None = None,
):
    # Identity keys invalidate cached pixels after a source changes. Keep at
    # most 128 downscaled frames/descriptors so worker threads do not thrash
    # while staying bounded in RAM.
    import numpy as np
    from PIL import Image

    cv2 = _cv2()
    frame = _prepare_rgb_frame(path, crop)
    if frame is None:
        return None, [], None, False, None
    monochrome = _is_monochrome(np.asarray(frame))
    pixels = np.asarray(frame.convert("L"))
    cbcr = np.asarray(
        frame.resize((CHROMA_THUMB_SIZE, CHROMA_THUMB_SIZE), Image.Resampling.BILINEAR).convert(
            "YCbCr"
        ),
        dtype=np.int16,
    )[:, :, 1:]
    h, w = pixels.shape
    edge_threshold = min(31, max(7, min(h, w) // 4))
    keypoints, descriptors = cv2.ORB_create(
        nfeatures=1800, edgeThreshold=edge_threshold, patchSize=edge_threshold
    ).detectAndCompute(pixels, None)
    return pixels, keypoints, descriptors, monochrome, cbcr


@lru_cache(maxsize=128)
def _flipped_orb_features(
    path: str,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    crop: tuple[int, int, int, int] | None = None,
):
    """Horizontally flipped grayscale frame, ORB features, and chroma thumbnail."""
    cv2 = _cv2()
    pixels, _kp, _desc, _mono, cbcr = _features(path, size, mtime_ns, ctime_ns, crop)
    if pixels is None:
        return None, [], None, None
    flipped = cv2.flip(pixels, 1)
    fh, fw = flipped.shape
    edge_threshold = min(31, max(7, min(fh, fw) // 4))
    kp_f, desc_f = cv2.ORB_create(
        nfeatures=1800, edgeThreshold=edge_threshold, patchSize=edge_threshold
    ).detectAndCompute(flipped, None)
    flipped_cbcr = cbcr[:, ::-1] if cbcr is not None else None
    return flipped, kp_f, desc_f, flipped_cbcr


@lru_cache(maxsize=128)
def _sift_features(
    path: str,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    crop: tuple[int, int, int, int] | None = None,
    flipped: bool = False,
):
    """Cached SIFT keypoints and descriptors keyed by file stat, crop, and flip."""
    cv2 = _cv2()
    if not hasattr(cv2, "SIFT_create"):
        return [], None
    if flipped:
        gray = _flipped_orb_features(path, size, mtime_ns, ctime_ns, crop)[0]
    else:
        gray = _features(path, size, mtime_ns, ctime_ns, crop)[0]
    if gray is None:
        return [], None
    return cv2.SIFT_create().detectAndCompute(gray, None)


def clear_edited_copy_cache() -> None:
    """Release cached frames and ORB/SIFT feature descriptors at scan boundaries."""
    _features.cache_clear()
    _flipped_orb_features.cache_clear()
    _sift_features.cache_clear()
    _color_frame.cache_clear()


@lru_cache(maxsize=4)
def _color_frame(
    path: str,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    crop: tuple[int, int, int, int] | None = None,
):
    """The RGB frame behind ``_features`` pixels, at the same size.

    Only a pair with one monochrome side reads it, so it is decoded lazily.
    """
    import numpy as np

    frame = _prepare_rgb_frame(path, crop)
    if frame is None:
        return None
    return np.asarray(frame)


def _color_frame_for(path: str, crop: tuple[int, int, int, int] | None = None):
    stat = Path(path).stat()
    return _color_frame(path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, crop)


def _is_monochrome(rgb) -> bool:
    import numpy as np

    channels = rgb.reshape(-1, 3)[::7].astype(np.int16)
    spread = channels.max(axis=1) - channels.min(axis=1)
    return float(spread.mean()) < MONOCHROME_SPREAD_MAX


def _match_tones(
    left_path,
    right_path,
    warped,
    right,
    transform,
    overlap,
    left_mono,
    right_mono,
    *,
    interp: int,
    flip: bool = False,
    crop_l: tuple[int, int, int, int] | None = None,
    crop_r: tuple[int, int, int, int] | None = None,
):
    """Align tones between the two sides without masking local subject edits.

    When one side is monochrome and the other is color, fits RGB channel weights
    to reproduce the monochrome mix, then fits a monotone tone curve. When both
    sides are color or both monochrome, fits one global monotone luminance curve
    from the darker image toward the brighter one so non-linear contrast, gamma,
    S-curve, and sepia edits match without hiding local edits.
    """
    import numpy as np

    cv2 = _cv2()
    height, width = right.shape
    if left_mono != right_mono:
        if right_mono:
            left_rgb = _color_frame_for(left_path, crop_l)
            if left_rgb is None or left_rgb.shape[:2] != _gray_shape(left_path, crop_l):
                return warped, right
            if flip:
                left_rgb = cv2.flip(left_rgb, 1)
            color = cv2.warpAffine(left_rgb, transform, (width, height), flags=interp)
            target = right
        else:
            color, target = _color_frame_for(right_path, crop_r), warped
            if color is None or color.shape[:2] != right.shape:
                return warped, right
        samples = color[overlap].astype(np.float64)
        design = np.column_stack([samples, np.ones(len(samples))])
        weights, *_ = np.linalg.lstsq(design, target[overlap].astype(np.float64), rcond=None)
        mixed = np.clip(color.astype(np.float64) @ weights[:3] + weights[3], 0, 255)
        mixed = _tone_curve(mixed.astype(np.uint8), target, overlap)
        return (mixed, right) if right_mono else (warped, mixed)

    if int(np.count_nonzero(overlap)) < TONE_CURVE_MIN_SAMPLES * 4:
        return warped, right
    if float(warped[overlap].mean()) <= float(right[overlap].mean()):
        return _tone_curve(warped, right, overlap), right
    return warped, _tone_curve(right, warped, overlap)


def _tone_curve(source, target, valid):
    """Map ``source`` levels onto ``target`` with one monotone curve.

    Black-and-white presets usually add contrast. A single increasing curve
    over brightness levels cannot move, add or remove anything in the frame.
    """
    import numpy as np

    # Both images contain 8-bit levels. A joint histogram gives the same
    # per-level medians without sorting every valid pixel.
    bins = source[valid].astype(np.intp) * 256 + target[valid]
    counts = np.bincount(bins, minlength=256 * 256).reshape(256, 256)
    totals = counts.sum(axis=1)
    known = totals >= TONE_CURVE_MIN_SAMPLES
    if known.sum() < 2:
        return source
    cumulative = counts[known].cumsum(axis=1)
    middle = totals[known, None] // 2
    lower = (cumulative > (totals[known, None] - 1) // 2).argmax(axis=1)
    upper = (cumulative > middle).argmax(axis=1)
    curve = np.interp(np.arange(256), np.flatnonzero(known), (lower + upper) / 2)
    curve = np.maximum.accumulate(curve)
    return np.clip(curve[source], 0, 255).astype(np.uint8)


def _gray_shape(path: str, crop: tuple[int, int, int, int] | None = None):
    stat = Path(path).stat()
    pixels = _features(path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, crop)[0]
    return None if pixels is None else pixels.shape


def edited_copy_verdict(
    left_path: str,
    right_path: str,
    *,
    crop_a: tuple[int, int, int, int] | None = None,
    crop_b: tuple[int, int, int, int] | None = None,
) -> bool | None:
    """True when either image, aligned onto the other, shows the same exposure.

    Warping resamples one side: enlarging a crop blurs it, while shrinking
    the larger image can alias fine texture. Either artifact can read as
    lost detail, so each direction is tried, and each applies the full
    fine-detail check. The verdict no longer depends on argument order.
    """
    verdicts = []
    first_verdict, matched_features = _aligned_verdict_with_match_info(
        left_path, right_path, crop_l=crop_a, crop_r=crop_b
    )
    if first_verdict is True:
        return True
    verdicts.append(first_verdict)
    # When raw ORB/SIFT matching failed to find 24 correspondences in both
    # upright and flipped orientations, reversing the pair cannot align it
    # either; short-circuit the redundant reverse pass.
    if not matched_features:
        return None
    second_verdict, _ = _aligned_verdict_with_match_info(
        right_path, left_path, crop_l=crop_b, crop_r=crop_a
    )
    if second_verdict is True:
        return True
    verdicts.append(second_verdict)
    return False if False in verdicts else None


def _estimate_affine_partial(cv2, points_a, points_b):
    if hasattr(cv2, "USAC_MAGSAC"):
        try:
            return cv2.estimateAffinePartial2D(
                points_a, points_b, method=cv2.USAC_MAGSAC, ransacReprojThreshold=2.5
            )
        except cv2.error:
            pass
    return cv2.estimateAffinePartial2D(
        points_a, points_b, method=cv2.RANSAC, ransacReprojThreshold=2.5
    )


def _match_descriptors(cv2, desc_a, desc_b, norm: int):
    if desc_a is None or desc_b is None or len(desc_a) < 2 or len(desc_b) < 2:
        return []
    pairs = cv2.BFMatcher(norm).knnMatch(desc_a, desc_b, k=2)
    return [
        pair[0]
        for pair in pairs
        if len(pair) == 2 and pair[0].distance < 0.7 * pair[1].distance
    ]


def _overlap_tile_mean(warped, right, overlap) -> float | None:
    import numpy as np

    a = warped[overlap].astype(float) / 255.0
    b = right[overlap].astype(float) / 255.0
    var_a = float(a.var())
    if a.std() < 0.04 or b.std() < 0.04 or var_a <= 1e-12:
        return None
    scale = float(np.clip(np.cov(a, b)[0, 1] / var_a, 0.4, 2.5))
    bias = float(b.mean() - scale * a.mean())
    return float(np.abs(a * scale + bias - b).mean())


def _chroma_agrees(cv2, left_cbcr, right_cbcr, transform, left_shape, right_shape) -> bool:
    import numpy as np

    if left_cbcr is None or right_cbcr is None:
        return True
    lh, lw = left_shape
    rh, rw = right_shape
    sx_up = lw / CHROMA_THUMB_SIZE
    sy_up = lh / CHROMA_THUMB_SIZE
    sx_dn = CHROMA_THUMB_SIZE / rw
    sy_dn = CHROMA_THUMB_SIZE / rh
    m = np.vstack([transform, [0.0, 0.0, 1.0]])
    m_thumb = np.diag([sx_dn, sy_dn, 1.0]) @ m @ np.diag([sx_up, sy_up, 1.0])
    warped = cv2.warpAffine(
        left_cbcr.astype(np.float32),
        m_thumb[:2],
        (CHROMA_THUMB_SIZE, CHROMA_THUMB_SIZE),
        flags=cv2.INTER_LINEAR,
    )
    overlap = (
        cv2.warpAffine(
            np.ones((CHROMA_THUMB_SIZE, CHROMA_THUMB_SIZE), dtype=np.uint8),
            m_thumb[:2],
            (CHROMA_THUMB_SIZE, CHROMA_THUMB_SIZE),
            flags=cv2.INTER_NEAREST,
        )
        > 0
    )
    diff = np.abs(warped - right_cbcr.astype(np.float32)).max(axis=2)
    ys = np.linspace(0, CHROMA_THUMB_SIZE, CHROMA_GRID + 1, dtype=int)
    xs = np.linspace(0, CHROMA_THUMB_SIZE, CHROMA_GRID + 1, dtype=int)
    block_means = []
    for r in range(CHROMA_GRID):
        for c in range(CHROMA_GRID):
            valid = overlap[ys[r] : ys[r + 1], xs[c] : xs[c + 1]]
            if valid.size and valid.mean() > 0.8:
                block_means.append(float(diff[ys[r] : ys[r + 1], xs[c] : xs[c + 1]][valid].mean()))
    if len(block_means) < 4:
        return True
    mean = sum(block_means) / len(block_means)
    worst = max(block_means)
    if worst > 64.0:
        return False
    return not (worst > 6.0 and mean > 0 and worst / mean > 8.0)


def _aligned_verdict(
    left_path: str,
    right_path: str,
    *,
    crop_l: tuple[int, int, int, int] | None = None,
    crop_r: tuple[int, int, int, int] | None = None,
) -> bool | None:
    return _aligned_verdict_with_match_info(
        left_path, right_path, crop_l=crop_l, crop_r=crop_r
    )[0]


def _aligned_verdict_with_match_info(
    left_path: str,
    right_path: str,
    *,
    crop_l: tuple[int, int, int, int] | None = None,
    crop_r: tuple[int, int, int, int] | None = None,
) -> tuple[bool | None, bool]:
    try:
        import numpy as np

        cv2 = _cv2()

        def stat_sig(path: str) -> tuple[str, int, int, int]:
            stat = Path(path).stat()
            return path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

        left_sig = stat_sig(left_path)
        right_sig = stat_sig(right_path)
        left, kp_a, desc_a, left_mono, left_cbcr = _features(*left_sig, crop_l)
        right, kp_b, desc_b, right_mono, right_cbcr = _features(*right_sig, crop_r)
        if left is None or right is None or min(*left.shape, *right.shape) < 64:
            return None, False

        had_enough_matches = False

        def align_once(
            src_gray,
            src_kp,
            src_desc,
            src_cbcr,
            *,
            flip: bool,
        ) -> bool | None:
            nonlocal had_enough_matches
            matches = _match_descriptors(cv2, src_desc, desc_b, cv2.NORM_HAMMING)
            pts_src_kp, pts_dst_kp = src_kp, kp_b
            if len(matches) < 24 and hasattr(cv2, "SIFT_create"):
                s_kp_a, s_desc_a = _sift_features(*left_sig, crop_l, flip)
                s_kp_b, s_desc_b = _sift_features(*right_sig, crop_r, False)
                sift_matches = _match_descriptors(cv2, s_desc_a, s_desc_b, cv2.NORM_L2)
                if len(sift_matches) >= 24:
                    matches = sift_matches
                    pts_src_kp, pts_dst_kp = s_kp_a, s_kp_b
            if len(matches) < 24:
                return None
            had_enough_matches = True

            points_a = np.float32([pts_src_kp[m.queryIdx].pt for m in matches])
            points_b = np.float32([pts_dst_kp[m.trainIdx].pt for m in matches])
            transform, inliers = _estimate_affine_partial(cv2, points_a, points_b)
            if transform is None or inliers is None:
                return None
            mask = inliers.ravel().astype(bool)
            inlier_count = int(mask.sum())
            inlier_ratio = float(mask.mean())
            if inlier_count < 24 or not (
                inlier_ratio >= 0.75 or (inlier_count >= 60 and inlier_ratio >= 0.45)
            ):
                return None
            scale = float(np.hypot(transform[0, 0], transform[1, 0]))
            if not (0.05 <= scale <= 20.0):
                return None

            height, width = right.shape
            overlap_raw = (
                cv2.warpAffine(
                    np.ones_like(src_gray), transform, (width, height), flags=cv2.INTER_NEAREST
                )
                > 0
            )
            overlap = (
                cv2.erode(
                    overlap_raw.astype("uint8"),
                    np.ones((7, 7), "uint8"),
                    borderType=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )
                > 0
            )
            if overlap.mean() < 0.25 or int(overlap.sum()) < 64 * 64:
                return None

            # Reject matches confined to a logo or small corner patch relative to
            # the actual warped overlap bounding box on each frame.
            ys_r, xs_r = np.nonzero(overlap_raw)
            bbox_area_r = max(
                1, (int(ys_r.max()) - int(ys_r.min()) + 1) * (int(xs_r.max()) - int(xs_r.min()) + 1)
            )
            hull_area_r = cv2.contourArea(cv2.convexHull(points_b[mask]))
            if hull_area_r / bbox_area_r < 0.2:
                return None

            inv = cv2.invertAffineTransform(transform)
            left_overlap = (
                cv2.warpAffine(
                    np.ones_like(right),
                    inv,
                    (src_gray.shape[1], src_gray.shape[0]),
                    flags=cv2.INTER_NEAREST,
                )
                > 0
            )
            ys_l, xs_l = np.nonzero(left_overlap)
            if not len(ys_l):
                return None
            bbox_area_l = max(
                1, (int(ys_l.max()) - int(ys_l.min()) + 1) * (int(xs_l.max()) - int(xs_l.min()) + 1)
            )
            hull_area_l = cv2.contourArea(cv2.convexHull(points_a[mask]))
            if hull_area_l / bbox_area_l < 0.2:
                return None

            interp = cv2.INTER_AREA if scale < 0.95 else cv2.INTER_LINEAR
            warped = cv2.warpAffine(src_gray, transform, (width, height), flags=interp)

            # Optional subpixel ECC refinement when it improves overlap alignment.
            if hasattr(cv2, "findTransformECC") and hasattr(cv2, "TERM_CRITERIA_EPS"):
                base_err = _overlap_tile_mean(warped, right, overlap)
                if base_err is not None and base_err < 0.08:
                    motion = (
                        cv2.MOTION_EUCLIDEAN
                        if 0.98 <= scale <= 1.02
                        else cv2.MOTION_AFFINE
                    )
                    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 15, 1e-3)
                    try:
                        _cc, ecc_warp = cv2.findTransformECC(
                            src_gray,
                            right,
                            transform.astype(np.float32).copy(),
                            motion,
                            criteria,
                            inputMask=overlap.astype("uint8"),
                            gaussFiltSize=5,
                        )
                        ecc_scale = float(np.hypot(ecc_warp[0, 0], ecc_warp[1, 0]))
                        if 0.9 <= ecc_scale / scale <= 1.1:
                            ecc_overlap_raw = (
                                cv2.warpAffine(
                                    np.ones_like(src_gray),
                                    ecc_warp,
                                    (width, height),
                                    flags=cv2.INTER_NEAREST,
                                )
                                > 0
                            )
                            ecc_overlap = (
                                cv2.erode(
                                    ecc_overlap_raw.astype("uint8"),
                                    np.ones((7, 7), "uint8"),
                                    borderType=cv2.BORDER_CONSTANT,
                                    borderValue=0,
                                )
                                > 0
                            )
                            if ecc_overlap.mean() >= 0.25 and int(ecc_overlap.sum()) >= 64 * 64:
                                ecc_interp = cv2.INTER_AREA if ecc_scale < 0.95 else cv2.INTER_LINEAR
                                ecc_warped = cv2.warpAffine(
                                    src_gray, ecc_warp, (width, height), flags=ecc_interp
                                )
                                ecc_err = _overlap_tile_mean(ecc_warped, right, ecc_overlap)
                                if ecc_err is not None and ecc_err < base_err:
                                    transform = ecc_warp.astype(np.float64)
                                    scale = ecc_scale
                                    interp = ecc_interp
                                    warped = ecc_warped
                                    overlap = ecc_overlap
                    except cv2.error:
                        pass

            warped_toned, right_toned = _match_tones(
                left_path,
                right_path,
                warped,
                right,
                transform,
                overlap,
                left_mono,
                right_mono,
                interp=interp,
                flip=flip,
                crop_l=crop_l,
                crop_r=crop_r,
            )

            # Fit linear brightness/contrast remainder, then inspect an exact 8x8 tile grid.
            a = warped_toned[overlap].astype(float) / 255
            b = right_toned[overlap].astype(float) / 255
            if a.std() < 0.04 or b.std() < 0.04:
                return None
            gain = float(np.clip(np.cov(a, b)[0, 1] / a.var(), 0.4, 2.5))
            bias = float(b.mean() - gain * a.mean())
            residual = np.abs((warped_toned.astype(float) / 255) * gain + bias - right_toned / 255)
            ys = np.linspace(0, height, 9, dtype=int)
            xs = np.linspace(0, width, 9, dtype=int)
            tile_errors = []
            for row in range(8):
                for col in range(8):
                    y0, y1 = int(ys[row]), int(ys[row + 1])
                    x0, x1 = int(xs[col]), int(xs[col + 1])
                    valid = overlap[y0:y1, x0:x1]
                    if valid.size and valid.mean() > 0.8:
                        tile_errors.append(float(residual[y0:y1, x0:x1][valid].mean()))
            if len(tile_errors) < 16:
                return None
            mean_tile = float(np.mean(tile_errors))
            med_tile = float(np.median(tile_errors))
            max_tile = float(max(tile_errors))
            confident = inlier_count >= 35 and inlier_ratio >= 0.60 and med_tile <= 0.030
            if max_tile > 0.075 or mean_tile > 0.035:
                if confident and med_tile <= 0.025 and max_tile > 0.075:
                    return False
                return None
            if not left_mono and not right_mono and not _chroma_agrees(
                cv2, src_cbcr, right_cbcr, transform, src_gray.shape, right.shape
            ):
                return False
            detail_left, detail_right = (
                (warped_toned, right_toned) if left_mono != right_mono else (warped, right)
            )
            if aligned_detail_matches(detail_left, detail_right, valid=overlap):
                return True
            return False if confident else None

        verdict = align_once(left, kp_a, desc_a, left_cbcr, flip=False)
        if verdict is not None:
            return verdict, True

        # Mirror/flip fallback: handles mirrored+cropped and mirrored+B&W copies.
        flipped, kp_f, desc_f, flipped_cbcr = _flipped_orb_features(*left_sig, crop_l)
        if flipped is None:
            return None, had_enough_matches
        return align_once(flipped, kp_f, desc_f, flipped_cbcr, flip=True), had_enough_matches
    except (ImportError, OSError, ValueError):
        return None, False
    except Exception:
        # Corrupt media and optional backend errors are an unverified candidate.
        return None, False


def verify_edited_copy(
    left_path: str,
    right_path: str,
    *,
    crop_a: tuple[int, int, int, int] | None = None,
    crop_b: tuple[int, int, int, int] | None = None,
) -> bool:
    return edited_copy_verdict(left_path, right_path, crop_a=crop_a, crop_b=crop_b) is True
