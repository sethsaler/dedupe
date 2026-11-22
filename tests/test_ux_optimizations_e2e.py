"""Playwright browser end-to-end tests for recent UX optimizations and bug fixes."""

from __future__ import annotations

import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from playwright.sync_api import expect
from werkzeug.serving import make_server

from dedupe.web.app import create_app

pytestmark = pytest.mark.e2e


@pytest.fixture
def live_dedupe_server(tmp_path: Path):
    """Serve an isolated review session and always stop its server thread."""
    app = create_app(review_session_path=tmp_path / "review.json")
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "hash-cache.sqlite3")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.status == 200
        yield url
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def _create_textured_image(
    path: Path,
    *,
    width: int = 320,
    height: int = 240,
    top_bar: int = 0,
    bottom_bar: int = 0,
    marker_pixels: list[tuple[int, int, tuple[int, int, int]]] | None = None,
) -> Path:
    """Create a synthetic high-detail textured image with optional crisp black bars."""
    img = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    y_start = top_bar
    y_end = height - bottom_bar
    for y in range(y_start, y_end, 8):
        for x in range(0, width, 8):
            color = (
                (x * 5 + y * 3) % 210 + 30,
                (x * 11 + y * 7) % 210 + 30,
                (x * 3 + y * 13) % 210 + 30,
            )
            draw.rectangle([x, y, min(width - 1, x + 7), min(y_end - 1, y + 7)], fill=color)
    if marker_pixels:
        for px, py, rgb in marker_pixels:
            img.putpixel((px, py), rgb)
    img.save(path)
    return path


def _element_contrast_ratio(page, selector: str) -> float:
    """Resolve effective foreground/background colors and return WCAG contrast ratio."""
    return page.evaluate(
        """(sel) => {
          const el = document.querySelector(sel);
          if (!el) throw new Error(`Element not found: ${sel}`);

          const parseColor = (raw) => {
            if (!raw || raw === 'transparent') return null;
            const srgb = raw.match(
              /^color\\(srgb\\s+([\\d.]+)\\s+([\\d.]+)\\s+([\\d.]+)(?:\\s*\\/\\s*([\\d.]+))?\\)$/i
            );
            if (srgb) {
              return {
                r: Number(srgb[1]) * 255,
                g: Number(srgb[2]) * 255,
                b: Number(srgb[3]) * 255,
                a: srgb[4] === undefined ? 1 : Number(srgb[4]),
              };
            }
            const rgba = raw.match(
              /^rgba?\\(\\s*([\\d.]+)[,\\s]+([\\d.]+)[,\\s]+([\\d.]+)(?:[,\\s/]+([\\d.]+))?\\s*\\)$/i
            );
            if (!rgba) return null;
            return {
              r: Number(rgba[1]),
              g: Number(rgba[2]),
              b: Number(rgba[3]),
              a: rgba[4] === undefined ? 1 : Number(rgba[4]),
            };
          };

          const blend = (fg, bg) => {
            const a = fg.a;
            return {
              r: fg.r * a + bg.r * (1 - a),
              g: fg.g * a + bg.g * (1 - a),
              b: fg.b * a + bg.b * (1 - a),
              a: 1,
            };
          };

          const layers = [];
          for (let cur = el; cur; cur = cur.parentElement) {
            const c = parseColor(getComputedStyle(cur).backgroundColor);
            if (c && c.a > 0) layers.push(c);
          }
          let bg = { r: 255, g: 255, b: 255, a: 1 };
          for (let i = layers.length - 1; i >= 0; i--) {
            bg = blend(layers[i], bg);
          }
          const rawFg = parseColor(getComputedStyle(el).color);
          if (!rawFg) throw new Error(`Unparseable foreground color for ${sel}`);
          const fg = rawFg.a < 1 ? blend(rawFg, bg) : rawFg;

          const srgbChannel = (v) => {
            const c = v / 255;
            return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
          };
          const lum = (rgb) => (
            0.2126 * srgbChannel(rgb.r)
            + 0.7152 * srgbChannel(rgb.g)
            + 0.0722 * srgbChannel(rgb.b)
          );
          const l1 = lum(fg);
          const l2 = lum(bg);
          return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        }""",
        selector,
    )


