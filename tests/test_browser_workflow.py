"""Critical browser workflow against a real loopback Flask server."""

from __future__ import annotations

import shutil
import threading
import urllib.request
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.serving import make_server

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
    page.locator(".group-item").first.click()
    page.locator("#members .card").first.wait_for(state="visible")

    # The default exact-match recommendation removes one file and always keeps one.
    assert page.locator("#members .card").count() == 2
    assert page.locator("#members .card.selected").count() == 1
    assert page.locator("#members .card.keep").count() == 1
    assert page.locator("#members .sel-cb:checked").count() == 1

    page.locator("#members .thumb-wrap").first.click()
    page.locator("#lightbox").wait_for(state="visible")
    page.locator("#lbClose").click()
    page.locator("#lightbox").wait_for(state="hidden")

    # Search and category filtering both update the browser-rendered group list.
    page.locator("#resultSearch").fill("does-not-exist")
    assert page.locator(".group-item").count() == 0
    page.locator("#resultSearch").fill("duplicate.png")
    assert page.locator(".group-item").count() == 1
    page.get_by_role("button", name="Exact 1").click()
    assert page.locator(".group-item").count() == 1

    # Dry-run traverses the action endpoint but cannot move either fixture to Trash.
    page.locator("#btnDryTrash").click()
    page.locator("#toast").filter(has_text="Preview: 1 ok, 0 failed").wait_for()
    assert sorted(path.name for path in duplicate_images.iterdir()) == [
        "duplicate.png",
        "keeper.png",
    ]
    assert page_errors == []
    assert console_errors == []
