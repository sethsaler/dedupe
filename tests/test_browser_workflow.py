"""Critical browser workflow against a real loopback Flask server."""

from __future__ import annotations

import shutil
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import expect
from werkzeug.serving import make_server

from dedupe.grouping import build_groups, build_no_human_groups
from dedupe.human_detection import human_detection_signature
from dedupe.models import FileRecord, MediaType, ScanResult
from dedupe.web.app import create_app


@pytest.fixture
def live_dedupe_server(tmp_path: Path):
    """Serve an isolated review session and always stop its server thread."""
    app = create_app(review_session_path=tmp_path / "review.json")
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "hash-cache.sqlite3")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    # Poll requests must not make server_close wait for a keep-alive timeout.
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


@pytest.fixture
def duplicate_images(tmp_path: Path) -> Path:
    media = tmp_path / "media"
    media.mkdir()
    first = media / "keeper.png"
    Image.new("RGB", (48, 32), (25, 100, 180)).save(first)
    shutil.copyfile(first, media / "duplicate.png")
    return media


@pytest.mark.e2e
def test_local_review_workflow(page, live_dedupe_server: str, duplicate_images: Path) -> None:
    page_errors: list[str] = []
    console_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )

    # The app polls status continuously, so network-idle is intentionally not a readiness signal.
    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    assert page.title() == "Dedupe — Media Duplicate Finder"

    # Start the actual scanner through the UI, without opening a native picker.
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)
    page.locator("#toast").filter(has_text="Done").wait_for(state="visible")
    page.locator(".group-item").first.click()
    page.locator("#members .card").first.wait_for(state="visible")

    # The default exact-match recommendation removes one file and always keeps one.
    assert page.locator("#members .card").count() == 2
    assert page.locator("#members .card.selected").count() == 1
    assert page.locator("#members .card.keep").count() == 1
    assert page.locator("#members .sel-cb:checked").count() == 1

    preview = page.locator("#members .thumb-wrap").first
    preview_box = preview.bounding_box()
    assert preview_box is not None
    assert preview_box["width"] / preview_box["height"] == pytest.approx(48 / 32, rel=0.02)

    preview.click()
    page.locator("#lightbox").wait_for(state="visible")
    page.locator("#lbClose").click()
    page.locator("#lightbox").wait_for(state="hidden")

    # Search and category filtering both update the browser-rendered group list.
    # (Search input is debounced; to_have_count auto-retries until it applies.)
    page.locator("#resultSearch").fill("does-not-exist")
    expect(page.locator(".group-item")).to_have_count(0)
    page.locator("#resultSearch").fill("duplicate.png")
    expect(page.locator(".group-item")).to_have_count(3)
    page.get_by_role("tab", name="Exact 1").click()
    expect(page.locator(".group-item")).to_have_count(1)
    assert page.locator(".group-item").count() == 1

    # Opening the exact-match action verifies the selection but cannot move either fixture
    # unless the user confirms.
    page.locator("#btnTrashExact").click()
    page.locator("#modalBackdrop").wait_for(state="visible")
    page.locator("#modalCancel").click()
    page.locator("#modalBackdrop").wait_for(state="hidden")
    assert sorted(path.name for path in duplicate_images.iterdir()) == [
        "duplicate.png",
        "keeper.png",
    ]
    assert page_errors == []
    assert console_errors == []


@pytest.mark.e2e
def test_low_resolution_review_uses_left_delete_and_right_keep(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)

    page.locator('.tab[data-kind="low_resolution"]').click()
    expect(page.locator(".group-item")).to_have_count(1)
    page.locator(".group-item").click()
    page.locator("#members .decision-card").wait_for(state="visible")
    assert page.locator("#members .decision-card").count() == 1

    page.keyboard.press("ArrowLeft")
    expect(page.locator("#detailMeta")).to_contain_text("1 reviewed")
    assert "1 marked Delete" in page.locator("#detailMeta").inner_text()

    page.keyboard.press("ArrowRight")
    expect(page.locator("#detailMeta")).to_contain_text("2 reviewed")
    assert "1 marked Delete" in page.locator("#detailMeta").inner_text()
    assert "0 remaining" in page.locator("#detailMeta").inner_text()

    # Revisit and correct the first decision without clearing the whole review.
    page.locator("#memberPagination .member-prev").click()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#detailMeta")).to_contain_text("0 marked Delete")
    assert "2 reviewed" in page.locator("#detailMeta").inner_text()

    # A Low-res decision does not become eligible for the duplicate actions,
    # but it does enable the Low-res + Random review action.
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#detailMeta")).to_contain_text("1 marked Delete")
    expect(page.locator("#btnTrashExact")).to_be_disabled()
    expect(page.locator("#btnTrashSimilar")).to_be_disabled()
    expect(page.locator("#btnTrashReview")).to_be_enabled()

    # The review action previews the quarantine split without moving anything.
    page.locator("#btnTrashReview").click()
    page.locator("#modalBackdrop").wait_for(state="visible")
    expect(page.locator("#modalTitle")).to_have_text(
        "Delete all selected Low-res + Random review files?"
    )
    expect(page.locator("#modalBody")).to_contain_text("_Dedupe Quarantine")
    expect(page.locator("#modalConfirm")).to_have_text("Move to Quarantine")
    page.locator("#modalCancel").click()
    page.locator("#modalBackdrop").wait_for(state="hidden")
    assert sorted(path.name for path in duplicate_images.iterdir()) == [
        "duplicate.png",
        "keeper.png",
    ]


