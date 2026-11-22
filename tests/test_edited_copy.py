"""Fine local changes also veto the semantic geometric verification path."""
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from dedupe.edited_copy import edited_copy_verdict, is_aligned_image_copy
from dedupe.similar_image import clear_image_comparison_cache

FIXTURE = Path(__file__).parent / "fixtures" / "astronaut.png"


def test_tone_curve_fits_exact_medians_with_masked_pixels_and_missing_levels():
    import numpy as np

    from dedupe.edited_copy import _tone_curve

    # Level 10 has an even sample count and a half-integer median. Level 30
    # has an odd sample count after masking. Level 20 has too few samples to fit.
    source = np.repeat([10, 20, 30], [20, 2, 22]).astype(np.uint8)
    target = np.array([12] * 10 + [15] * 10 + [255] * 2 + [50] * 11 + [80] * 11, dtype=np.uint8)
    valid = np.ones_like(source, dtype=bool)
    valid[22] = False
    result = _tone_curve(source, target, valid)
    np.testing.assert_array_equal(result, np.repeat([13, 46, 80], [20, 2, 22]))


@pytest.mark.parametrize("change", ["smile", "hair", "pose"])
@pytest.mark.parametrize("brightness", [1.0, 1.08])
def test_geometry_cannot_hide_small_local_changes(tmp_path, change, brightness):
    pytest.importorskip("cv2")
    with Image.open(FIXTURE) as source:
        image = source.convert("RGB")
    changed = image.copy()
    if change == "smile":
        ImageDraw.Draw(changed).line(
            [(204, 145), (213, 147), (225, 147), (239, 144)],
            fill=(110, 66, 56), width=3,
        )
    else:
        box, offset = {
            "hair": ((180, 37, 200, 63), (2, 0)),
            "pose": ((178, 79, 270, 171), (1, 1)),
        }[change]
        changed.paste(image.crop(box), (box[0] + offset[0], box[1] + offset[1]))
    path = tmp_path / "changed.jpg"
    ImageEnhance.Brightness(changed).enhance(brightness).save(path, quality=75)
    assert edited_copy_verdict(str(FIXTURE), str(path)) is False
    assert edited_copy_verdict(str(path), str(FIXTURE)) is False


@pytest.mark.parametrize("variant", ["recompress", "crop", "filter"])
def test_geometry_preserves_actual_edits(tmp_path, variant):
    pytest.importorskip("cv2")
    with Image.open(FIXTURE) as source:
        image = source.convert("RGB")
    if variant != "recompress":
        image = image.crop((64, 64, 448, 448))
    if variant == "filter":
        image = ImageEnhance.Brightness(ImageOps.grayscale(image)).enhance(1.1)
    path = tmp_path / "copy.jpg"
    image.save(path, quality=75)
    assert edited_copy_verdict(str(FIXTURE), str(path)) is True
    assert edited_copy_verdict(str(path), str(FIXTURE)) is True


def test_featureless_fallback_checks_turns_and_fails_closed(tmp_path):
    image = Image.new("RGB", (64, 96), "navy")
    ImageDraw.Draw(image).rectangle((6, 12, 34, 60), fill="gold")
    original, turned = tmp_path / "original.png", tmp_path / "turned.png"
    image.save(original)
    image.transpose(Image.Transpose.ROTATE_90).save(turned)
    clear_image_comparison_cache()
    try:
        assert is_aligned_image_copy(str(original), str(turned))
        turned.unlink()
        assert not is_aligned_image_copy(str(original), str(turned))
    finally:
        clear_image_comparison_cache()


