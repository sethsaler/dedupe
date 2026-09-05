"""Critical browser workflow against a real loopback Flask server."""

from __future__ import annotations

import re
import shutil
import subprocess
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
    # The setup form folds into its slim bar so the results get the screen.
    expect(page.locator("#scanPanel")).to_have_class("scan-panel collapsed")
    assert page.locator("#scanCollapse").get_attribute("aria-expanded") == "false"
    assert page.locator("#scanCollapsePaths").inner_text() == str(duplicate_images)
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
    # Exact, Low-res, Random, and the All-Files browse group all contain it.
    expect(page.locator(".group-item")).to_have_count(4)
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
def test_empty_results_offer_recovery_without_changing_selections(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#toast").filter(has_text="Done").wait_for(state="visible", timeout=20_000)
    exact_tab = page.locator('.tab[data-kind="exact"]')
    exact_tab.click()
    page.locator(".group-item").first.click()
    expect(page.locator("#members .sel-cb:checked")).to_have_count(1)
    expect(page.locator("#filteredCount")).to_have_text("1 of 1 groups shown")
    page.locator("#resultSort").select_option("date")

    page.locator("#resultSearch").fill("not-a-file")
    empty = page.locator(".group-empty")
    expect(empty).to_contain_text("No matching groups")
    expect(page.locator("#filteredCount")).to_have_text("0 of 1 groups shown")
    # Keyboard activation restores results and moves focus out of the removed button.
    empty.get_by_role("button", name="Clear filters").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#resultSearch")).to_be_focused()
    expect(page.locator(".group-item")).to_have_count(1)
    expect(exact_tab).to_have_attribute("aria-selected", "true")
    expect(page.locator("#resultSort")).to_have_value("date")
    expect(page.locator("#members .sel-cb:checked")).to_have_count(1)

    page.locator("#advancedFilters summary").click()
    page.locator("#filterMinWidth").fill("99999")
    expect(empty).to_contain_text("No matching groups")
    empty.get_by_role("button", name="Clear filters").click()
    expect(page.locator("#filterMinWidth")).to_have_value("")
    expect(page.locator(".group-item")).to_have_count(1)
    expect(page.locator("#members .sel-cb:checked")).to_have_count(1)

    # A genuinely empty category should not imply that clearing filters will help.
    page.locator('.tab[data-kind="faces"]').click()
    expect(empty).to_contain_text("No groups in this category")
    expect(empty.get_by_role("button", name="Clear filters")).to_have_count(0)
    empty.get_by_role("button", name="View all categories").click()
    all_tab = page.locator('.tab[data-kind="all"]')
    expect(all_tab).to_be_focused()
    expect(all_tab).to_have_attribute("aria-selected", "true")
    expect(page.locator(".group-item")).to_have_count(4)
    exact_tab.click()
    page.locator(".group-item").first.click()
    expect(page.locator("#members .sel-cb:checked")).to_have_count(1)


@pytest.mark.e2e
def test_lingering_hover_shows_a_full_image_preview(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)
    page.locator(".group-item").first.click()
    thumb = page.locator("#members .thumb-wrap").first
    thumb.wait_for(state="visible")

    # A passing hover under a second never opens the quick-look.
    thumb.hover()
    page.wait_for_timeout(400)
    assert page.locator("#hoverPreview").is_hidden()

    # ...but lingering past one second shows the full-image preview, which then
    # swaps the cached thumbnail for the large preview variant.
    expect(page.locator("#hoverPreview")).to_be_visible(timeout=2_000)
    expect(page.locator("#hoverPreviewImage")).to_have_attribute(
        "src", re.compile(r"variant=preview")
    )

    # Moving off the thumbnail dismisses it.
    page.locator("#detailTitle").hover()
    expect(page.locator("#hoverPreview")).to_be_hidden()

    # Escape dismisses it too — and dismissal sticks until the pointer leaves
    # and re-enters the thumbnail.
    thumb.hover()
    expect(page.locator("#hoverPreview")).to_be_visible(timeout=2_000)
    page.keyboard.press("Escape")
    expect(page.locator("#hoverPreview")).to_be_hidden()

    # Clicking through the quick-look still opens the real lightbox.
    page.locator("#detailTitle").hover()
    thumb.hover()
    expect(page.locator("#hoverPreview")).to_be_visible(timeout=2_000)
    thumb.click()
    page.locator("#lightbox").wait_for(state="visible")
    expect(page.locator("#hoverPreview")).to_be_hidden()
    page.locator("#lbClose").click()

    assert page_errors == []


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

    # ↑ steps back to the previous candidate without changing any decision,
    # and ↓ steps forward again.
    page.keyboard.press("ArrowUp")
    expect(page.locator("#memberPagination .member-page-summary")).to_have_text("1 of 2")
    expect(page.locator("#detailMeta")).to_contain_text("2 reviewed")
    page.keyboard.press("ArrowDown")
    expect(page.locator("#memberPagination .member-page-summary")).to_have_text("2 of 2")

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
def test_executed_trash_can_be_undone_from_the_result_toast(
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
    # Let the scan's own "Done" toast clear so it cannot swallow the click.
    page.locator("#toast").wait_for(state="hidden", timeout=10_000)

    # Execute the exact-match Trash for real; the fixture lands in the Trash.
    page.locator("#btnTrashExact").click()
    page.locator("#modalBackdrop").wait_for(state="visible")
    expect(page.locator("#modalConfirm")).to_have_text("Move to Trash")
    page.locator("#modalConfirm").click()
    page.locator("#toast").filter(has_text="Done").wait_for(state="visible")
    # The keeper ranking decides which name survives; exactly one file goes.
    assert len(list(duplicate_images.iterdir())) == 1

    # The result toast carries a sticky Undo; pressing it opens the restore
    # sheet, which previews and then moves the file back to its original path.
    expect(page.locator("#toastAction")).to_be_visible()
    page.locator("#toastAction").click()
    page.locator("#modalBackdrop").wait_for(state="visible")
    expect(page.locator("#modalTitle")).to_have_text("Restore 1 file?")
    expect(page.locator("#modalBody")).to_contain_text("next scan")
    page.locator("#modalConfirm").click()
    page.locator("#toast").filter(has_text="Restored 1 file").wait_for(state="visible")
    assert sorted(path.name for path in duplicate_images.iterdir()) == [
        "duplicate.png",
        "keeper.png",
    ]

    # The review is not re-populated by the restore: the dissolved exact group
    # stays gone until a rescan.
    expect(page.locator('.tab[data-kind="exact"]')).to_contain_text("0")
    assert page_errors == []


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
    # Exact, Low-res, Random, and the All-Files browse group all contain it.
    expect(page.locator(".group-item")).to_have_count(4)
    page.locator("#resultSearch").fill("keeper")
    page.locator("#issuesOnly").check()
    page.locator("#btnClearFilters").click()
    expect(page.locator(".group-item")).to_have_count(4)
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
        # Let the tab filter settle; the default All view also lists the
        # All-Files browse group, so an unsettled list has more than one row.
        expect(page.locator(".group-item")).to_have_count(1)
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
def test_all_files_review_trashes_uncategorized_files_from_the_lightbox(
    page, tmp_path: Path
) -> None:
    """The Files tab sifts a whole folder: arrows step, d trashes, undo restores."""
    media = tmp_path / "media"
    media.mkdir()
    records = []
    for index, color in enumerate(((36, 128, 72), (110, 92, 28), (180, 60, 60))):
        path = media / f"plain-{index}.png"
        Image.new("RGB", (48, 32), color).save(path)
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
            )
        )
    # No detector category matched: the result carries no review groups at all,
    # and the app still synthesizes the per-folder All-Files browse group.
    result = ScanResult(roots=[str(media)], files=records, groups=[])
    app = create_app(result, review_session_path=tmp_path / "review.json")
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "hash-cache.sqlite3")

    with _serve_app(app) as url:
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#results").wait_for(state="visible", timeout=10_000)
        page.get_by_role("tab", name="Files 3").click()
        page.locator(".group-item").first.click()
        page.locator("#members .triage-card").first.wait_for(state="visible")
        assert page.locator("#members .card").count() == 3
        # The sidebar row names the scanned folder; the detail title does too.
        expect(page.locator(".badge.all_files")).to_have_text("all files")
        expect(page.locator(".group-item")).to_contain_text("media")
        expect(page.locator("#detailTitle")).to_contain_text("All files")
        expect(page.locator("#detailTitle")).to_contain_text("media")

        # Cards are triage-style: one-click Trash, no selection checkboxes.
        expect(page.locator("#members .card-actions .delete-candidate")).to_have_count(3)
        expect(page.locator("#members .sel-cb")).to_have_count(0)

        page.locator("#members .thumb-wrap").first.click()
        page.locator("#lightbox").wait_for(state="visible")
        expect(page.locator("#lbCounter")).to_have_text("1 / 3")
        expect(page.locator("#lbDelete")).to_be_visible()

        page.keyboard.press("ArrowRight")
        expect(page.locator("#lbCounter")).to_have_text("2 / 3")
        victim = Path(page.locator("#lbMeta").inner_text())

        page.keyboard.press("d")
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        page.locator("#toast").filter(has_text="Moved").wait_for()
        assert not victim.exists()
        # The lightbox auto-advances to the next file and keeps going.
        expect(page.locator("#lightbox")).to_be_visible()
        expect(page.locator("#lbCounter")).to_have_text("2 / 2")
        page.keyboard.press("Escape")
        page.locator("#lightbox").wait_for(state="hidden")
        expect(page.locator("#members .card.deleted")).to_have_count(1)

        page.locator("#toastAction").click()
        page.locator("#toast").filter(has_text="restored").wait_for()
        assert victim.exists()
        expect(page.locator("#members .card.deleted")).to_have_count(0)


