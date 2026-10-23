"""Decider look-alike verification against a stand-in for Decider.app's server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from dedupe.decider import URL_ENV, DeciderJudge, decider_available
from dedupe.models import FileRecord, MediaType


class FakeDecider:
    """Serves /health, /v1/models and /v1/decisions; answers ``same`` = probability."""

    def __init__(self, probability: float = 0.9, images: bool = True) -> None:
        self.probability = probability
        self.images = images
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _reply(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._reply({"status": "ready", "images": fake.images})
                elif self.path == "/v1/models":
                    self._reply({"data": [
                        {"id": "perplexity/pplx-decider-flash-v1.2"},
                        {"id": "pplx-decider-flash-v1.2"},
                        {"id": "pplx-decider-flash-latest"},
                    ]})
                else:
                    self._reply({"error": {"message": "Not found"}}, 404)

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(request)
                same = fake.probability
                self._reply({"answers": {"copy": {"type": "choice", "probabilities": {
                    "same": same, "burst": (1 - same) / 2, "different": (1 - same) / 2,
                }}}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_decider(monkeypatch):
    server = FakeDecider()
    monkeypatch.setenv(URL_ENV, server.url)
    yield server
    server.close()


def _record(path: Path, color: tuple[int, int, int]) -> FileRecord:
    Image.new("RGB", (32, 32), color).save(path)
    stat = path.stat()
    return FileRecord(path=str(path), size=stat.st_size, mtime=stat.st_mtime,
                      media_type=MediaType.IMAGE, extension=path.suffix)


def test_judge_asks_the_pinned_model_about_both_images(
    tmp_path: Path, fake_decider: FakeDecider
) -> None:
    a = _record(tmp_path / "a.png", (200, 20, 20))
    b = _record(tmp_path / "b.png", (20, 200, 20))

    with DeciderJudge(use_cache=False) as judge:
        assert judge(a, b) == pytest.approx(0.9)

    (request,) = fake_decider.requests
    assert request["model"] == "pplx-decider-flash-v1.2"
    images = [part for part in request["state"] if part["type"] == "image_url"]
    assert len(images) == 2
    assert all(p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in images)
    assert set(request["questions"]["copy"]["criteria"]) == {"same", "burst", "different"}


def test_verdicts_are_cached_until_a_file_changes(
    tmp_path: Path, fake_decider: FakeDecider
) -> None:
    cache = tmp_path / "cache.sqlite3"
    a = _record(tmp_path / "a.png", (200, 20, 20))
    b = _record(tmp_path / "b.png", (20, 200, 20))

    with DeciderJudge(cache_path=cache) as judge:
        judge(a, b)
    with DeciderJudge(cache_path=cache) as judge:
        assert judge(b, a) == pytest.approx(0.9)  # either order hits the cache
    assert len(fake_decider.requests) == 1

    b = _record(tmp_path / "b.png", (20, 20, 200))
    b.mtime += 5
    with DeciderJudge(cache_path=cache) as judge:
        judge(a, b)
    assert len(fake_decider.requests) == 2


def test_unreadable_media_skips_one_pair_but_a_dead_server_stops_judging(
    tmp_path: Path, fake_decider: FakeDecider
) -> None:
    a = _record(tmp_path / "a.png", (200, 20, 20))
    b = _record(tmp_path / "b.png", (20, 200, 20))
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    bad = FileRecord(path=str(broken), size=12, mtime=0.0,
                     media_type=MediaType.IMAGE, extension=".png")

    with DeciderJudge(use_cache=False) as judge:
        assert judge(a, bad) is None
        assert judge.failure is None
        fake_decider.close()
        assert judge(a, b) is None
        assert judge.failure and judge.failure.startswith("Decider verification stopped")


def test_availability_needs_a_ready_image_model(monkeypatch, tmp_path: Path) -> None:
    text_only = FakeDecider(images=False)
    try:
        assert not decider_available(text_only.url)
    finally:
        text_only.close()
    monkeypatch.setenv(URL_ENV, "http://127.0.0.1:9")
    assert not decider_available()
    with pytest.raises(RuntimeError, match="Decider is not running"):
        DeciderJudge(use_cache=False)
