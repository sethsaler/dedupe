"""Small visible subject changes must survive duplicate matching.

These deterministic edits to a public-domain photograph are regression fixtures,
not a replacement for evaluating genuine portrait/burst pairs from a library.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageEnhance

from dedupe.cache import HashCache
from dedupe.scanner import inventory
from dedupe.similar_image import find_similar_image_groups, is_dense_match

FIXTURE = Path(__file__).parent / "fixtures" / "astronaut.png"
DETAIL_CHANGES = ("smile", "hair", "eyes", "pose")


def _detail_pair(tmp_path: Path, change: str, brightness: float = 1.0, mirrored=False):
    with Image.open(FIXTURE) as image:
        base = image.convert("RGB")
    changed = base.copy()
    if change == "smile":
        # Cover the narrow visible band of teeth, preserving the rest of the face.
        ImageDraw.Draw(changed).line(
            [(204, 145), (213, 147), (225, 147), (239, 144)],
            fill=(110, 66, 56), width=3,
        )
    else:
        box, offset = {
            "hair": ((180, 37, 200, 63), (2, 0)),
            "eyes": ((183, 91, 266, 115), (2, 0)),
            "pose": ((178, 79, 270, 171), (1, 1)),
        }[change]
        changed.paste(base.crop(box), (box[0] + offset[0], box[1] + offset[1]))
    changed = ImageEnhance.Brightness(changed).enhance(brightness)
    if mirrored:
        changed = changed.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    original, variant = tmp_path / "original.jpg", tmp_path / "changed.jpg"
    base.save(original, quality=92)
    changed.save(variant, quality=75)
    return original, variant


@pytest.mark.parametrize("change", DETAIL_CHANGES)
@pytest.mark.parametrize("brightness", [1.0, 1.08])
def test_detail_verification_rejects_small_subject_changes(tmp_path, change, brightness):
    original, changed = _detail_pair(tmp_path, change, brightness)

    assert not is_dense_match(str(original), str(changed)), (change, brightness)
    assert not is_dense_match(str(changed), str(original)), (change, brightness)


@pytest.mark.parametrize("change", DETAIL_CHANGES)
@pytest.mark.parametrize("brightness", [1.0, 1.08])
@pytest.mark.parametrize("mirrored", [False, True])
def test_subject_changes_stay_distinct_after_cache_and_orientation(
    tmp_path, change, brightness, mirrored
):
    original, changed = _detail_pair(tmp_path, change, brightness, mirrored)
    cache = HashCache(tmp_path / "hashes.sqlite3")
    try:
        for cached in (False, True):
            records = inventory([original, changed])
            if cached:
                assert cache.hydrate(records) == 2
                records.reverse()
            assert find_similar_image_groups(records, workers=1) == [], (
                change, brightness, mirrored, cached
            )
            cache.store_all(records)
    finally:
        cache.close()


@pytest.mark.parametrize("size,quality", [(512, 60), (512, 75), (256, 75), (128, 75)])
@pytest.mark.parametrize("brightness", [0.8, 1.0, 1.08, 1.2])
def test_detail_verification_preserves_actual_reexports(tmp_path, size, quality, brightness):
    with Image.open(FIXTURE) as image:
        base = image.convert("RGB")
    original, exported = tmp_path / "original.jpg", tmp_path / "exported.jpg"
    base.save(original, quality=92)
    ImageEnhance.Brightness(base).enhance(brightness).resize(
        (size, size), Image.Resampling.LANCZOS
    ).save(exported, quality=quality)

    assert is_dense_match(str(original), str(exported))
    assert is_dense_match(str(exported), str(original))
    groups = find_similar_image_groups(inventory([original, exported]), workers=1)
    assert [{record.path for record in group} for group in groups] == [
        {str(original), str(exported)}
    ]


@pytest.mark.parametrize("shape", [(7, 7), (15, 16), (33, 90), (120, 160)])
def test_whole_frame_support_matches_the_mask_filter(shape):
    """The whole-frame shortcut marks the pixels the mask filter would."""
    import numpy as np

    from dedupe.image_detail import DETAIL_WINDOWS, _window_support

    mask = np.ones(shape, dtype=bool)
    for window in DETAIL_WINDOWS:
        if min(shape) < window:
            continue
        filtered = _window_support(mask, window, False)
        assert np.array_equal(_window_support(mask, window, True), filtered)
        assert filtered.any() == (min(shape) >= window)


@pytest.mark.parametrize(
    "kind",
    [
        "jpeg-20",
        "webp-30",
        "gif-256",
        "unsharp",
        "blur-1",
        "median-3",
        "contrast-135",
        "gamma-07",
        "gamma-15",
        "bright-065",
        "bright-135",
        "rotate-15",
        "scurve",
    ],
)
def test_benign_global_edits_pass_aligned_detail_matches(tmp_path: Path, kind: str) -> None:
    """Global compression, filtering, tone curves, and 1.5deg straightening pass fine detail."""
    import numpy as np
    from PIL import ImageFilter

    from dedupe.image_detail import aligned_detail_matches

    with Image.open(FIXTURE) as source:
        base = source.convert("RGB")
    base_gray = np.asarray(base.convert("L"))
    arr = np.asarray(base, dtype=np.float32) / 255.0
    out_path = tmp_path / f"{kind}.png"
    if kind == "jpeg-20":
        out_path = tmp_path / "out.jpg"
        base.save(out_path, quality=20)
    elif kind == "webp-30":
        out_path = tmp_path / "out.webp"
        base.save(out_path, quality=30)
    elif kind == "gif-256":
        out_path = tmp_path / "out.gif"
        base.convert("P", palette=Image.Palette.ADAPTIVE, colors=256).save(out_path)
    elif kind == "unsharp":
        base.filter(ImageFilter.UnsharpMask(radius=1, percent=100)).save(out_path)
    elif kind == "blur-1":
        base.filter(ImageFilter.GaussianBlur(radius=1.0)).save(out_path)
    elif kind == "median-3":
        base.filter(ImageFilter.MedianFilter(size=3)).save(out_path)
    elif kind == "contrast-135":
        ImageEnhance.Contrast(base).enhance(1.35).save(out_path)
    elif kind == "gamma-07":
        Image.fromarray(np.clip((arr**0.7) * 255, 0, 255).astype(np.uint8)).save(out_path)
    elif kind == "gamma-15":
        Image.fromarray(np.clip((arr**1.5) * 255, 0, 255).astype(np.uint8)).save(out_path)
    elif kind == "bright-065":
        ImageEnhance.Brightness(base).enhance(0.65).save(out_path)
    elif kind == "bright-135":
        ImageEnhance.Brightness(base).enhance(1.35).save(out_path)
    elif kind == "rotate-15":
        base.rotate(1.5, resample=Image.Resampling.BICUBIC).save(out_path)
    elif kind == "scurve":
        scurve = np.clip(3.0 * arr**2 - 2.0 * arr**3, 0.0, 1.0)
        Image.fromarray((scurve * 255).astype(np.uint8)).save(out_path)

    with Image.open(out_path) as encoded:
        variant_gray = np.asarray(encoded.convert("L"))
    assert aligned_detail_matches(base_gray, variant_gray), kind
    assert aligned_detail_matches(variant_gray, base_gray), kind


@pytest.mark.parametrize("size", [256, 192])
@pytest.mark.parametrize("change", DETAIL_CHANGES)
@pytest.mark.parametrize("brightness", [1.0, 1.08])
def test_downscaled_thumbnail_regressions_strictly_rejected(
    tmp_path: Path, size: int, change: str, brightness: float
) -> None:
    """Subject edits remain rejected even on 192px and 256px thumbnails."""
    import numpy as np

    from dedupe.image_detail import aligned_detail_matches

    original, changed = _detail_pair(tmp_path, change, brightness)
    for p in (original, changed):
        with Image.open(p) as im:
            im.resize((size, size), Image.Resampling.LANCZOS).save(p, quality=90)
    with Image.open(original) as im_a, Image.open(changed) as im_b:
        gray_a = np.asarray(im_a.convert("L"))
        gray_b = np.asarray(im_b.convert("L"))

    assert not aligned_detail_matches(gray_a, gray_b), (size, change, brightness)
    assert not is_dense_match(str(original), str(changed)), (size, change, brightness)
