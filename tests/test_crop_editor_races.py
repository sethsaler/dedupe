"""Crop editing remains authoritative while asynchronous saves complete."""
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


@pytest.fixture
def crop_editor(page, tmp_path: Path):
    records = []
    for name in ("first.png", "second.png"):
        path = tmp_path / name
        Image.new("RGB", (320, 240), "blue").save(path)
        stat = path.stat()
        records.append(FileRecord(
            path=str(path), size=stat.st_size, mtime=stat.st_mtime,
            media_type=MediaType.IMAGE, extension=".png", device=stat.st_dev,
            inode=stat.st_ino, mtime_ns=stat.st_mtime_ns, width=320, height=240,
            border_crop="40,30,280,210,320,240", border_signature=BORDER_SIGNATURE,
            border_crop_conservative="40,30,280,210,320,240",
            border_crop_aggressive="40,30,280,210,320,240",
            border_levels_signature=BORDER_LEVELS_SIGNATURE,
        ))
    app = create_app(
        ScanResult(roots=[str(tmp_path)], files=records, groups=build_crop_groups(records)),
        review_session_path=tmp_path / "review.json",
    )
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "cache.sqlite3")
    # Hold replies, not requests: the real server has already stored the edit.
    page.add_init_script("""(() => {
      const original = window.fetch;
      window.adjustRequests = [];
      window.heldAdjustments = [];
      window.holdAdjustments = true;
      window.fetch = async (...args) => {
        const adjust = String(args[0]).endsWith('/api/crop/adjust');
        if (adjust) window.adjustRequests.push(JSON.parse(args[1].body));
        const response = await original(...args);
        if (adjust && window.holdAdjustments) {
          await new Promise(resolve => window.heldAdjustments.push(resolve));
        }
        return response;
      };
    })();""")
    with _serve_app(app) as url:
        page.goto(url, wait_until="domcontentloaded")
        page.locator('.tab[data-kind="crop"]').click()
        page.locator(".group-item").first.click()
        expect(page.locator("#members .crop-card")).to_have_count(2)
        yield page.locator("#members .crop-card")


def _left(card):
    return card.locator(".crop-box").evaluate("el => parseFloat(el.style.left)")


def _release(page):
    page.evaluate("window.holdAdjustments = false; window.heldAdjustments.splice(0).forEach(f => f())")


def test_delayed_save_preserves_newer_edit_and_crop_flushes_latest(page, crop_editor):
    card = crop_editor.first
    card.locator(".crop-box").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_function("() => window.heldAdjustments.length === 1")
    # A status refresh can replace the edited card before the save replies.
    # It must retain zone focus, or the next arrow navigates to another card.
    page.evaluate("""async () => {
      const {state} = await import('/static/state.js');
      const {selectGroup} = await import('/static/members.js');
      await selectGroup(state.currentId, {silent: true, preservePlayback: true});
    }""")
    expect(card.locator(".crop-box")).to_be_focused()
    page.keyboard.press("ArrowRight")
    latest = _left(card)
    _release(page)
    page.wait_for_function("() => window.heldAdjustments.length === 0")
    # Deliver the old response before the newer edit's debounce expires.
    page.wait_for_timeout(70)
    assert _left(card) == latest
    applied = []
    page.route("**/api/crop/apply", lambda route: (
        applied.append(route.request.post_data_json),
        route.fulfill(status=400, json={"error": "test intercepted apply"}),
    ))
    page.keyboard.press("c")
    page.wait_for_function("() => window.adjustRequests.length >= 2")
    expect(page.locator("#toastMessage")).to_contain_text("test intercepted apply")
    assert applied[0]["box"]["left"] == 46
    assert page.evaluate("window.adjustRequests.at(-1).box.left") == 46


def test_same_file_saves_are_serialized_and_reset_wins(page, crop_editor):
    card = crop_editor.first
    card.locator(".crop-box").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_function("() => window.heldAdjustments.length === 1")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(450)
    assert page.evaluate("window.adjustRequests.length") == 1
    _release(page)
    page.wait_for_function("() => window.adjustRequests.length === 2")
    expect(card.locator(".crop-reset")).to_be_visible()
    page.evaluate("window.holdAdjustments = true")
    card.locator(".crop-box").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_function("() => window.heldAdjustments.length === 1")
    card.locator(".crop-reset").click()
    _release(page)
    expect(card.locator(".crop-reset")).to_have_count(0)
    assert _left(card) == 12.5
    page.wait_for_function("() => window.adjustRequests.at(-1).box === null")


def test_tab_focus_selects_crop_target_and_reordered_cards_keep_edits(page, crop_editor):
    first, second = crop_editor.first, crop_editor.nth(1)
    first.locator(".crop-box").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_function("() => window.heldAdjustments.length === 1")
    second.locator(".crop-thumb").focus()
    page.keyboard.press("Tab")
    expect(second.locator(".crop-box")).to_be_focused()
    page.keyboard.press("ArrowRight")
    page.wait_for_function("() => window.heldAdjustments.length === 2")
    page.evaluate("window.heldAdjustments.pop()()")
    expect(second.locator(".crop-reset")).to_be_visible()
    _release(page)
    expect(first.locator(".crop-reset")).to_be_visible()
    assert _left(second) == 43 / 320 * 100
    expect(second.locator(".crop-box")).to_be_focused()
    applied = []
    page.route("**/api/crop/apply", lambda route: (
        applied.append(route.request.post_data_json),
        route.fulfill(status=400, json={"error": "test intercepted apply"}),
    ))
    page.keyboard.press("c")
    expect(page.locator("#toastMessage")).to_contain_text("test intercepted apply")
    assert applied[0]["path"] == second.get_attribute("data-path")
    assert applied[0]["box"]["left"] == 43


