"""Fine local evidence that two aligned images depict the same exposure.

This is deliberately independent of semantic similarity and face detection:
small changes matter wherever they occur, including hair, hands and backgrounds.
"""
from __future__ import annotations

# Local contrast normalization is the structural term of SSIM. Its stabilizer
# is (0.03 * 255)^2, which prevents JPEG noise in flat regions dominating it.
STRUCTURE_STABILIZER = (0.03 * 255) ** 2
STRUCTURE_ERROR_MAX = 0.25
LOCAL_RESIDUAL_MAX = 4.0
DETAIL_WINDOWS = (7, 15, 31)
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

    def matrix(parameters, shape):
        angle, dy, dx = parameters
        angle = np.deg2rad(angle)
        rotation = np.array([[np.cos(angle), -np.sin(angle)],
                             [np.sin(angle), np.cos(angle)]])
        center = (np.array(shape) - 1) / 2
        return rotation, center - rotation @ center + np.array([dy, dx])

    def loss(parameters):
        rotation, offset = matrix(parameters, a.shape)
        moved = affine_transform(a, rotation, offset, order=1, mode="nearest")
        difference = np.abs(moved[mask] - b[mask])
        # A changed mouth or hand must not steer the global alignment.
        return float(np.minimum(difference, 12).mean())

    initial = loss((0, 0, 0))
    if initial < 1.0:
        return left, valid
    result = minimize(loss, (0, 0, 0), method="Powell",
                      bounds=((-1, 1), (-1.5, 1.5), (-1.5, 1.5)),
                      options={"maxiter": 12, "xtol": .01, "ftol": .002})
    if result.fun >= initial * .85:
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


def aligned_detail_matches(left, right, *, valid=None) -> bool:
    """Compare same-sized grayscale arrays (0–255), optionally within an overlap.

    Global exposure and a tiny rigid registration accommodate ordinary exports.
    Overlapping windows retain local evidence even on grid boundaries. Both a
    structural change and appreciable pixel residual are required, so JPEG
    noise in flat areas is not mistaken for a new expression.
    """
    import numpy as np
    from scipy.ndimage import gaussian_filter, minimum_filter, uniform_filter

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
    a = _exposure_match(a, b, mask)
    a, mask = _register(a, b, mask)
    if not mask.any():
        return False
    a, b = gaussian_filter(a, .8), gaussian_filter(b, .8)
    difference = np.abs(a - b)
    if float(difference[mask].max()) <= LOCAL_RESIDUAL_MAX:
        return True
    checked = False
    for window in DETAIL_WINDOWS:
        if min(a.shape) < window:
            continue
        support = minimum_filter(mask, size=window, mode="constant", cval=0)
        if not support.any():
            continue
        checked = True
        mean_a, mean_b = uniform_filter(a, window), uniform_filter(b, window)
        variance_a = np.maximum(0, uniform_filter(a * a, window) - mean_a * mean_a)
        variance_b = np.maximum(0, uniform_filter(b * b, window) - mean_b * mean_b)
        covariance = uniform_filter(a * b, window) - mean_a * mean_b
        structural_error = np.maximum(0, variance_a + variance_b - 2 * covariance)
        structural_error /= variance_a + variance_b + STRUCTURE_STABILIZER
        residual = uniform_filter(difference, window)
        changed = support & (structural_error > STRUCTURE_ERROR_MAX) & (residual > LOCAL_RESIDUAL_MAX)
        if np.count_nonzero(changed) >= MIN_CHANGED_WINDOWS:
            return False
    return checked
