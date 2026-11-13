"""User-facing protected-library, saved-job, and durable-history workflow."""

import shutil
import threading

import pytest
from PIL import Image
from playwright.sync_api import expect
from werkzeug.serving import make_server

from dedupe.web.app import create_app


@pytest.mark.e2e
def test_protected_job_and_history_survive_reload(page, tmp_path):
    archive = tmp_path / "archive"
    inbox = tmp_path / "inbox"
    archive.mkdir()
    inbox.mkdir()
    original = archive / "original.png"
    duplicate = inbox / "copy.png"
    Image.new("RGB", (80, 60), "coral").save(original)
    shutil.copyfile(original, duplicate)
    app = create_app(review_session_path=tmp_path / "review.json",
                     cache_path=tmp_path / "cache.sqlite3")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text)
            if message.type == "error" else None)
    try:
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.locator("#paths").fill(str(inbox))
        page.locator("#protectedFolders").fill(str(archive))
        page.locator("#cleanupJobName").fill("Inbox against archive")
        page.locator("#btnSaveJob").click()
        expect(page.locator("#cleanupJob option")).to_have_count(2)
        page.locator("#btnScan").click()
        expect(page.locator("#countExact")).to_have_text("1", timeout=20_000)
        expect(page.locator("#btnScan")).to_be_enabled()
        page.locator('.tab[data-kind="exact"]').click()
        page.locator(".group-item").first.click()
        expect(page.locator("#members .protected-label")).to_have_text("Protected library")
        expect(page.locator("#members .sel-cb:checked")).to_have_count(1)

        # Only the inbox copy can be trashed, and history can restore it after reload.
        page.locator("#btnTrashExact").click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        with page.expect_response(lambda response: response.url.endswith("/api/action")
                                  and not response.request.post_data_json.get("dry_run", True)) as executed:
            page.locator("#modalConfirm").click()
        assert executed.value.json()["success_count"] == 1
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(page.locator("#toast")).to_contain_text("Done", timeout=15_000)
        assert original.exists() and not duplicate.exists()
        page.reload()
        page.locator("#btnHistory").click()
        restore = page.locator("[data-history-restore]").first
        expect(restore).to_be_visible()
        restore.click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        page.locator("#modalConfirm").click()
        expect(page.locator("#toast")).to_contain_text("Restored", timeout=15_000)
        assert original.read_bytes() == duplicate.read_bytes()

        page.locator("#btnCloseHistory").click()
        if "collapsed" in page.locator("#scanPanel").get_attribute("class"):
            page.locator("#scanCollapse").click()
        page.locator("#cleanupJob").select_option(label="Inbox against archive")
        expect(page.locator("#protectedFolders")).to_have_value(str(archive))
        expect(page.locator("#btnResumeJob")).to_be_enabled()
        page.locator("#btnResumeJob").click()
        expect(page.locator("#progressMsg")).to_contain_text("Resumed")
        assert not errors
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
