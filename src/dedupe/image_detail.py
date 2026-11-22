"""Fine local evidence that two aligned images depict the same exposure.

This is deliberately independent of semantic similarity and face detection:
small changes matter wherever they occur, including hair, hands and backgrounds.
"""
from __future__ import annotations

# Local contrast normalization is the structural term of SSIM. Its stabilizer
# is (0.03 * 255)^2, which prevents JPEG noise in flat regions dominating it.
STRUCTURE_STABILIZER = (0.03 * 255) ** 2
STRUCTURE_ERROR_MAX = 0.25
SMALL_STRUCTURE_ERROR_MAX = 0.15
TINY_STRUCTURE_ERROR_MAX = 0.12
CONTRAST_COLLAPSE_MAX = 0.50
LOCAL_RESIDUAL_MAX = 4.0
RESIDUAL_FLOOR_CAP = 14.0
SOLID_RESIDUAL_MIN = 24.0
SOLID_RESIDUAL_CAP = 40.0
DETAIL_WINDOWS = (7, 15, 31)
SMALL_DETAIL_WINDOWS = (5, 7, 15)
SMALL_THUMBNAIL_SIDE = 256
TINY_THUMBNAIL_SIDE = 192
MIN_CHANGED_WINDOWS = 4


def _exposure_match(left, right, valid):
    import numpy as np

    a, b = left[valid][::4], right[valid][::4]
    if not len(a):
        return left
    keep = (a > 10) & (a < 245) & (b > 10) & (b < 245)
    scale, bias = 1.0, float(np.median(b - a))
    for _ in range(3):
        if keep.sum() < 32 or np.std(a[keep]) < 5:
            break
        x, y = a[keep], b[keep]
        scale = float(np.clip(np.mean((x - x.mean()) * (y - y.mean())) / x.var(), .5, 2))
        bias = float(np.median(y - scale * x))
        residual = np.abs(a * scale + bias - b)
        keep &= residual <= max(2.0, float(np.percentile(residual[keep], 85)))
    return np.clip(left * scale + bias, 0, 255)