def test_feature_cache_not_reused_after_edit_with_preserved_size_and_mtime(tmp_path):
    pytest.importorskip("cv2")
    with Image.open(FIXTURE) as source:
        image = source.convert("RGB")
    original, edited = tmp_path / "original.png", tmp_path / "edited.png"
    # Uncompressed PNG keeps the byte length constant when pixel values change.
    image.save(original, compress_level=0)
    image.save(edited, compress_level=0)
    before = edited.stat()
    assert edited_copy_verdict(str(original), str(edited)) is True

    ImageDraw.Draw(image).line(
        [(204, 145), (213, 147), (225, 147), (239, 144)],
        fill=(110, 66, 56), width=3,
    )
    image.save(edited, compress_level=0)
    os.utime(edited, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert edited.stat().st_size == before.st_size
    assert edited.stat().st_mtime_ns == before.st_mtime_ns
    assert edited_copy_verdict(str(original), str(edited)) is False


def _black_and_white(image, weights, gamma=1.0):
    import numpy as np

    mixed = np.asarray(image, dtype=float) @ np.array(weights)
    toned = 255 * (np.clip(mixed, 0, 255) / 255) ** gamma
    return Image.fromarray(toned.astype("uint8")).convert("RGB")


@pytest.mark.parametrize(
    "weights,gamma",
    [((0.299, 0.587, 0.114), 1.0), ((0.8, 0.2, 0.0), 1.0), ((0.1, 0.3, 0.6), 1.4)],
    ids=["standard", "red-filter", "blue-filter-with-contrast"],
)
def test_black_and_white_conversion_matches_its_color_photo(tmp_path, weights, gamma):
    pytest.importorskip("cv2")
    with Image.open(FIXTURE) as source:
        image = source.convert("RGB")
    color, mono = tmp_path / "color.jpg", tmp_path / "mono.jpg"
    image.save(color, quality=92)
    _black_and_white(image.crop((10, 10, 502, 502)), weights, gamma).save(mono, quality=88)
    clear_image_comparison_cache()
    assert edited_copy_verdict(str(color), str(mono)) is True
    assert edited_copy_verdict(str(mono), str(color)) is True


def test_black_and_white_channel_fit_cannot_hide_a_local_change(tmp_path):
    pytest.importorskip("cv2")
    with Image.open(FIXTURE) as source:
        image = source.convert("RGB")
    color, mono = tmp_path / "color.jpg", tmp_path / "mono.jpg"
    image.save(color, quality=92)
    changed = image.crop((10, 10, 502, 502))
    ImageDraw.Draw(changed).line(
        [(194, 135), (203, 137), (215, 137), (229, 134)], fill=(110, 66, 56), width=3,
    )
    _black_and_white(changed, (0.8, 0.2, 0.0)).save(mono, quality=88)
    clear_image_comparison_cache()
    assert edited_copy_verdict(str(color), str(mono)) is False
    assert edited_copy_verdict(str(mono), str(color)) is False


def test_cv2_set_num_threads_called_once(monkeypatch) -> None:
    """OpenCV thread cap is applied once per module identity and guarded cleanly."""
    import sys
    import types

    import dedupe.edited_copy as ec

    calls: list[int] = []
    fake_with = types.SimpleNamespace(setNumThreads=lambda n: calls.append(n))
    monkeypatch.setitem(sys.modules, "cv2", fake_with)
    monkeypatch.setattr(ec, "_CV2_CONFIGURED", None)
    assert ec._cv2() is fake_with
    assert ec._cv2() is fake_with
    assert calls == [1]

    fake_without = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "cv2", fake_without)
    assert ec._cv2() is fake_without


def test_sift_fallback_on_low_orb_matches(tmp_path: Path, monkeypatch) -> None:
    """When ORB yields < 24 matches (e.g., blurred bokeh), SIFT alignment succeeds."""
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "SIFT_create"):
        pytest.skip("OpenCV SIFT_create unavailable")
    from PIL import ImageFilter

    import dedupe.edited_copy as ec

    with Image.open(FIXTURE) as source:
        base = source.convert("RGB").filter(ImageFilter.GaussianBlur(radius=1.5))
    cropped = base.crop((40, 40, 472, 472))
    a, b = tmp_path / "bokeh_a.png", tmp_path / "bokeh_b.png"
    base.save(a)
    cropped.save(b)

    real_match = ec._match_descriptors

    def orb_starved_matcher(mod, desc_a, desc_b, norm):
        if norm == mod.NORM_HAMMING:
            return []
        return real_match(mod, desc_a, desc_b, norm)

    monkeypatch.setattr(ec, "_match_descriptors", orb_starved_matcher)
    clear_image_comparison_cache()
    assert edited_copy_verdict(str(a), str(b)) is True


def test_mirror_crop_and_mirror_bw_alignment(tmp_path: Path) -> None:
    """Horizontal flip fallback aligns mirrored+cropped and mirrored+B&W copies."""
    pytest.importorskip("cv2")
    with Image.open(FIXTURE) as source:
        base = source.convert("RGB")
    orig = tmp_path / "orig.png"
    base.save(orig)

    mirrored_crop = tmp_path / "mirror_crop.png"
    base.transpose(Image.Transpose.FLIP_LEFT_RIGHT).crop((48, 48, 464, 464)).save(mirrored_crop)

    mirrored_bw = tmp_path / "mirror_bw.png"
    _black_and_white(
        base.transpose(Image.Transpose.FLIP_LEFT_RIGHT).crop((24, 24, 488, 488)),
        (0.299, 0.587, 0.114),
    ).save(mirrored_bw)

    clear_image_comparison_cache()
    assert edited_copy_verdict(str(orig), str(mirrored_crop)) is True
    assert edited_copy_verdict(str(mirrored_crop), str(orig)) is True
    assert edited_copy_verdict(str(orig), str(mirrored_bw)) is True
    assert edited_copy_verdict(str(mirrored_bw), str(orig)) is True


