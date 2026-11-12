"""The large crop editor, edge snapping, and per-edge keys in the Crop review."""
import re
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import expect
from test_browser_workflow import _serve_app

from dedupe.borders import BORDER_LEVELS_SIGNATURE, BORDER_SIGNATURE
from dedupe.grouping import build_crop_groups
from dedupe.models import FileRecord, MediaType, ScanResult
from dedupe.web.app import create_app

pytestmark = pytest.mark.e2e

BALANCED = "40,30,280,210,320,240"
AGGRESSIVE = "60,40,270,200,320,240"


@pytest.fixture
def crop_review(page, tmp_path: Path):
    """Two 320x240 files: no conservative box, a balanced one, a tighter aggressive one."""
    records = []
    for name in ("first.png", "second.png"):
        path = tmp_path / name
        Image.new("RGB", (320, 240), "blue").save(path)
        stat = path.stat()
        records.append(FileRecord(
            path=str(path), size=stat.st_size, mtime=stat.st_mtime,
            media_type=MediaType.IMAGE, extension=".png", device=stat.st_dev,
            inode=stat.st_ino, mtime_ns=stat.st_mtime_ns, width=320, height=240,
            border_crop=BALANCED, border_signature=BORDER_SIGNATURE,
            border_crop_conservative="", border_crop_aggressive=AGGRESSIVE,
            border_levels_signature=BORDER_LEVELS_SIGNATURE,
        ))
    app = create_app(
        ScanResult(roots=[str(tmp_path)], files=records, groups=build_crop_groups(records)),
        review_session_path=tmp_path / "review.json",
    )
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "cache.sqlite3")
    page.add_init_script("""(() => {
      const original = window.fetch;
      window.adjustRequests = [];
      window.fetch = async (...args) => {
        if (String(args[0]).endsWith('/api/crop/adjust')) {
          window.adjustRequests.push(JSON.parse(args[1].body));
        }
        return original(...args);
      };
    })();""")
    with _serve_app(app) as url:
        page.goto(url, wait_until="domcontentloaded")
        page.locator('.tab[data-kind="crop"]').click()
        page.locator(".group-item").first.click()
        expect(page.locator("#members .crop-card")).to_have_count(2)
        yield page.locator("#members .crop-card")


def _sides(page) -> dict[str, str]:
    return {side: page.input_value(f"#ce{side}") for side in ("Top", "Right", "Bottom", "Left")}


def _last_box(page) -> dict:
    return page.evaluate("window.adjustRequests.at(-1).box")


def _drag(page, start: tuple[float, float], end: tuple[float, float]) -> None:
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=6)
    page.mouse.up()


def test_adjust_opens_the_zone_large_with_a_field_per_side(page, crop_review):
    card = crop_review.first
    card.locator(".crop-adjust").click()
    editor = page.locator("#cropEditor")
    expect(editor).to_be_visible()
    expect(page.locator("#ceName")).to_have_text("first.png")
    expect(page.locator("#ceCount")).to_have_text("1 of 2 to review")
    assert _sides(page) == {"Top": "30", "Right": "40", "Bottom": "30", "Left": "40"}
    expect(page.locator("#ceBox")).to_be_focused()

    page.fill("#ceBottom", "12")
    page.keyboard.press("Enter")
    page.wait_for_function("() => window.adjustRequests.length === 1")
    assert _last_box(page) == {"left": 40, "top": 30, "right": 280, "bottom": 228}
    expect(page.locator("#ceSummary")).to_contain_text("Adjusted zone")
    expect(card.locator(".evidence")).to_contain_text("Adjusted zone")

    page.locator('.ce-keep[data-side="top"]').click()
    page.wait_for_function("() => window.adjustRequests.length === 2")
    assert _last_box(page)["top"] == 0
    expect(page.locator('.ce-keep[data-side="top"]')).to_be_disabled()

    page.keyboard.press("r")
    page.wait_for_function("() => window.adjustRequests.at(-1).box === null")
    expect(page.locator("#ceTop")).to_have_value("30")
    expect(page.locator("#ceBottom")).to_have_value("30")

    page.keyboard.press("Escape")
    expect(editor).to_be_hidden()
    expect(card.locator(".crop-box")).to_be_focused()


def test_double_click_and_e_open_the_editor(page, crop_review):
    crop_review.first.locator(".crop-box").dblclick()
    expect(page.locator("#cropEditor")).to_be_visible()
    page.keyboard.press("Escape")
    crop_review.nth(1).locator(".crop-box").click()
    page.keyboard.press("e")
    expect(page.locator("#ceName")).to_have_text("second.png")