@pytest.mark.e2e
def test_all_files_lightbox_sifts_across_pages_sorts_and_reveals(
    page, tmp_path: Path
) -> None:
    """The Files lightbox ignores the 50-card page edge; sorting and Reveal work mid-sift."""
    media = tmp_path / "media"
    media.mkdir()
    records = []
    for index in range(55):
        # One deliberately larger file so "Largest first" has a clear winner.
        dimensions = (64, 64) if index == 7 else (16, 16)
        path = media / f"file-{index:02d}.png"
        Image.new("RGB", dimensions, (index % 256, 100, 150)).save(path)
        stat = path.stat()
        records.append(
            FileRecord(
                path=str(path),
                size=stat.st_size,
                mtime=stat.st_mtime + index,
                media_type=MediaType.IMAGE,
                extension=".png",
                device=stat.st_dev,
                inode=stat.st_ino,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    result = ScanResult(roots=[str(media)], files=records, groups=[])
    app = create_app(result, review_session_path=tmp_path / "review.json")
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "hash-cache.sqlite3")

    with _serve_app(app) as url:
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#results").wait_for(state="visible", timeout=10_000)
        page.get_by_role("tab", name="Files 55").click()
        page.locator(".group-item").first.click()
        page.locator("#members .triage-card").first.wait_for(state="visible")
        # The grid still pages at 50…
        assert page.locator("#members .card").count() == 50
        expect(page.locator("#memberPagination .member-page-summary")).to_have_text(
            "1–50 of 55"
        )

        # …but the lightbox sifts the whole group without stopping at the edge.
        page.locator("#members .thumb-wrap").first.click()
        page.locator("#lightbox").wait_for(state="visible")
        expect(page.locator("#lbCounter")).to_have_text("1 / 55")
        for _ in range(50):
            page.keyboard.press("ArrowRight")
        expect(page.locator("#lbCounter")).to_have_text("51 / 55")
        victim = Path(page.locator("#lbMeta").inner_text())
        page.keyboard.press("d")
        page.locator("#toast").filter(has_text="Moved").wait_for()
        assert not victim.exists()
        expect(page.locator("#lbCounter")).to_have_text("51 / 54")

        # Undo with the lightbox still open jumps back to the restored file.
        # (The toast renders under the overlay, so click it via the DOM.)
        page.evaluate("document.querySelector('#toastAction').click()")
        page.locator("#toast").filter(has_text="restored").wait_for()
        assert victim.exists()
        expect(page.locator("#lbCounter")).to_have_text("51 / 55")
        expect(page.locator("#lbMeta")).to_contain_text(victim.name)
        page.keyboard.press("Escape")
        page.locator("#lightbox").wait_for(state="hidden")

        # Largest first surfaces the space hog.
        page.locator("#memberSort").select_option("largest")
        expect(page.locator("#members .card .name").first).to_have_text("file-07.png")

        # `r` reveals the current file in Finder without leaving the lightbox.
        revealed: list[str] = []

        def fake_reveal(route) -> None:
            revealed.append(route.request.url)
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"path": "x", "exists": true}',
            )

        page.route("**/api/reveal*", fake_reveal)
        page.locator("#members .thumb-wrap").first.click()
        page.locator("#lightbox").wait_for(state="visible")
        expect(page.locator("#lbMeta")).to_contain_text("file-07.png")
        page.keyboard.press("r")
        for _ in range(50):
            if revealed:
                break
            page.wait_for_timeout(100)
        assert revealed and "file-07.png" in revealed[0] and "open=1" in revealed[0]
        page.keyboard.press("Escape")
        page.locator("#lightbox").wait_for(state="hidden")


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