def test_focus_trap_tabs_through_lightbox_and_crop_editor(
    page, live_dedupe_server: str, tmp_path: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    media = tmp_path / "media"
    media.mkdir()
    first = _create_textured_image(media / "framed_a.png", top_bar=40, bottom_bar=40)
    shutil.copyfile(first, media / "framed_b.png")

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#optBlackBorders").check(force=True)
    page.locator("#paths").fill(str(media))
    page.locator("#btnScan").click()

    expect(page.locator("#results")).to_be_visible(timeout=20_000)
    page.locator('.tab[data-kind="exact"]').click()
    page.locator(".group-item").first.click()
    expect(page.locator("#members .card")).to_have_count(2, timeout=20_000)

    # Open lightbox and verify Tab cycles through multiple interior controls without escaping.
    page.locator("#members .card .thumb-wrap").first.click()
    expect(page.locator("#lightbox")).to_be_visible()
    assert page.evaluate("document.activeElement?.id") == "lbClose"

    lb_visited: list[str] = ["lbClose"]
    for _ in range(8):
        page.keyboard.press("Tab")
        state_info = page.evaluate(
            """() => ({
              inside: document.getElementById('lightbox').contains(document.activeElement),
              id: document.activeElement?.id || document.activeElement?.tagName || '',
            })"""
        )
        assert state_info["inside"] is True
        lb_visited.append(state_info["id"])

    assert lb_visited[1] != lb_visited[0]
    assert len(set(lb_visited)) >= 5

    page.keyboard.press("Escape")
    expect(page.locator("#lightbox")).to_be_hidden()

    # Open crop editor and verify Tab advances through controls and wraps cleanly inside.
    page.locator('.tab[data-kind="crop"]').click()
    expect(page.locator("#members .crop-card")).to_have_count(2, timeout=20_000)
    page.locator("#members .crop-card .crop-adjust").first.click()
    expect(page.locator("#cropEditor")).to_be_visible()
    assert page.evaluate("document.activeElement?.id") == "ceBox"

    focusable_count = page.evaluate(
        """() => {
          const ce = document.getElementById('cropEditor');
          return [...ce.querySelectorAll(
            'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
          )].filter((el) => el.offsetWidth || el.offsetHeight || el.getClientRects().length).length;
        }"""
    )
    assert focusable_count >= 6

    ce_visited: list[str] = []
    for _ in range(focusable_count + 2):
        page.keyboard.press("Tab")
        state_info = page.evaluate(
            """() => {
              const el = document.activeElement;
              const ce = document.getElementById('cropEditor');
              return {
                inside: ce.contains(el),
                key: el?.id || el?.dataset?.side || el?.className || el?.tagName || '',
              };
            }"""
        )
        assert state_info["inside"] is True
        ce_visited.append(state_info["key"])

    assert ce_visited[0] != "ceBox"
    assert len(set(ce_visited)) >= 6
    for expected_id in ("ceTop", "ceRight", "ceApply", "ceClose"):
        assert expected_id in ce_visited
    assert "ceBox" in ce_visited[focusable_count - 2 :]

    page.keyboard.press("Escape")
    expect(page.locator("#cropEditor")).to_be_hidden()
    assert page_errors == []


def test_crop_card_evidence_visible_and_keep_whole_persists(
    page, live_dedupe_server: str, tmp_path: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    media = tmp_path / "top_border"
    media.mkdir()
    _create_textured_image(media / "top_bar.png", width=320, height=240, top_bar=48)

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#scanPreset").select_option("crop")
    page.locator("#paths").fill(str(media))
    page.locator("#btnScan").click()

    page.locator('.tab[data-kind="crop"]').click()
    card = page.locator("#members .crop-card").first
    expect(card).to_be_visible(timeout=20_000)

    # Evidence line must be visible on crop cards by default without hovering.
    evidence = card.locator(".evidence")
    expect(evidence).to_be_visible()
    expect(evidence).to_contain_text("48px top")

    # Open crop editor, click Keep side on top, wait past adjust debounce, verify persistence.
    card.locator(".crop-adjust").click()
    expect(page.locator("#cropEditor")).to_be_visible()
    expect(page.locator("#ceTop")).to_have_value("48")

    page.locator('.ce-keep[data-side="top"]').click()
    expect(page.locator("#ceTop")).to_have_value("0")
    expect(page.locator("#ceApply")).to_be_disabled()

    page.wait_for_timeout(700)
    expect(page.locator("#ceTop")).to_have_value("0")
    expect(page.locator("#ceApply")).to_be_disabled()
    assert page_errors == []


def test_crop_editor_video_scrubber(
    page, live_dedupe_server: str, tmp_path: Path
) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")

    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    media = tmp_path / "video_border"
    media.mkdir()
    video_path = media / "letterbox.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x160:rate=12,pad=320:240:0:40:black",
            "-t",
            "3",
            "-pix_fmt",
            "yuv420p",
            str(video_path),
        ],
        check=True,
    )

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#scanPreset").select_option("crop")
    page.locator("#paths").fill(str(media))
    page.locator("#btnScan").click()

    page.locator('.tab[data-kind="crop"]').click()
    card = page.locator("#members .crop-card").first
    expect(card).to_be_visible(timeout=20_000)

    card.locator(".crop-adjust").click()
    expect(page.locator("#cropEditor")).to_be_visible()
    expect(page.locator("#ceVideo")).to_be_visible()
    expect(page.locator("#ceVideoStrip")).to_be_visible()

    chips = page.locator("#ceVideoStrip .ce-sample-chip")
    expect(chips).to_have_count(5)

    # Wait for video metadata so currentTime seeks reliably in Chromium.
    page.wait_for_function(
        """() => {
          const v = document.getElementById('ceVideo');
          return Boolean(v && v.readyState >= 1 && Number.isFinite(v.duration) && v.duration > 0);
        }"""
    )

    target_chip = chips.nth(3)  # 73% sample chip
    target_frac = float(target_chip.get_attribute("data-frac") or "0.73")
    target_chip.click()
    page.wait_for_function(
        """(frac) => {
          const v = document.getElementById('ceVideo');
          return v && Math.abs(v.currentTime - frac * v.duration) < 0.35;
        }""",
        arg=target_frac,
    )
    expect(target_chip).to_have_attribute("aria-pressed", "true")
    assert page_errors == []