def test_dragged_edge_lands_on_another_levels_edge_unless_alt(page, crop_review):
    crop_review.first.locator(".crop-adjust").click()
    frame = page.locator("#ceFrame").bounding_box()
    scale = frame["width"] / 320
    assert scale > 2, "the editor draws the picture large"
    middle = frame["y"] + frame["height"] / 2
    left_edge = frame["x"] + 40 * scale

    # One picture pixel short of the aggressive level's left edge (60).
    _drag(page, (left_edge, middle), (left_edge + 19 * scale, middle))
    page.wait_for_function("() => window.adjustRequests.length === 1")
    assert _last_box(page)["left"] == 60

    # Back to within a pixel of the suggestion: it lands exactly on it,
    # which is no adjustment at all.
    here = frame["x"] + 60 * scale
    _drag(page, (here, middle), (frame["x"] + 41 * scale, middle))
    page.wait_for_function("() => window.adjustRequests.length === 2")
    assert _last_box(page)["left"] == 40
    expect(page.locator("#ceReset")).to_be_disabled()

    # Alt places the edge where the pointer is.
    page.keyboard.down("Alt")
    _drag(page, (left_edge, middle), (left_edge + 19 * scale, middle))
    page.keyboard.up("Alt")
    page.wait_for_function("() => window.adjustRequests.length === 3")
    assert _last_box(page)["left"] == 59


def test_edge_pulled_to_the_pictures_edge_keeps_that_side(page, crop_review):
    crop_review.first.locator(".crop-adjust").click()
    frame = page.locator("#ceFrame").bounding_box()
    scale = frame["width"] / 320
    centre = frame["x"] + frame["width"] / 2
    _drag(page, (centre, frame["y"] + 30 * scale), (centre, frame["y"] + 1.5 * scale))
    page.wait_for_function("() => window.adjustRequests.length === 1")
    assert _last_box(page)["top"] == 0


def test_alt_arrows_move_the_top_and_left_edges_on_a_card(page, crop_review):
    card = crop_review.first
    card.locator(".crop-box").focus()
    page.keyboard.press("Alt+ArrowRight")
    page.keyboard.press("Alt+ArrowDown")
    page.wait_for_function("() => window.adjustRequests.length === 1")
    # 1% of each dimension: 3px across, 2px down; the far edges stay put.
    assert _last_box(page) == {"left": 43, "top": 32, "right": 280, "bottom": 210}
    expect(card.locator(".crop-box")).to_be_focused()


def test_arrows_in_the_editor_move_by_a_screen_pixel(page, crop_review):
    crop_review.first.locator(".crop-adjust").click()
    page.keyboard.press("Shift+ArrowLeft")
    page.wait_for_function("() => window.adjustRequests.length === 1")
    # The picture is drawn larger than life, so one key is one picture pixel.
    assert _last_box(page) == {"left": 40, "top": 30, "right": 279, "bottom": 210}


def test_crop_and_skip_in_the_editor_move_to_the_next_file(page, crop_review):
    crop_review.first.locator(".crop-adjust").click()
    page.keyboard.press("x")
    expect(page.locator("#ceName")).to_have_text("second.png")
    expect(page.locator("#ceCount")).to_have_text("1 of 1 to review")
    expect(crop_review.first).to_have_class(re.compile(r"\bskipped\b"))
    page.locator("#ceApply").click()
    # Nothing left to review: the editor closes on its own.
    expect(page.locator("#cropEditor")).to_be_hidden()
    expect(crop_review.nth(1)).to_have_class(re.compile(r"\bcropped\b"))
    with Image.open(Path(crop_review.nth(1).get_attribute("data-path"))) as cropped:
        assert cropped.size == (240, 180)


def test_a_level_with_no_suggestion_starts_from_the_whole_picture(page, crop_review):
    card = crop_review.first
    card.locator('.crop-level-btn[data-level="conservative"]').click()
    expect(card.locator(".crop-apply")).to_be_disabled()
    card.locator(".crop-adjust").click()
    assert _sides(page) == {"Top": "0", "Right": "0", "Bottom": "0", "Left": "0"}
    expect(page.locator("#ceApply")).to_be_disabled()
    expect(page.locator("#ceSummary")).to_contain_text("Nothing is cut yet")

    page.fill("#ceLeft", "25")
    page.keyboard.press("Enter")
    page.wait_for_function("() => window.adjustRequests.length === 1")
    assert _last_box(page) == {"left": 25, "top": 0, "right": 320, "bottom": 240}
    expect(page.locator("#ceApply")).to_be_enabled()
    page.keyboard.press("Escape")
    expect(card.locator(".crop-apply")).to_be_enabled()
    expect(card.locator(".evidence")).to_contain_text("25px left")