@pytest.mark.e2e
def test_lightbox_shows_metadata_and_toggles_selection(
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
    assert page.locator("#members .card.selected").count() == 1

    # Open the lightbox on the selected (marked-for-removal) card.
    selected_name = page.locator("#members .card.selected .name").inner_text()
    page.locator("#members .card.selected .thumb-wrap").click()
    page.locator("#lightbox").wait_for(state="visible")

    # The lightbox shows the file's metadata, not just its path.
    expect(page.locator("#lbMeta")).to_contain_text(selected_name)
    expect(page.locator("#lbDetails")).to_contain_text("48×32")
    expect(page.locator("#lbDetails")).to_contain_text("B")

    # The removal toggle reflects and changes the group selection.
    const_select = page.locator("#lbSelect")
    expect(const_select).to_be_visible()
    expect(const_select).to_have_attribute("aria-pressed", "true")
    expect(const_select).to_have_text("Marked for removal")
    const_select.click()
    expect(const_select).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#members .card.selected")).to_have_count(0)

    # `d` toggles removal for duplicate groups (it is not a no-op here).
    page.keyboard.press("d")
    expect(const_select).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#members .card.selected")).to_have_count(1)
    # The group always keeps one file; the keeper card is never selected away.
    expect(page.locator("#members .card.keep")).to_have_count(1)

    page.keyboard.press("Escape")
    page.locator("#lightbox").wait_for(state="hidden")
    assert page_errors == []


@pytest.mark.e2e
@pytest.mark.parametrize("viewport", [(1280, 800), (1000, 500), (390, 844)])
@pytest.mark.parametrize("dimensions", [(1600, 900), (900, 1600), (2400, 400)])
@pytest.mark.parametrize("kind", ["image", "compare", "video"])
def test_lightbox_fits_media_without_cropping(
    page, live_dedupe_server: str, tmp_path: Path, viewport, dimensions, kind
) -> None:
    """Media fits the space left by controls, including wrapped mobile text."""
    width, height = dimensions
    image_path = tmp_path / "preview.png"
    image = Image.new("RGB", dimensions, "steelblue")
    # Distinct edges make clipping visible in browser screenshots.
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width - 1, height - 1), outline="gold", width=12)
    image.save(image_path)
    page.route("**/api/thumbnail?*", lambda route: route.fulfill(path=image_path))
    if kind == "video":
        if not shutil.which("ffmpeg"):
            pytest.skip("ffmpeg is required for the video layout fixture")
        video_path = tmp_path / "preview.webm"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-loop", "1", "-i", str(image_path),
             "-t", "1", "-r", "1", "-c:v", "libvpx", str(video_path)],
            check=True, capture_output=True, timeout=30,
        )
        page.route("**/api/media?*", lambda route: route.fulfill(path=video_path))

    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    page.emulate_media(reduced_motion="reduce")
    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.evaluate("""async ({kind, width, height}) => {
        const {state} = await import('/static/state.js');
        const {openLightbox} = await import('/static/lightbox.js');
        state.lightboxItems = [{
            path: '/photos/a-long-folder-name/another-folder/portrait-or-landscape-preview.png',
            mediaType: kind === 'video' ? 'video' : 'image',
            kind: kind === 'compare' ? 'similar' : 'all_files',
            keeper: '/photos/keeper.png', width, height, size: 123456,
        }];
        openLightbox(0);
    }""", {"kind": kind, "width": width, "height": height})
    selector = "#lbVideo" if kind == "video" else "#lbImage"
    page.wait_for_function("""selector => {
        const media = document.querySelector(selector);
        return media.tagName === 'VIDEO' ? media.readyState >= 2 : media.naturalWidth > 0;
    }""", arg=selector)
    media = page.locator(selector)
    box = media.bounding_box()
    assert box is not None
    assert box["width"] / box["height"] == pytest.approx(width / height, rel=0.01)
    assert box["width"] > 0 and box["height"] > 0
    for target in [selector, "#lbMeta", "#lbReveal", "#lbClose"]:
        bounds = page.locator(target).bounding_box()
        assert bounds is not None
        assert bounds["x"] >= 0 and bounds["y"] >= 0
        assert bounds["x"] + bounds["width"] <= viewport[0] + 1
        assert bounds["y"] + bounds["height"] <= viewport[1] + 1
    tools = page.locator("#lbVideoTools" if kind == "video" else "#lbStageTools")
    assert box["y"] + box["height"] <= tools.bounding_box()["y"]
    expect(media).to_have_css("border-radius", "0px")
    expect(media).to_have_css("box-shadow", "none")
    if kind == "compare":
        expect(page.locator("#lbKeeperImage")).to_be_visible()
        expect(page.locator("#lbCompareTools")).to_be_visible()
    if kind == "image":
        page.locator("#lbZoom").click()
        expect(media).to_have_attribute("src", re.compile(r"variant=full"))
        stack = page.locator("#lbImageStack")
        assert stack.evaluate("""el =>
            el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth
        """)
        assert stack.bounding_box()["y"] + stack.bounding_box()["height"] <= (
            tools.bounding_box()["y"]
        )
        page.locator("#lbZoom").click()
        expect(media).to_have_attribute("src", re.compile(r"variant=preview"))