@pytest.mark.e2e
def test_bulk_selection_and_advanced_filters(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)
    page.locator(".group-item").first.click()
    page.locator("#members .card").first.wait_for(state="visible")

    # A path glob narrows the sidebar and Clear filters resets every result-display filter.
    # (Text filters are debounced; to_have_count auto-retries until they apply.)
    page.locator("#advancedFilters summary").click()
    page.locator("#filterPathPattern").fill("*/no-such-folder/*")
    expect(page.locator(".group-item")).to_have_count(0)
    page.locator("#filterPathPattern").fill("*keeper*")
    expect(page.locator(".group-item")).to_have_count(3)
    page.locator("#resultSearch").fill("keeper")
    page.locator("#issuesOnly").check()
    page.locator("#btnClearFilters").click()
    expect(page.locator(".group-item")).to_have_count(3)
    assert page.locator("#resultSearch").input_value() == ""
    assert not page.locator("#issuesOnly").is_checked()

    page.locator('.tab[data-kind="exact"]').click()
    expect(page.locator(".group-item")).to_have_count(1)

    # Bulk operations always leave one member of a duplicate group behind.
    page.locator("#bulkPanel summary").click()
    page.locator("#btnBulkNone").click()
    page.locator("#toast").filter(has_text="Select none").wait_for()
    page.locator("#members .sel-cb").first.wait_for(state="visible")
    assert page.locator("#members .sel-cb:checked").count() == 0
    page.locator("#btnBulkAll").click()
    page.locator("#toast").filter(has_text="Select all").wait_for()
    expect(page.locator("#members .sel-cb:checked")).to_have_count(1)
    assert page.locator("#members .card.keep").count() == 1

    # The review sheet states how long its server-issued preview stays valid.
    page.locator("#btnTrashExact").click()
    page.locator("#modalBackdrop").wait_for(state="visible")
    assert "preview valid for" in page.locator("#modalValidity").inner_text()
    page.locator("#modalCancel").click()
    page.locator("#modalBackdrop").wait_for(state="hidden")
    assert sorted(path.name for path in duplicate_images.iterdir()) == [
        "duplicate.png",
        "keeper.png",
    ]
    assert page_errors == []