def test_lightbox_comparison_modes_flicker_and_zoom_persistence(
    page, live_dedupe_server: str, tmp_path: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    media = tmp_path / "similar_media"
    media.mkdir()
    _create_textured_image(media / "photo_a.png", width=320, height=240)
    _create_textured_image(
        media / "photo_b.png",
        width=320,
        height=240,
        marker_pixels=[(0, 0, (255, 40, 40)), (1, 0, (255, 40, 40))],
    )

    page.add_init_script("localStorage.setItem('dedupe.similarView', 'grid');")
    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(media))
    page.locator("#btnScan").click()

    expect(page.locator("#results")).to_be_visible(timeout=20_000)
    page.locator('.tab[data-kind="similar"]').click()
    page.locator(".group-item").first.click()
    expect(page.locator("#members .card")).to_have_count(2, timeout=20_000)

    # Open lightbox on the non-keeper candidate copy so comparison tools are active.
    candidate_card = page.locator("#members .card:not(.keep)").first
    candidate_card.locator(".thumb-wrap").click()
    expect(page.locator("#lightbox")).to_be_visible()
    expect(page.locator("#lbCompareTools")).to_be_visible()

    expect(page.locator("#lbOpacity")).to_have_value("0")
    expect(page.locator("#lbStageBadge")).to_have_text("Candidate")
    expect(page.locator("#lbKeeperImage")).to_have_css("opacity", "0")

    # Hold 'f' to flicker to the Reference image and release to restore Candidate.
    page.keyboard.down("f")
    expect(page.locator("#lbStageBadge")).to_have_text("Reference")
    expect(page.locator("#lbKeeperImage")).to_have_css("opacity", "1")
    page.keyboard.up("f")
    expect(page.locator("#lbStageBadge")).to_have_text("Candidate")
    expect(page.locator("#lbKeeperImage")).to_have_css("opacity", "0")

    # Zoom to 2x and verify zoom level persists across #lbSelect and item stepping.
    page.locator("#lbZoomLevel").select_option("2")
    expect(page.locator("#lbZoomLevel")).to_have_value("2")

    page.locator("#lbSelect").click()
    expect(page.locator("#lbZoomLevel")).to_have_value("2")

    page.locator("#lbNext").click()
    expect(page.locator("#lbZoomLevel")).to_have_value("2")
    page.locator("#lbPrev").click()
    expect(page.locator("#lbZoomLevel")).to_have_value("2")

    # Verify Split curtain and Side-by-side comparison modes.
    page.locator("#lbCompareMode").select_option("split")
    expect(page.locator("#lbSplitHandle")).to_be_visible()
    expect(page.locator("#lbStageBadge")).to_have_text("Reference | Candidate")

    page.locator("#lbCompareMode").select_option("side")
    expect(page.locator("#lbStageBadge")).to_have_text("Candidate | Reference")
    assert page_errors == []