@pytest.mark.e2e
def test_lightbox_zoom_swaps_to_full_resolution(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)
    page.locator(".group-item").first.click()
    page.locator("#members .card .thumb-wrap").first.click()
    page.locator("#lightbox").wait_for(state="visible")

    expect(page.locator("#lbZoom")).to_be_visible()
    page.keyboard.press("z")
    expect(page.locator("#lbImageStack")).to_have_class("lb-image-stack zoomed")
    expect(page.locator("#lbZoom")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#lbImage")).to_have_attribute("src", re.compile(r"variant=full"))

    page.keyboard.press("z")
    expect(page.locator("#lbImageStack")).not_to_have_class("lb-image-stack zoomed")
    page.keyboard.press("Escape")
    page.locator("#lightbox").wait_for(state="hidden")
    assert page_errors == []


@pytest.mark.e2e
def test_error_toasts_persist_until_dismissed(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)
    page.locator(".group-item").first.click()
    page.locator("#members .card").first.wait_for(state="visible")

    page.route(
        "**/api/selection",
        lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"error": "boom"}'
        ),
    )
    page.locator("#members .sel-cb").first.click()
    page.locator("#toast").filter(has_text="boom").wait_for(state="visible")
    # An error must not vanish on a timer (plain toasts dismiss in ~3.4 s).
    page.wait_for_timeout(4200)
    expect(page.locator("#toast").filter(has_text="boom")).to_be_visible()
    page.locator("#toastDismiss").click()
    page.locator("#toast").wait_for(state="hidden")


@pytest.mark.e2e
def test_tabs_and_group_list_are_keyboard_navigable(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)

    # Arrow keys move between tabs (roving tabindex, activate on move).
    # Each switch reloads the list asynchronously; the filtered-count text is
    # written synchronously inside that reload, so it is the settle signal.
    page.locator('.tab[data-kind="all"]').focus()
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.dataset.kind") == "exact"
    expect(page.locator('.tab[data-kind="exact"]')).to_have_attribute("aria-selected", "true")
    expect(page.locator("#filteredCount")).to_have_text("1 of 1 groups shown")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.dataset.kind") == "similar"
    expect(page.locator("#filteredCount")).to_have_text("0 of 0 groups shown")
    page.keyboard.press("Home")
    assert page.evaluate("document.activeElement.dataset.kind") == "all"
    expect(page.locator("#filteredCount")).to_have_text("4 of 4 groups shown")
    # The auto-select marks its group item active synchronously at start; once
    # it exists, a j-initiated selection supersedes it and lands the focus.
    page.locator(".group-item.active").wait_for(state="attached")

    # j/k navigation moves real focus onto the group items (selection is
    # async — poll until focus lands). wait_for_function's string predicate
    # needs eval, which the app's CSP forbids; page.evaluate is unaffected.
    page.keyboard.press("j")
    focused = False
    for _ in range(50):
        focused = page.evaluate("document.activeElement?.classList?.contains('group-item')")
        if focused:
            break
        page.wait_for_timeout(100)
    assert focused
    assert page.evaluate(
        "document.activeElement.getAttribute('aria-current')"
    ) == "true"
    assert page_errors == []


@pytest.mark.e2e
def test_review_action_shortcut_and_card_reveal_from_the_keyboard(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)

    # Stage one Low-res deletion, then open its action from the keyboard.
    page.locator('.tab[data-kind="low_resolution"]').click()
    expect(page.locator(".group-item")).to_have_count(1)
    page.locator(".group-item").click()
    page.locator("#members .decision-card").wait_for(state="visible")
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#detailMeta")).to_contain_text("1 marked Delete")
    expect(page.locator("#btnTrashReview")).to_be_enabled()

    page.keyboard.press("Shift+D")
    page.locator("#modalBackdrop").wait_for(state="visible")
    expect(page.locator("#modalTitle")).to_have_text(
        "Delete all selected Low-res + Random review files?"
    )
    page.locator("#modalCancel").click()
    page.locator("#modalBackdrop").wait_for(state="hidden")

    # r on a focused card fires the same Reveal request as the card's button.
    reveal_requests: list[str] = []
    page.route(
        "**/api/reveal*",
        lambda route: (
            reveal_requests.append(route.request.url),
            route.fulfill(status=200, content_type="application/json", body="{}"),
        ),
    )
    page.locator('.tab[data-kind="exact"]').click()
    expect(page.locator(".group-item")).to_have_count(1)
    page.locator(".group-item").click()
    page.locator("#members .card").first.wait_for(state="visible")
    page.keyboard.press("ArrowRight")
    page.locator("#members .card.focused").wait_for(state="attached")
    page.keyboard.press("r")
    expect(page.locator("#members .card.focused")).to_have_count(1)
    for _ in range(50):
        if reveal_requests:
            break
        page.wait_for_timeout(100)
    assert len(reveal_requests) == 1
    assert "open=1" in reveal_requests[0]
    assert page_errors == []


