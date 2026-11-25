"""Event filtering for monitored cleanup jobs."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dedupe.monitor import FolderMonitor, _JobEventHandler


def _handler(root: Path, **overrides) -> _JobEventHandler:
    options = {
        "include_images": True,
        "include_gifs": True,
        "include_videos": True,
        "include_hidden": False,
        "exclusions": [],
    }
    options.update(overrides)
    return _JobEventHandler(root, lambda: None, **options)


def _event(path: Path, *, directory: bool = False, dest_path: str = "", event_type: str = "modified"):
    return SimpleNamespace(
        src_path=str(path),
        dest_path=dest_path,
        is_directory=directory,
        event_type=event_type,
    )


def test_media_events_are_relevant_but_noise_is_filtered(tmp_path: Path) -> None:
    root = tmp_path / "media"
    handler = _handler(root, exclusions=["ignored*"])

    assert handler.relevant(_event(root / "inbox" / "photo.jpg"))
    assert handler.relevant(_event(root / "inbox", directory=True))
    assert handler.relevant(_event(root / "old.txt", dest_path=str(root / "renamed.mov")))
    assert not handler.relevant(_event(root / "notes.txt"))
    assert not handler.relevant(_event(root / ".hidden" / "photo.jpg"))
    assert not handler.relevant(_event(root / "ignored-photo.jpg"))
    assert not handler.relevant(_event(root / "photo.jpg", event_type="opened"))
    assert not handler.relevant(_event(root / "photo.jpg", event_type="closed_no_write"))


def test_media_type_toggles_filter_events_like_scan_inventory(tmp_path: Path) -> None:
    root = tmp_path / "media"
    handler = _handler(root, include_images=False, include_gifs=False)

    assert not handler.relevant(_event(root / "photo.jpg"))
    assert not handler.relevant(_event(root / "animation.gif"))
    assert handler.relevant(_event(root / "clip.mp4"))


def test_recreated_watch_root_is_reattached(tmp_path) -> None:
    events = []
    root = tmp_path / "watch"
    root.mkdir()
    monitor = FolderMonitor(
        lambda job_id: events.append(job_id), debounce_seconds=0.02, health_seconds=0.02
    )
    try:
        monitor.start("job", [root], {})
        original_observer = monitor._observers["job"]
        shutil.rmtree(root)
        root.mkdir()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            monitor.refresh("job")
            if monitor._observers.get("job") is not original_observer:
                break
            time.sleep(0.01)
        assert monitor._observers.get("job") is not original_observer
        # Deleting the watched root is itself a relevant event. Only a creation
        # after reattachment proves that the replacement directory is watched.
        events.clear()
        (root / "arrival.jpg").write_bytes(b"jpg")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not events:
            time.sleep(0.01)
        assert events == ["job"]
    finally:
        monitor.stop_all()


def test_start_failure_shuts_down_partial_observer(tmp_path, monkeypatch) -> None:
    import dedupe.monitor as monitor_module

    stopped = []

    class FailingObserver:
        def __init__(self):
            self.running = False

        def schedule(self, *_args, **_kwargs):
            return None

        def start(self):
            self.running = True
            raise OSError("emitter failed")

        def stop(self):
            stopped.append(True)
            self.running = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.running

    monkeypatch.setattr(monitor_module, "Observer", FailingObserver)
    monitor = FolderMonitor(lambda _job: None)
    try:
        with pytest.raises(OSError, match="emitter failed"):
            monitor.start("job", [tmp_path], {})
        assert stopped == [True]
        assert not monitor.running("job")
    finally:
        monitor.stop_all()
