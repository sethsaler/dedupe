"""Bars of any flat colour, and what the aggressive level may call a bar.

A picture set on white, gray or a colour is framed as surely as one on
black. What must stay uncropped is a page's own margin, a backdrop behind
a subject, a sky that shades away, and any dark part of a picture.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from dedupe.borders import CropBox, detect_colour_bars, detect_crop_levels

WHITE = (255, 255, 255)


def _picture(width: int, height: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.integers(70, 230, size=(height, width, 3), dtype=np.uint8)
    base[:, : width // 2, 0] = 200
    return base


def _on(colour, size, box, seed: int = 0) -> Image.Image:
    """A picture filling ``box`` (left, top, right, bottom) of a flat canvas."""
    left, top, right, bottom = box
    image = Image.new("RGB", size, colour)
    image.paste(Image.fromarray(_picture(right - left, bottom - top, seed)), (left, top))
    return image


def _levels(image: Image.Image, path: Path, **save):
    image.save(path, **save)
    return detect_crop_levels(path)


@pytest.mark.parametrize(
    "colour", [WHITE, (128, 128, 132), (250, 244, 225), (52, 120, 200)],
    ids=["white", "gray", "cream", "blue"],
)
def test_flat_frame_of_any_colour_is_cropped_at_every_level(tmp_path: Path, colour) -> None:
    levels = _levels(_on(colour, (640, 800), (40, 120, 600, 700)), tmp_path / "framed.png")
    assert levels["conservative"] == CropBox(40, 120, 600, 700, 640, 800)
    assert levels["balanced"] == levels["conservative"]
    assert levels["aggressive"] == levels["conservative"]


def test_white_letterbox_in_a_jpeg_leaves_no_hairline(tmp_path: Path) -> None:
    # 260 and 940 fall inside 8-row blocks, so the bar's last rows ring.
    levels = _levels(
        _on(WHITE, (900, 1200), (0, 260, 900, 940)), tmp_path / "story.jpg", quality=90
    )
    box = levels["balanced"]
    assert box is not None
    assert 260 <= box.top <= 262 and 938 <= box.bottom <= 940
    assert (box.left, box.right) == (0, 900)


def test_collage_loses_its_frame_and_keeps_its_divider(tmp_path: Path) -> None:
    image = _on(WHITE, (800, 1000), (50, 60, 750, 940))
    ImageDraw.Draw(image).rectangle([50, 490, 749, 509], fill=WHITE)  # between two photos
    assert _levels(image, tmp_path / "collage.png")["balanced"] == CropBox(
        50, 60, 750, 940, 800, 1000
    )


def test_frame_side_goes_though_its_other_side_cannot(tmp_path: Path) -> None:
    # The right of the picture is as white as the canvas for most of its
    # height, so no edge shows there and that margin stays; it stands at the
    # edge of what is kept, between nothing, and is no page showing through.
    image = _on(WHITE, (1000, 800), (150, 0, 850, 800))
    ImageDraw.Draw(image).rectangle([700, 0, 849, 500], fill=WHITE)
    assert _levels(image, tmp_path / "pale.png")["balanced"] == CropBox(
        150, 0, 1000, 800, 1000, 800
    )


def test_frame_behind_a_hairline_at_the_edge_is_still_a_frame(tmp_path: Path) -> None:
    # One dark column outside the white margin, as an export a pixel wide leaves.
    image = _on(WHITE, (1000, 800), (150, 0, 820, 800))
    ImageDraw.Draw(image).line([999, 0, 999, 799], fill=(30, 30, 30))
    assert _levels(image, tmp_path / "edged.png")["conservative"] == CropBox(
        150, 0, 820, 800, 1000, 800
    )


def test_frame_inside_a_frame_loses_both(tmp_path: Path) -> None:
    image = _on((128, 128, 132), (800, 1000), (100, 120, 700, 880))
    canvas = Image.new("RGB", (900, 1100), WHITE)
    canvas.paste(image, (50, 50))
    assert _levels(canvas, tmp_path / "matted.png")["conservative"] == CropBox(
        150, 170, 750, 930, 900, 1100
    )


def test_a_pages_margin_is_not_a_frame(tmp_path: Path) -> None:
    # Paragraphs and a picture inset on a white page: white comes back
    # between every line, so it is the page, not a bar around the content.
    image = _on(WHITE, (800, 1200), (40, 500, 760, 900))
    draw = ImageDraw.Draw(image)
    for top in (*range(60, 460, 50), *range(940, 1160, 50)):
        draw.rectangle([40, top, 760, top + 28], fill=(30, 30, 30))
    levels = _levels(image, tmp_path / "page.png")
    assert levels == {"conservative": None, "balanced": None, "aggressive": None}


def test_backdrop_behind_a_subject_is_not_a_bar(tmp_path: Path) -> None:
    # A seamless white backdrop behind a figure: no straight side of it runs
    # along half of any row or column, however much of the frame it fills.
    image = Image.new("RGB", (900, 1200), WHITE)
    figure = Image.fromarray(_picture(700, 1000))
    outline = Image.new("L", (700, 1000), 0)
    ImageDraw.Draw(outline).ellipse([0, 0, 699, 1399], fill=255)  # head and shoulders
    image.paste(figure, (100, 200), outline)
    levels = _levels(image, tmp_path / "studio.png")
    assert levels == {"conservative": None, "balanced": None, "aggressive": None}


def test_picture_to_one_side_of_a_canvas_loses_every_margin(tmp_path: Path) -> None:
    # A third of the canvas wide: the sides are bars at once (the picture
    # runs most of their length), the band above it only after they go.
    image = _on(WHITE, (900, 1200), (300, 300, 600, 1200))
    assert _levels(image, tmp_path / "offset.png")["conservative"] == CropBox(
        300, 300, 600, 1200, 900, 1200
    )


def test_sky_that_shades_away_is_not_a_bar(tmp_path: Path) -> None:
    # Every row is one colour and drifts past the tolerance a little at a
    # time: the colour is left behind, but no row steps off the one before.
    canvas = _picture(800, 1000)
    ramp = np.linspace(0, 90, 400)[:, None]
    canvas[:400] = (np.array([120, 150, 160]) + ramp).astype(np.uint8)[:, None, :]
    levels = _levels(Image.fromarray(canvas), tmp_path / "sky.png")
    assert levels == {"conservative": None, "balanced": None, "aggressive": None}


def test_bar_beside_a_pale_picture_past_a_soft_edge(tmp_path: Path) -> None:
    # A hazy sky barely off white, behind three rows the edge's ringing left
    # between the two. Measured from those rows the sky is no step at all;
    # measured from the bar's last flat row it is.
    canvas = np.full((600, 800, 3), 255, dtype=np.uint8)
    canvas[100:103] = 246
    canvas[100:103, ::10] = 200
    canvas[103:] = np.random.default_rng(5).integers(236, 241, size=(497, 800, 1))
    image = Image.fromarray(canvas)
    hill = Image.new("L", (800, 600), 0)
    ImageDraw.Draw(hill).ellipse([100, 380, 699, 899], fill=255)  # no straight side to it
    image.paste(Image.fromarray(_picture(800, 600)), (0, 0), hill)
    levels = _levels(image, tmp_path / "haze.png")
    assert levels["conservative"] == CropBox(0, 103, 800, 600, 800, 600)


def test_transparent_canvas_is_not_a_white_frame(tmp_path: Path) -> None:
    image = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    image.paste(Image.fromarray(_picture(240, 240)).convert("RGBA"), (80, 80))
    levels = _levels(image, tmp_path / "sticker.png")
    assert levels == {"conservative": None, "balanced": None, "aggressive": None}


def test_strip_holding_an_icon_needs_aggressive(tmp_path: Path) -> None:
    image = _on(WHITE, (900, 1200), (0, 0, 900, 1110))
    ImageDraw.Draw(image).ellipse([840, 1140, 870, 1170], fill=(90, 90, 90))  # a mute button
    levels = _levels(image, tmp_path / "muted.png")
    assert levels["conservative"] is None and levels["balanced"] is None
    assert levels["aggressive"] == CropBox(0, 0, 900, 1110, 900, 1200)


def test_caption_under_a_picture_stays_at_every_level(tmp_path: Path) -> None:
    # A meme's words on white are the point of it, not ink riding in a bar.
    image = _on(WHITE, (900, 1200), (0, 0, 900, 900))
    draw = ImageDraw.Draw(image)
    for top in (950, 1010, 1070):
        for left in range(60, 840, 26):
            draw.rectangle([left, top, left + 15, top + 36], fill=(20, 20, 20))
    levels = _levels(image, tmp_path / "meme.png")
    assert levels == {"conservative": None, "balanced": None, "aggressive": None}


def test_interface_owns_its_edge_unless_the_colour_frames_the_picture() -> None:
    frame = np.asarray(_on((52, 120, 200), (640, 800), (0, 90, 640, 800)))
    assert detect_colour_bars(frame) == (90, 0, 0, 0)
    # Under a cut-away status bar, a flat band above the picture is the
    # picture's own sky as far as anyone can tell.
    assert detect_colour_bars(frame, interface=(True, False)) is None
    framed = np.asarray(_on((52, 120, 200), (640, 800), (40, 90, 600, 800)))
    assert detect_colour_bars(framed, interface=(True, False)) == (90, 0, 40, 40)


# --- the aggressive level -----------------------------------------------------


def _dark_scene(width: int, height: int) -> np.ndarray:
    """A picture whose top is a dark, textured beam: dark, never empty."""
    canvas = _picture(width, height)
    rng = np.random.default_rng(3)
    beam = 30 + 14 * np.sin(np.arange(width) / 23.0)[None, :] + rng.normal(0, 3, (160, width))
    beam += np.linspace(0, 18, 160)[:, None]
    canvas[:160] = np.clip(beam, 0, 255).astype(np.uint8)[:, :, None]
    return canvas


def test_dark_part_of_a_picture_is_not_an_aggressive_bar(tmp_path: Path) -> None:
    levels = _levels(Image.fromarray(_dark_scene(800, 1000)), tmp_path / "beam.png")
    assert levels == {"conservative": None, "balanced": None, "aggressive": None}


def test_dark_sky_without_a_ruled_edge_is_not_an_aggressive_bar(tmp_path: Path) -> None:
    # Flat dark gray for 150 rows, then a roofline falling across the frame
    # over the next forty: empty, and followed by picture, but where the
    # dark stops only a third of the row has stepped off it.
    canvas = _picture(800, 1000)
    canvas[:300] = 52
    for x in range(800):
        canvas[150 + (x * 40) // 800 : 300, x] = 180
    levels = _levels(Image.fromarray(canvas), tmp_path / "night.png")
    assert levels == {"conservative": None, "balanced": None, "aggressive": None}


def test_hairline_bar_is_aggressive_only(tmp_path: Path) -> None:
    canvas = _picture(1200, 1500)
    canvas[:, :3] = 2  # three black columns: under the balanced minimum
    levels = _levels(Image.fromarray(canvas), tmp_path / "hairline.png")
    assert levels["conservative"] is None and levels["balanced"] is None
    assert levels["aggressive"] == CropBox(3, 0, 1200, 1500, 1200, 1500)