@pytest.mark.e2e
def test_scan_setup_hints_and_exclusion_check(
    page, live_dedupe_server: str, tmp_path: Path
) -> None:
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    Image.new("RGB", (48, 32), (25, 100, 180)).save(folder_a / "a.png")
    Image.new("RGB", (48, 32), (25, 100, 180)).save(folder_b / "b.png")

    page.goto(live_dedupe_server, wait_until="domcontentloaded")

    # Two folders with parallel streams (the default) surface the
    # no-cross-folder-dedup hint; turning streams off hides it again.
    expect(page.locator("#crossFolderHint")).to_be_hidden()
    page.locator("#paths").fill(f"{folder_a}, {folder_b}")
    expect(page.locator("#crossFolderHint")).to_be_visible()
    page.locator("#optsToggle").click()
    # Chip checkboxes are visually hidden behind their label; force the toggle.
    page.locator("#optParallel").uncheck(force=True)
    expect(page.locator("#crossFolderHint")).to_be_hidden()

    # The exclusion check reports what each glob matches, flagging dead ones.
    page.locator("#exclusions").fill("a*, zzz-*")
    page.locator("#btnCheckExclusions").click()
    expect(page.locator("#exclusionsCheckResult")).to_contain_text("✓ a*")
    expect(page.locator("#exclusionsCheckResult")).to_contain_text("zzz-* — matches nothing")


@pytest.mark.e2e
def test_empty_path_scan_is_rejected(page, live_dedupe_server: str) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")

    # Empty field: the client refuses before any request leaves the browser.
    page.locator("#btnScan").click()
    expect(page.locator("#toastMessage")).to_have_text("Enter at least one folder path")
    assert page.evaluate("document.activeElement.id") == "paths"
    assert page.locator("#results").is_hidden()
    assert page.locator("#emptyState").is_visible()
    assert page.locator("#progressWrap").is_hidden()
    assert page.locator(".group-item").count() == 0

    # Whitespace-only input trims to the same refusal.
    page.locator("#paths").fill("   ")
    page.locator("#btnScan").click()
    expect(page.locator("#toastMessage")).to_have_text("Enter at least one folder path")
    assert page.locator("#results").is_hidden()

    # The server-side guard carries the checklist's "paths required" error.
    token = page.evaluate("document.querySelector('meta[name=dedupe-token]').content")
    response = page.request.post(
        f"{live_dedupe_server}/api/scan",
        data={"paths": []},
        headers={"X-Dedupe-Token": token},
    )
    assert response.status == 400
    assert response.json()["error"] == "paths required"
    assert page.locator("#results").is_hidden()
    assert page_errors == []


@pytest.mark.e2e
def test_attention_navigation_and_space_u_shortcuts(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)
    page.locator("#toast").filter(has_text="Done").wait_for(state="visible")
    page.locator("#toast").wait_for(state="hidden", timeout=10_000)

    # The Exact tab's one group is complete under the suggested selection, so
    # no shown group needs attention — ] says so instead of moving.
    page.locator('.tab[data-kind="exact"]').click()
    expect(page.locator(".group-item")).to_have_count(1)
    page.keyboard.press("]")
    expect(page.locator("#toastMessage")).to_have_text("No shown groups need attention")

    # Space toggles the focused card's checkbox; u restores the suggestion.
    page.locator(".group-item").click()
    page.locator("#members .card").first.wait_for(state="visible")
    expect(page.locator("#members .card.selected")).to_have_count(1)
    selected_index = page.evaluate(
        "[...document.querySelectorAll('#members .card')]"
        ".findIndex((card) => card.classList.contains('selected'))"
    )
    page.keyboard.press("ArrowRight")
    if selected_index == 0:
        # Keeper-ranking tie edge: the selected card is the first one.
        page.keyboard.press("ArrowLeft")
    page.locator("#members .card.focused").wait_for(state="attached")
    assert page.evaluate(
        "document.querySelector('#members .card.focused').classList.contains('selected')"
    )
    page.keyboard.press("Space")
    expect(page.locator("#members .sel-cb:checked")).to_have_count(0)
    expect(page.locator("#members .card.selected")).to_have_count(0)
    assert not page.evaluate(
        "document.querySelector('#members .card.focused .sel-cb').checked"
    )
    page.keyboard.press("u")
    page.locator("#toast").filter(has_text="Suggested selection applied").wait_for(
        state="visible"
    )
    expect(page.locator("#members .card.selected")).to_have_count(1)
    expect(page.locator("#members .card.keep")).to_have_count(1)
    assert not page.evaluate(
        "document.querySelector('#members .card.keep .sel-cb').checked"
    )

    # The unreviewed Low-res group needs attention: ] lands on it, and with a
    # single candidate the next ] wraps back onto the same group. (Exact and
    # Low-res both list one group, so the badge — not the count — proves the
    # tab's list has re-rendered.)
    page.locator('.tab[data-kind="low_resolution"]').click()
    page.locator(".group-item .badge.low_resolution").wait_for(state="attached")
    for _ in range(2):
        page.keyboard.press("]")
        focused = False
        for _ in range(50):
            focused = page.evaluate(
                "document.activeElement?.classList?.contains('group-item')"
            )
            if focused:
                break
            page.wait_for_timeout(100)
        assert focused
        assert page.evaluate(
            "document.activeElement.getAttribute('aria-current')"
        ) == "true"
        # innerText is CSS-uppercased; textContent carries the raw badge text.
        assert "low-res" in page.evaluate("document.activeElement.textContent")
    assert page_errors == []