def test_tight_crop_and_inset_phone_screenshot_match(tmp_path: Path) -> None:
    """55% crops and inset phone screenshots (with and without explicit crops) match."""
    pytest.importorskip("cv2")
    with Image.open(FIXTURE) as source:
        base = source.convert("RGB")
    orig = tmp_path / "orig.png"
    base.save(orig)

    # 55% center portrait crop (~380x380 -> 30% area of 512x512).
    tight = tmp_path / "tight_crop.png"
    base.crop((96, 60, 416, 440)).save(tight)

    # 384x384 inset on a 600x1000 phone bezel canvas.
    shot = tmp_path / "phone_shot.png"
    canvas = Image.new("RGB", (600, 1000), (16, 18, 22))
    canvas.paste(base.resize((384, 384), Image.Resampling.LANCZOS), (108, 308))
    canvas.save(shot)

    clear_image_comparison_cache()
    assert edited_copy_verdict(str(orig), str(tight)) is True
    assert edited_copy_verdict(str(orig), str(shot)) is True
    assert edited_copy_verdict(str(shot), str(orig)) is True
    assert edited_copy_verdict(str(orig), str(shot), crop_b=(108, 308, 492, 692)) is True


def test_color_to_color_tone_curve_matches_scurve_and_rejects_local_edit(tmp_path: Path) -> None:
    """Monotone color tone curves match S-curve+crop while still rejecting local edits."""
    pytest.importorskip("cv2")
    import numpy as np

    with Image.open(FIXTURE) as source:
        base = source.convert("RGB")
    orig = tmp_path / "orig.png"
    base.save(orig)

    cropped = base.crop((24, 24, 488, 488))
    arr = np.asarray(cropped, dtype=np.float32) / 255.0
    scurve = Image.fromarray((np.clip(3 * arr**2 - 2 * arr**3, 0, 1) * 255).astype(np.uint8))
    sc_path = tmp_path / "scurve_crop.png"
    scurve.save(sc_path)

    edited_local = cropped.copy()
    ImageDraw.Draw(edited_local).line(
        [(180, 121), (189, 123), (201, 123), (215, 120)],
        fill=(110, 66, 56),
        width=3,
    )
    arr_e = np.asarray(edited_local, dtype=np.float32) / 255.0
    sc_edited = Image.fromarray((np.clip(3 * arr_e**2 - 2 * arr_e**3, 0, 1) * 255).astype(np.uint8))
    bad_path = tmp_path / "scurve_local_edit.png"
    sc_edited.save(bad_path)

    clear_image_comparison_cache()
    assert edited_copy_verdict(str(orig), str(sc_path)) is True
    assert edited_copy_verdict(str(orig), str(bad_path)) is False


def test_unaligned_or_unrelated_returns_none_not_false(tmp_path: Path) -> None:
    """Unrelated textured scenes return None rather than blocking semantic candidates."""
    pytest.importorskip("cv2")
    import numpy as np

    rng = np.random.default_rng(7)
    noise_arr = rng.integers(0, 255, size=(256, 256, 3), dtype=np.uint8)
    other = tmp_path / "unrelated.png"
    Image.fromarray(noise_arr).save(other)

    clear_image_comparison_cache()
    assert edited_copy_verdict(str(FIXTURE), str(other)) is None


def test_isoluminant_local_patch_rejected_by_edited_copy_verdict(tmp_path: Path) -> None:
    """Localized red/green shift with unchanged luma is rejected by _chroma_agrees."""
    pytest.importorskip("cv2")
    with Image.open(FIXTURE) as source:
        base = source.convert("RGB")
    patched = base.copy()
    px = patched.load()
    for y in range(210, 266):
        for x in range(210, 266):
            r, g, b = px[x, y]
            g2 = max(0, min(255, g - 60))
            r2 = max(0, min(255, round(r + (0.587 / 0.299) * (g - g2))))
            g2 = max(0, min(255, round(g + (0.299 * (r - r2)) / 0.587)))
            px[x, y] = (r2, g2, b)
    a, b = tmp_path / "base.png", tmp_path / "patched.png"
    base.save(a)
    patched.save(b)
    clear_image_comparison_cache()
    assert edited_copy_verdict(str(a), str(b)) is False