def _tone_curve_match(left, right, valid):
    """Map ``left`` intensities onto ``right`` with a monotone curve.

    Accommodates non-linear tone edits (gamma, S-curves, strong contrast) on
    sufficiently large overlaps while falling back to linear exposure matching
    on small masks or low-dynamic-range inputs.
    """
    import numpy as np

    valid_count = int(np.count_nonzero(valid))
    if valid_count < 1024:
        return _exposure_match(left, right, valid)
    source_levels = np.clip(np.rint(left[valid]), 0, 255).astype(np.intp)
    target_levels = np.clip(np.rint(right[valid]), 0, 255).astype(np.intp)
    counts = np.bincount(source_levels * 256 + target_levels, minlength=256 * 256).reshape(256, 256)
    totals = counts.sum(axis=1)
    min_samples = max(4, min(20, valid_count // 1024))
    known = totals >= min_samples
    if int(known.sum()) < 8:
        return _exposure_match(left, right, valid)
    cumulative = counts[known].cumsum(axis=1)
    middle = totals[known, None] // 2
    lower = (cumulative > (totals[known, None] - 1) // 2).argmax(axis=1)
    upper = (cumulative > middle).argmax(axis=1)
    curve = np.interp(np.arange(256), np.flatnonzero(known), (lower + upper) / 2)
    curve = np.maximum.accumulate(curve)
    clipped = np.clip(left, 0, 255)
    idx0 = np.floor(clipped).astype(np.intp)
    idx1 = np.minimum(255, idx0 + 1)
    frac = clipped - idx0
    return (curve[idx0] * (1.0 - frac) + curve[idx1] * frac).astype(np.float32)


def _register(left, right, valid):
    """Fit only a small whole-frame turn/translation, never local subject motion."""
    import numpy as np
    from scipy.ndimage import affine_transform, gaussian_filter, zoom
    from scipy.optimize import minimize

    factor = min(1.0, 128 / max(left.shape))
    a = zoom(gaussian_filter(left, .8 / factor), factor, order=1)
    b = zoom(gaussian_filter(right, .8 / factor), factor, order=1)
    mask = zoom(valid.astype(float), factor, order=0) > .99
    mask[:3] = mask[-3:] = False
    mask[:, :3] = mask[:, -3:] = False
    if mask.sum() < 64:
        return left, valid
    # Normalize exposure on the proxy frames so brightness/gamma edits do not
    # saturate the clipped residual loss before spatial alignment runs.
    a_proxy = _exposure_match(a, b, mask)

    def matrix(parameters, shape):
        angle, dy, dx = parameters
        angle = np.deg2rad(angle)
        rotation = np.array([[np.cos(angle), -np.sin(angle)],
                             [np.sin(angle), np.cos(angle)]])
        center = (np.array(shape) - 1) / 2
        return rotation, center - rotation @ center + np.array([dy, dx])

    def loss(parameters):
        rotation, offset = matrix(parameters, a.shape)
        moved = affine_transform(a_proxy, rotation, offset, order=1, mode="nearest")
        difference = np.abs(moved[mask] - b[mask])
        # A changed mouth or hand must not steer the global alignment.
        return float(np.minimum(difference, 12).mean())

    initial = loss((0, 0, 0))
    if initial < 1.0:
        return left, valid
    result = minimize(loss, (0, 0, 0), method="Powell",
                      bounds=((-3, 3), (-2.0, 2.0), (-2.0, 2.0)),
                      options={"maxiter": 16, "xtol": .01, "ftol": .002})
    if result.fun >= initial * .95:
        return left, valid
    angle, dy, dx = result.x
    rotation, offset = matrix((angle, dy / factor, dx / factor), left.shape)
    moved = affine_transform(left, rotation, offset, order=1, mode="nearest")
    # Rotated exports can contain a narrow filled border in either image.
    # Exclude that uncertain edge, including the smoothing kernel's reach.
    border = int(np.ceil(abs(np.deg2rad(angle)) * max(left.shape) / 2
                         + max(abs(dy), abs(dx)) / factor)) + 3
    interior = valid.copy()
    interior[:border] = interior[-border:] = False
    interior[:, :border] = interior[:, -border:] = False
    support = affine_transform(interior.astype(float), rotation, offset, order=1,
                               mode="constant", cval=0) > .999
    return moved, interior & support


def _window_support(mask, window: int, whole_frame: bool):
    """Pixels whose whole ``window`` neighborhood is valid and inside the frame."""
    import numpy as np
    from scipy.ndimage import minimum_filter

    if whole_frame and window % 2:
        # Every pixel is valid, so a window has full support exactly when it
        # lies inside the frame; no filter pass is needed.
        edge = window // 2
        support = np.zeros(mask.shape, dtype=bool)
        support[edge : mask.shape[0] - edge, edge : mask.shape[1] - edge] = True
        return support
    return minimum_filter(mask, size=window, mode="constant", cval=0)


def _min_component_area(window: int, valid_area: int, small: bool) -> int:
    if small and window == 5:
        return max(5, min(10, int(valid_area * 0.00022)))
    if small and window == 7:
        return max(6, min(12, int(valid_area * 0.00022)))
    if window == 7:
        return max(32, int(valid_area * 0.00012))
    return max(16, int(valid_area * 0.00015))


def _max_component_size(mask) -> int:
    import numpy as np
    from scipy.ndimage import label

    labels, n_components = label(mask)
    if not n_components:
        return 0
    return int(np.bincount(labels.ravel())[1:].max())


def aligned_detail_matches(left, right, *, valid=None) -> bool:
    """Compare same-sized grayscale arrays (0–255), optionally within an overlap.

    Small rigid registration runs before monotone tone matching so straightening
    and non-linear contrast/gamma edits align cleanly. Pure SSIM structure with
    a contrast-collapse guard and connected-component area checks reject local
    subject edits while tolerating global compression and filtering.
    """
    import numpy as np
    from scipy.ndimage import gaussian_filter, uniform_filter

    a, b = np.asarray(left, dtype=np.float32), np.asarray(right, dtype=np.float32)
    if a.ndim != 2 or a.shape != b.shape or not a.size:
        return False
    mask = np.ones(a.shape, dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    if mask.shape != a.shape or not mask.any() or not np.isfinite(a).all() or not np.isfinite(b).all():
        return False
    if np.array_equal(a, b):
        return True
    # Fit toward the brighter image: highlight clipping is reproducible in
    # this direction but cannot be undone in the reverse direction.
    if float(a[mask].mean()) > float(b[mask].mean()):
        a, b = b, a
    a, mask = _register(a, b, mask)
    if not mask.any():
        return False
    a = _tone_curve_match(a, b, mask)
    min_dim = min(a.shape)
    small = min_dim <= SMALL_THUMBNAIL_SIDE
    sigma = max(0.35, 0.8 * min_dim / SMALL_THUMBNAIL_SIDE) if min_dim < SMALL_THUMBNAIL_SIDE else 0.8
    a, b = gaussian_filter(a, sigma), gaussian_filter(b, sigma)
    difference = np.abs(a - b)
    if float(difference[mask].max()) <= LOCAL_RESIDUAL_MAX:
        return True
    p75 = float(np.percentile(difference[mask], 75))
    residual_floor = max(LOCAL_RESIDUAL_MAX, min(RESIDUAL_FLOOR_CAP, 3.0 * p75))
    solid_floor = max(SOLID_RESIDUAL_MIN, min(SOLID_RESIDUAL_CAP, 4.0 * p75))
    checked = False
    whole_frame = bool(mask.all())
    valid_area = int(np.count_nonzero(mask))
    if small:
        windows = SMALL_DETAIL_WINDOWS
        scale = min(1.0, min_dim / SMALL_THUMBNAIL_SIDE)
        stabilizer = STRUCTURE_STABILIZER * (scale * scale) * 0.5
        se_max = TINY_STRUCTURE_ERROR_MAX if min_dim <= TINY_THUMBNAIL_SIDE else SMALL_STRUCTURE_ERROR_MAX
    else:
        windows = DETAIL_WINDOWS
        stabilizer = STRUCTURE_STABILIZER
        se_max = STRUCTURE_ERROR_MAX
    for window in windows:
        if min_dim < window:
            continue
        support = _window_support(mask, window, whole_frame)
        if not support.any():
            continue
        checked = True
        local_diff = uniform_filter(difference, window)
        min_cc = _min_component_area(window, valid_area, small)
        high_residual = support & (local_diff > residual_floor)
        if np.count_nonzero(high_residual) < min_cc:
            continue
        solid_min = max(9 if small else 16, min_cc // 2)
        solid = support & (local_diff > solid_floor)
        if np.count_nonzero(solid) >= solid_min and _max_component_size(solid) >= solid_min:
            return False
        mean_a, mean_b = uniform_filter(a, window), uniform_filter(b, window)
        variance_a = np.maximum(0, uniform_filter(a * a, window) - mean_a * mean_a)
        variance_b = np.maximum(0, uniform_filter(b * b, window) - mean_b * mean_b)
        covariance = uniform_filter(a * b, window) - mean_a * mean_b
        denom = 2.0 * np.sqrt(variance_a * variance_b) + stabilizer
        structural_error = 1.0 - np.clip((2.0 * covariance + stabilizer) / denom, 0.0, 1.0)
        contrast_collapse = 1.0 - denom / (variance_a + variance_b + stabilizer)
        changed = high_residual & (
            (structural_error > se_max) | (contrast_collapse > CONTRAST_COLLAPSE_MAX)
        )
        if np.count_nonzero(changed) >= min_cc and _max_component_size(changed) >= min_cc:
            return False
    return checked
