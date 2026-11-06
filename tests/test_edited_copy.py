"""Fine local changes also veto the semantic geometric verification path."""
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from dedupe.edited_copy import edited_copy_verdict, is_aligned_image_copy
from dedupe.similar_image import clear_image_comparison_cache

FIXTURE = Path(__file__).parent / "fixtures" / "astronaut.png"


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