@pytest.mark.e2e
def test_similar_cards_show_percentage_and_use_a_separate_bulk_scope(
    page, tmp_path: Path
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    records = []
    for name, phash in (
        ("keeper.png", "0000000000000000"),
        ("similar.png", "0000000000000001"),
    ):
        path = media / name
        Image.new("RGB", (48, 32), (25, 100, 180)).save(path)
        stat = path.stat()
        records.append(FileRecord(
            path=str(path),
            size=stat.st_size,
            mtime=stat.st_mtime,
            media_type=MediaType.IMAGE,
            extension=".png",
            width=48,
            height=32,
            phash=phash,
            dhash="0000000000000000",
            tile_phashes="t2:" + ",".join(["0000000000000000"] * 5),
        ))
    groups = build_groups([], [records])
    groups[0].suggested_keep = records[0].path
    app = create_app(
        ScanResult(roots=[str(media)], files=records, groups=groups),
        review_session_path=tmp_path / "review.json",
    )

    with _serve_app(app) as url:
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#results").wait_for(state="visible", timeout=10_000)

        expect(page.locator("#btnTrashExact")).to_have_text(
            "Delete All Selected Exact Matches"
        )
        expect(page.locator("#btnTrashSimilar")).to_have_text(
            "Delete All Selected Similar Matches"
        )
        expect(page.locator("#btnTrashReview")).to_have_text(
            "Delete All Selected Low-res + Random"
        )
        page.locator('.tab[data-kind="similar"]').click()
        page.locator(".group-item").click()
        expect(page.locator("#members .evidence")).to_contain_text([
            "100% Similar",
            "99.8% Similar",
        ])
        expect(page.locator("#members .evidence").last).to_contain_text("not a probability")
        page.locator("#btnTrashSimilar").click()
        expect(page.locator("#modalTitle")).to_have_text(
            "Delete all selected similar matches?"
        )
        page.locator("#modalCancel").click()


@contextmanager
def _serve_app(app):
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


@pytest.mark.e2e
def test_non_human_delete_is_one_click_and_undoable(page, tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    records = []
    # Mixed orientations: previews must still render at one uniform size.
    for index, (color, size) in enumerate(
        (((36, 128, 72), (72, 48)), ((110, 92, 28), (48, 72)))
    ):
        path = media / f"mixed-{index}.png"
        Image.new("RGB", size, color).save(path)
        stat = path.stat()
        records.append(
            FileRecord(
                path=str(path),
                size=stat.st_size,
                mtime=stat.st_mtime,
                media_type=MediaType.IMAGE,
                extension=".png",
                device=stat.st_dev,
                inode=stat.st_ino,
                mtime_ns=stat.st_mtime_ns,
                human_detection_status="no_person_detected",
                human_detection_signature=human_detection_signature(),
            )
        )
    result = ScanResult(
        roots=[str(media)],
        files=records,
        groups=build_no_human_groups(records),
    )
    newest = Path(max(records, key=lambda record: record.mtime_ns).path)
    app = create_app(result, review_session_path=tmp_path / "review.json")
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "hash-cache.sqlite3")

    with _serve_app(app) as url:
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#results").wait_for(state="visible", timeout=10_000)
        page.locator(".group-item").first.click()
        page.locator("#members .triage-card").first.wait_for(state="visible")
        assert page.locator("#members .card").count() == 2
        assert page.locator("#modalBackdrop").is_hidden()

        # Results auto-select the first group, then the click fetches it again.
        # Each selectGroup replaces the member DOM. Playwright bounding_box()
        # returns None on a detached node, so measure sizes in this wait —
        # a mid-wait re-render just retries instead of failing the assertion.
        page.wait_for_function(
            """() => {
              const wraps = [...document.querySelectorAll("#members .triage-card .thumb-wrap")];
              if (wraps.length !== 2) return false;
              const boxes = wraps.map((el) => el.getBoundingClientRect());
              return (
                boxes.every((box) => box.width > 0 && box.height > 0)
                && boxes[0].width === boxes[1].width
                && boxes[0].height === boxes[1].height
              );
            }"""
        )

        page.locator("#members .card-actions .delete-candidate").first.click()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        page.locator("#toast").filter(has_text="Moved").wait_for()
        expect(page.locator("#toastAction")).to_be_visible()
        # The trashed card stays in place as a placeholder so the grid never reflows.
        expect(page.locator("#members .card")).to_have_count(2)
        expect(page.locator("#members .card.deleted")).to_have_count(1)
        expect(page.locator("#members .deleted-preview")).to_contain_text("Moved to Trash")
        assert not newest.exists()

        page.locator("#toastAction").click()
        page.locator("#toast").filter(has_text="restored").wait_for()
        expect(page.locator("#members .card")).to_have_count(2)
        expect(page.locator("#members .card.deleted")).to_have_count(0)
        assert newest.exists()

        page.locator("#members .card .name").nth(1).click()
        page.keyboard.press("d")
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        page.locator("#toast").filter(has_text="Moved").wait_for()
        expect(page.locator("#members .card")).to_have_count(2)
        expect(page.locator("#members .card.deleted")).to_have_count(1)


@pytest.mark.e2e
def test_a_shortcut_opens_action_sheet_and_enter_respects_focus(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)
    page.locator("#toast").filter(has_text="Done").wait_for(state="visible")
    page.locator(".group-item").first.click()
    page.locator("#members .card").first.wait_for(state="visible")

    # `a` opens the same Trash preview sheet as the primary action-bar button.
    page.keyboard.press("a")
    page.locator("#modalBackdrop").wait_for(state="visible")

    # The sheet opens with focus on Cancel (the safe default), so a stray Enter
    # cancels instead of confirming — nothing moves.
    assert page.evaluate("document.activeElement.id") == "modalCancel"
    page.keyboard.press("Enter")
    page.locator("#modalBackdrop").wait_for(state="hidden")
    page.wait_for_timeout(500)
    assert sorted(path.name for path in duplicate_images.iterdir()) == [
        "duplicate.png",
        "keeper.png",
    ]

    # Enter confirms only when the Confirm button itself has focus.
    page.locator("#btnTrashExact").click()
    page.locator("#modalBackdrop").wait_for(state="visible")
    page.locator("#modalConfirm").focus()
    assert page.evaluate("document.activeElement.id") == "modalConfirm"
    page.keyboard.press("Enter")
    # "1 ok" is the execute result, not the earlier "Done — …" scan-complete toast.
    page.locator("#toast").filter(has_text="1 ok").wait_for(state="visible")
    assert len(list(duplicate_images.iterdir())) == 1
    assert page_errors == []


@pytest.mark.e2e
def test_keyboard_help_lists_a_shortcut(page, live_dedupe_server: str) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.keyboard.press("?")
    page.locator("#helpBackdrop").wait_for(state="visible")
    expect(
        page.locator("#helpBackdrop .shortcuts dt").get_by_text("a", exact=True)
    ).to_be_visible()
    expect(page.locator("#helpBackdrop .shortcuts")).to_contain_text("Preview Trash")
    page.keyboard.press("Escape")
    page.locator("#helpBackdrop").wait_for(state="hidden")
    assert page_errors == []