@pytest.mark.e2e
def test_lightbox_enter_wrap_focus_trap_and_esc(
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
    expect(page.locator("#members .card.selected")).to_have_count(1)

    # → focuses a member card; Enter opens the lightbox on it and focus moves
    # inside the overlay (onto the close control).
    page.keyboard.press("ArrowRight")
    page.locator("#members .card.focused").wait_for(state="attached")
    page.keyboard.press("Enter")
    page.locator("#lightbox").wait_for(state="visible")
    assert page.evaluate(
        "document.getElementById('lightbox').contains(document.activeElement)"
    )
    assert page.evaluate("document.activeElement.id") == "lbClose"

    # The lightbox opened on the second of two members; → wraps past the end.
    expect(page.locator("#lbCounter")).to_have_text("2 / 2")
    page.keyboard.press("ArrowRight")
    expect(page.locator("#lbCounter")).to_have_text("1 / 2")
    page.keyboard.press("ArrowRight")
    expect(page.locator("#lbCounter")).to_have_text("2 / 2")

    # Tab stays trapped inside the overlay no matter how often it cycles.
    for _ in range(6):
        page.keyboard.press("Tab")
        assert page.evaluate(
            "document.getElementById('lightbox').contains(document.activeElement)"
        )

    # Escape closes without side effects and returns focus to the card that
    # opened the lightbox; selections are untouched.
    page.keyboard.press("Escape")
    page.locator("#lightbox").wait_for(state="hidden")
    assert page.evaluate(
        "document.activeElement?.classList?.contains('thumb-wrap')"
    )
    assert page.evaluate(
        "document.activeElement.closest('.card')?.classList?.contains('focused')"
    )
    expect(page.locator("#members .card.selected")).to_have_count(1)
    assert page_errors == []


@pytest.mark.e2e
def test_stale_preview_re_previews_on_confirm(
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
    # Let the scan's own "Done" toast clear so it cannot swallow the click.
    page.locator("#toast").wait_for(state="hidden", timeout=10_000)

    page.locator("#btnTrashExact").click()
    page.locator("#modalBackdrop").wait_for(state="visible")
    expect(page.locator("#modalTitle")).to_have_text("Delete all selected exact matches?")
    assert "preview valid for" in page.locator("#modalValidity").inner_text()

    # A second tab on the same session moves the selection to the other file
    # behind the open sheet (deselect the pick, then select the sibling).
    tab2 = page.context.new_page()
    tab2_errors: list[str] = []
    tab2.on("pageerror", lambda error: tab2_errors.append(str(error)))
    tab2.goto(live_dedupe_server, wait_until="domcontentloaded")
    tab2.locator("#results").wait_for(state="visible", timeout=10_000)
    tab2.locator(".group-item").first.click()
    tab2.locator("#members .card").first.wait_for(state="visible")
    selected_index = tab2.evaluate(
        "[...document.querySelectorAll('#members .card')]"
        ".findIndex((card) => card.classList.contains('selected'))"
    )
    tab2.locator("#members .card.selected .sel-cb").click()
    expect(tab2.locator("#members .sel-cb:checked")).to_have_count(0)
    expect(tab2.locator("#groupSelectionSummary")).to_have_text("0 of 2 selected for removal")
    tab2.locator("#members .sel-cb").nth(1 - selected_index).click()
    expect(tab2.locator("#members .sel-cb:checked")).to_have_count(1)
    expect(tab2.locator("#groupSelectionSummary")).to_have_text("1 of 2 selected for removal")

    # Confirming with the stale token is refused; the client re-previews and
    # re-opens the sheet with re-verified numbers instead of moving anything.
    page.bring_to_front()
    page.locator("#modalConfirm").click()
    expect(page.locator("#toastMessage")).to_contain_text(
        "selection changed since the preview"
    )
    page.locator("#modalBackdrop").wait_for(state="visible")
    expect(page.locator("#modalTitle")).to_have_text("Delete all selected exact matches?")
    expect(page.locator("#modalBody .preview-notice")).to_contain_text("re-verified")
    assert sorted(path.name for path in duplicate_images.iterdir()) == [
        "duplicate.png",
        "keeper.png",
    ]
    page.locator("#modalCancel").click()
    page.locator("#modalBackdrop").wait_for(state="hidden")
    assert tab2_errors == []
    assert page_errors == []


@pytest.mark.e2e
def test_escape_discards_preview_and_reopening_renews_the_token(
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
    page.locator("#toast").wait_for(state="hidden", timeout=10_000)

    # Escape closes the sheet without moving anything; the preview dies with it.
    page.locator("#btnTrashExact").click()
    page.locator("#modalBackdrop").wait_for(state="visible")
    assert "preview valid for" in page.locator("#modalValidity").inner_text()
    page.keyboard.press("Escape")
    page.locator("#modalBackdrop").wait_for(state="hidden")
    assert sorted(path.name for path in duplicate_images.iterdir()) == [
        "duplicate.png",
        "keeper.png",
    ]

    # Reopening runs a fresh preview: the server-issued validity line is back.
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
def test_parallel_streams_toggle_controls_cross_folder_groups(
    page, live_dedupe_server: str, tmp_path: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    Image.new("RGB", (48, 32), (25, 100, 180)).save(folder_a / "same.png")
    shutil.copyfile(folder_a / "same.png", folder_b / "same.png")

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(f"{folder_a}, {folder_b}")
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)

    # Parallel streams (the default): one progress line per folder, and no
    # cross-folder exact group is found.
    expect(page.locator("#streamProgress .stream-row")).to_have_count(2)
    expect(page.locator("#countExact")).to_have_text("0")
    expect(page.locator("#countAll")).not_to_have_text("0")

    # Forcing one pool finds the cross-folder duplicate.
    page.locator("#scanCollapse").click()
    page.locator("#optsToggle").click()
    # Chip checkboxes are visually hidden behind their label; force the toggle.
    page.locator("#optParallel").uncheck(force=True)
    page.locator("#btnScan").click()
    expect(page.locator("#countExact")).to_have_text("1", timeout=20_000)
    # One-pool scans have no per-folder stream panel.
    expect(page.locator("#streamProgress")).to_be_hidden()
    page.locator('.tab[data-kind="exact"]').click()
    expect(page.locator(".group-item")).to_have_count(1)
    assert page_errors == []


@pytest.mark.e2e
def test_resume_banner_reports_pruned_files_and_discard_starts_clean(
    page, tmp_path: Path, duplicate_images: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    session_path = tmp_path / "review.json"

    # Scan once through the UI; completion persists the durable review session.
    app = create_app(review_session_path=session_path)
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "hash-cache.sqlite3")
    with _serve_app(app) as url:
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#paths").fill(str(duplicate_images))
        page.locator("#btnScan").click()
        page.locator("#actionBar").wait_for(state="visible", timeout=20_000)
        assert session_path.exists()

    # One scanned file changes on disk before the restart.
    Image.new("RGB", (64, 64), (200, 30, 30)).save(duplicate_images / "duplicate.png")

    # A new server over the same session file resumes without scanning and
    # reports the pruned file with its reason.
    resumed = create_app(review_session_path=session_path)
    resumed.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "hash-cache.sqlite3")
    with _serve_app(resumed) as url:
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#results").wait_for(state="visible", timeout=10_000)
        page.locator(".group-item").first.wait_for(state="attached")
        # The exact group lost a member below its two-file minimum.
        expect(page.locator("#countExact")).to_have_text("0")
        expect(page.locator("#countAll")).to_have_text("3")
        expect(page.locator("#sessionStatus")).to_be_visible()
        expect(page.locator("#sessionStatusText")).to_contain_text("Resumed review")
        expect(page.locator("#sessionStatusText")).to_contain_text("1 stale file pruned")
        expect(page.locator("#sessionPrunedSummary")).to_contain_text(
            "1 changed since the scan"
        )
        page.locator("#sessionPrunedSummary").click()
        expect(page.locator("#sessionPrunedList")).to_contain_text("duplicate.png")
        expect(page.locator("#sessionPrunedList")).to_contain_text("changed since scan")

        # Discard removes the durable state and returns to the empty setup.
        page.locator("#btnDiscardSession").click()
        page.locator("#modalBackdrop").wait_for(state="visible")
        expect(page.locator("#modalTitle")).to_have_text("Discard saved review?")
        page.locator("#modalConfirm").click()
        page.locator("#toast").filter(has_text="Saved review discarded").wait_for(
            state="visible"
        )
        assert not session_path.exists()
        expect(page.locator("#emptyState")).to_be_visible()
        expect(page.locator("#results")).to_be_hidden()
        expect(page.locator(".group-item")).to_have_count(0)
    assert page_errors == []


@pytest.mark.e2e
def test_selections_survive_a_server_restart(
    page, tmp_path: Path, duplicate_images: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    session_path = tmp_path / "review.json"

    # Scan, then undo the suggested selection (1 selected -> 0 selected).
    app = create_app(review_session_path=session_path)
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "hash-cache.sqlite3")
    with _serve_app(app) as url:
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#paths").fill(str(duplicate_images))
        page.locator("#btnScan").click()
        page.locator("#actionBar").wait_for(state="visible", timeout=20_000)
        page.locator('.tab[data-kind="exact"]').click()
        expect(page.locator(".group-item")).to_have_count(1)
        page.locator(".group-item").click()
        page.locator("#members .card").first.wait_for(state="visible")
        expect(page.locator("#members .sel-cb:checked")).to_have_count(1)
        page.locator("#members .card.selected .sel-cb").click()
        # The summary rewrites only after the selection POST (and its
        # synchronous session persist) has completed.
        expect(page.locator("#groupSelectionSummary")).to_have_text(
            "0 of 2 selected for removal"
        )

    # A new server over the same session file restores groups and selections.
    resumed = create_app(review_session_path=session_path)
    resumed.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "hash-cache.sqlite3")
    with _serve_app(resumed) as url:
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#results").wait_for(state="visible", timeout=10_000)
        page.locator('.tab[data-kind="exact"]').click()
        expect(page.locator(".group-item")).to_have_count(1)
        page.locator(".group-item").click()
        page.locator("#members .card").first.wait_for(state="visible")
        expect(page.locator("#members .sel-cb:checked")).to_have_count(0)
        expect(page.locator("#members .card.selected")).to_have_count(0)
        expect(page.locator("#groupSelectionSummary")).to_have_text(
            "0 of 2 selected for removal"
        )
    assert page_errors == []


@pytest.mark.e2e
def test_sticky_toast_queues_newer_toasts(
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
    page.locator("#toast").wait_for(state="hidden", timeout=10_000)

    # A sticky error toast stays until dismissed.
    selection_calls: list[str] = []

    def fail_selection(route) -> None:
        selection_calls.append(route.request.url)
        route.fulfill(status=500, content_type="application/json", body='{"error": "boom"}')

    page.route("**/api/selection", fail_selection)
    page.locator("#members .sel-cb").first.click()
    page.locator("#toast").filter(has_text="boom").wait_for(state="visible")

    # A repeat of the same failure collapses into the visible toast.
    page.locator("#members .sel-cb").nth(1).click()
    for _ in range(50):
        if len(selection_calls) >= 2:
            break
        page.wait_for_timeout(100)
    assert len(selection_calls) == 2
    expect(page.locator("#toastMessage")).to_have_text("boom")

    # A different failure queues behind the sticky toast, never replacing it.
    smart_select_calls: list[str] = []
    page.route(
        "**/api/smart-select",
        lambda route: (
            smart_select_calls.append(route.request.url),
            route.fulfill(status=500, content_type="application/json", body='{"error": "zap"}'),
        ),
    )
    # Move focus off the checkbox so the "u" shortcut is not treated as typing.
    page.locator("#detailTitle").click()
    page.keyboard.press("u")
    for _ in range(50):
        if smart_select_calls:
            break
        page.wait_for_timeout(100)
    assert smart_select_calls
    page.wait_for_timeout(200)  # the toast() call trails the failed response
    expect(page.locator("#toastMessage")).to_have_text("boom")

    # Dismissing the sticky toast reveals the queued one; the queue then drains
    # (the collapsed "boom" repeat does not resurface).
    page.locator("#toastDismiss").click()
    expect(page.locator("#toastMessage")).to_have_text("zap")
    page.locator("#toastDismiss").click()
    page.locator("#toast").wait_for(state="hidden")
    assert page_errors == []


@pytest.mark.e2e
def test_random_review_decision_mechanics(
    page, live_dedupe_server: str, duplicate_images: Path
) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)

    page.locator('.tab[data-kind="random_review"]').click()
    expect(page.locator(".group-item")).to_have_count(1)
    page.locator(".group-item").click()
    page.locator("#members .decision-card").wait_for(state="visible")
    assert page.locator("#members .decision-card").count() == 1

    # ← stages a Delete and steps to the next candidate…
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#detailMeta")).to_contain_text("1 reviewed")
    assert "1 marked Delete" in page.locator("#detailMeta").inner_text()
    expect(page.locator("#memberPagination .member-page-summary")).to_have_text("2 of 2")

    # …→ keeps it, and the drained pile enables the shared review action.
    page.keyboard.press("ArrowRight")
    expect(page.locator("#detailMeta")).to_contain_text("2 reviewed")
    assert "1 marked Delete" in page.locator("#detailMeta").inner_text()
    assert "0 remaining" in page.locator("#detailMeta").inner_text()
    expect(page.locator("#btnTrashReview")).to_be_enabled()
    page.locator("#toast").filter(has_text="Review complete").wait_for(state="visible")
    assert page_errors == []


@pytest.mark.e2e
def test_scan_streams_groups_and_cancel_restores_previous(
    page, live_dedupe_server: str, duplicate_images: Path, tmp_path: Path
) -> None:
    import os
    import time

    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    # Baseline scan completes fully; its results are what Cancel must restore.
    page.goto(live_dedupe_server, wait_until="domcontentloaded")
    page.locator("#paths").fill(str(duplicate_images))
    page.locator("#btnScan").click()
    page.locator("#actionBar").wait_for(state="visible", timeout=20_000)
    page.locator("#toast").filter(has_text="Done").wait_for(state="visible")
    page.locator("#toast").wait_for(state="hidden", timeout=10_000)
    expect(page.locator("#countExact")).to_have_text("1")

    # A bigger fixture: exact-duplicate pairs of random-noise PNGs, so groups
    # keep streaming in while the similar-image stage is still hashing.
    big = tmp_path / "big"
    big.mkdir()
    for index in range(125):
        image_path = big / f"noise-{index:03d}-a.png"
        Image.frombytes("RGB", (96, 96), os.urandom(96 * 96 * 3)).save(image_path)
        shutil.copyfile(image_path, big / f"noise-{index:03d}-b.png")

    # Start the second scan from the collapsed setup bar.
    page.locator("#scanCollapse").click()
    page.locator("#paths").fill(str(big))
    page.locator("#btnScan").click()

    # Groups stream into the sidebar before the scan completes. One evaluate
    # per poll keeps the count and the scanning flag race-free.
    streamed = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        snapshot = page.evaluate(
            """() => ({
                groups: document.querySelectorAll(".group-item").length,
                scanning: !document.getElementById("btnCancelScan").hidden,
            })"""
        )
        if snapshot["groups"] > 0:
            streamed = snapshot
            break
        page.wait_for_timeout(100)
    assert streamed is not None, "no groups streamed into the sidebar within 30s"
    assert streamed["scanning"], "groups only appeared after the scan finished"

    # Cancel mid-run: the cancel control lives in the collapsed setup panel.
    # The "Cancelling…" toast is replaced almost immediately (the worker halts
    # between work items), faster than wait_for polls — observe the toast's
    # text transitions instead of waiting for a visible end state.
    page.evaluate(
        """() => {
          window.__toastLog = [];
          const el = document.getElementById("toastMessage");
          new MutationObserver(() => window.__toastLog.push(el.textContent))
            .observe(el, { childList: true, characterData: true, subtree: true });
        }"""
    )
    page.locator("#scanCollapse").click()
    page.locator("#btnCancelScan").click()
    # The scan stops and the previous (duplicate_images) results come back.
    expect(page.locator("#btnCancelScan")).to_be_hidden(timeout=30_000)
    expect(page.locator("#countExact")).to_have_text("1", timeout=30_000)
    expect(page.locator(".group-item")).to_have_count(4, timeout=30_000)
    toast_log = page.evaluate("window.__toastLog")
    assert any("Cancelling" in text for text in toast_log)
    assert page_errors == []