def test_previous_scan_reply_cannot_replace_current_review(page, crop_editor):
    card = crop_editor.first
    card.locator(".crop-box").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_function("() => window.heldAdjustments.length === 1")
    page.evaluate("""async () => {
      const {state} = await import('/static/state.js');
      const {renderCropReview} = await import('/static/crop.js');
      state.scanId = 'replacement-scan';
      renderCropReview(state.allGroups.find(g => g.id === state.currentId));
    }""")
    _release(page)
    page.wait_for_timeout(100)
    assert _left(card) == 12.5
    expect(card.locator(".crop-reset")).to_have_count(0)


def test_tabbed_zone_keyboard_crop_targets_that_card(page, crop_editor):
    second = crop_editor.nth(1)
    second.locator(".crop-thumb").focus()
    page.keyboard.press("Tab")
    expect(second.locator(".crop-box")).to_be_focused()
    applied = []
    page.route("**/api/crop/apply", lambda route: (
        applied.append(route.request.post_data_json),
        route.fulfill(status=400, json={"error": "test intercepted apply"}),
    ))
    page.keyboard.press("ArrowRight")
    _release(page)
    page.keyboard.press("c")
    expect(page.locator("#toastMessage")).to_contain_text("test intercepted apply")
    assert applied[0]["path"] == second.get_attribute("data-path")
    assert applied[0]["box"]["left"] == 43


def test_delayed_save_does_not_revert_skip(page, crop_editor):
    card = crop_editor.first
    card.locator(".crop-box").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_function("() => window.heldAdjustments.length === 1")
    page.keyboard.press("x")
    expect(card).to_have_class(re.compile(r"\bskipped\b"))
    _release(page)
    page.wait_for_timeout(100)
    expect(card).to_have_class(re.compile(r"\bskipped\b"))


def test_bulk_crop_retries_unsaved_geometry_and_aborts_on_save_failure(page, crop_editor):
    card = crop_editor.first
    _release(page)
    page.route("**/api/crop/adjust", lambda route: route.fulfill(
        status=400, json={"error": "save unavailable"},
    ))
    applied = []
    page.route("**/api/crop/apply-all", lambda route: (
        applied.append(route.request.post_data_json),
        route.fulfill(status=400, json={"error": "test intercepted bulk apply"}),
    ))
    card.locator(".crop-box").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#toastMessage")).to_contain_text("save unavailable")
    # The failed save's timer is gone, but its visible edit must still be saved
    # successfully before bulk apply can use the server's geometry.
    page.locator("#btnCropAll").click()
    page.locator("#modalConfirm").click()
    page.wait_for_function("() => window.adjustRequests.length === 2")
    expect(page.locator("#btnCropAll")).to_be_enabled()
    assert not applied
    assert _left(card) == 43 / 320 * 100
    page.unroute("**/api/crop/adjust")
    page.locator("#btnCropAll").click()
    with page.expect_response("**/api/crop/apply-all"):
        page.locator("#modalConfirm").click()
    assert len(applied) == 1
    assert page.evaluate("window.adjustRequests.at(-1).box.left") == 43


def test_delayed_reset_does_not_render_crop_ui_over_another_group(page, crop_editor):
    card = crop_editor.first
    _release(page)
    card.locator(".crop-box").focus()
    with page.expect_response("**/api/crop/adjust"):
        page.keyboard.press("ArrowRight")
    expect(card.locator(".crop-reset")).to_be_visible()
    crop_group_id = page.evaluate("""async () => {
      const {state} = await import('/static/state.js');
      return state.currentId;
    }""")
    page.evaluate("window.holdAdjustments = true")
    card.locator(".crop-reset").click()
    page.wait_for_function("() => window.heldAdjustments.length === 1")

    # Use the real tab and group navigation while the reset reply is held.
    page.locator('.tab[data-kind="all_files"]').click()
    page.locator(".group-item").first.click()
    expect(page.locator("#detailTitle")).to_contain_text("All files")
    expect(page.locator("#members .crop-card")).to_have_count(0)
    expect(page.locator("#members .card")).to_have_count(2)
    page.evaluate("""() => {
      window.unexpectedCropCards = [];
      new MutationObserver(records => {
        for (const record of records) for (const node of record.addedNodes) {
          if (node.nodeType === 1 && node.matches('.crop-card')) {
            window.unexpectedCropCards.push(node.dataset.path);
          }
        }
      }).observe(document.querySelector('#members'), {childList: true});
    }""")
    _release(page)
    page.wait_for_function("""async (id) => {
      const {state} = await import('/static/state.js');
      return state.allGroups.find(g => g.id === id).members.every(m => !m.crop_adjusted);
    }""", arg=crop_group_id)
    page.wait_for_timeout(200)
    assert page.evaluate("window.unexpectedCropCards") == []
    expect(page.locator("#members .crop-card")).to_have_count(0)
    expect(page.locator("#members")).not_to_have_class(re.compile(r"\bcrop-grid\b"))
    expect(page.locator("#detailTitle")).to_contain_text("All files")

    # The originating review still receives the reset when revisited.
    page.locator('.tab[data-kind="crop"]').click()
    page.locator(".group-item").first.click()
    expect(page.locator("#members .crop-card")).to_have_count(2)
    expect(page.locator("#members .crop-reset")).to_have_count(0)
    assert _left(page.locator("#members .crop-card").first) == 12.5