def test_toast_queue_preemption_and_rapid_undo_replacement(
    page, live_dedupe_server: str
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.wait_for_function("() => Boolean(document.getElementById('toast'))")

    # Rapid Undo toasts replace immediately rather than queuing behind one another.
    page.evaluate(
        """async () => {
          const { toast } = await import('/static/util.js');
          toast('Undo first action', 'ok', { actionLabel: 'Undo', onAction: () => {} });
        }"""
    )
    expect(page.locator("#toast")).to_be_visible()
    expect(page.locator("#toastMessage")).to_have_text("Undo first action")

    page.evaluate(
        """async () => {
          const { toast } = await import('/static/util.js');
          toast('Undo second action', 'ok', { actionLabel: 'Undo', onAction: () => {} });
        }"""
    )
    expect(page.locator("#toastMessage")).to_have_text("Undo second action")

    # An error toast preempts an active non-error/Undo toast immediately.
    page.evaluate(
        """async () => {
          const { toast } = await import('/static/util.js');
          toast('boom', 'error');
        }"""
    )
    expect(page.locator("#toastMessage")).to_have_text("boom")

    # A second distinct error toast queues behind the active sticky error toast.
    page.evaluate(
        """async () => {
          const { toast } = await import('/static/util.js');
          toast('zap', 'error');
        }"""
    )
    page.wait_for_timeout(150)
    expect(page.locator("#toastMessage")).to_have_text("boom")

    # Dismissing reveals the queued error toast; old Undo toast never resurfaces.
    page.locator("#toastDismiss").click()
    expect(page.locator("#toastMessage")).to_have_text("zap")
    page.locator("#toastDismiss").click()
    expect(page.locator("#toast")).to_be_hidden()
    assert page_errors == []


def test_comma_folder_path_and_light_theme_contrast(
    page, live_dedupe_server: str, tmp_path: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    folder = tmp_path / "Photos, 2026"
    folder.mkdir()
    first = _create_textured_image(folder / "one.png", width=64, height=64)
    shutil.copyfile(first, folder / "two.png")

    page.goto(live_dedupe_server, wait_until="domcontentloaded")

    # Switch to Light theme and check setup chip contrast while scan setup is visible.
    while page.evaluate("document.documentElement.dataset.theme") != "light":
        page.locator("#btnTheme").click()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"

    opt_contrast = _element_contrast_ratio(page, "#optExactWrap span")
    assert opt_contrast >= 4.5, f"Expected >= 4.5:1 for #optExactWrap span, got {opt_contrast:.2f}"

    # Scan the comma-containing folder path without splitting it on ', '.
    page.locator("#paths").fill(str(folder))
    page.locator("#btnScan").click()

    expect(page.locator("#results")).to_be_visible(timeout=20_000)
    expect(page.locator("#countExact")).to_have_text("1", timeout=20_000)
    expect(page.locator(".group-item .badge.exact").first).to_be_visible()

    badge_contrast = _element_contrast_ratio(page, ".group-item .badge.exact")
    assert badge_contrast >= 4.5, (
        f"Expected >= 4.5:1 for .badge.exact in light theme, got {badge_contrast:.2f}"
    )
    assert page_errors == []
