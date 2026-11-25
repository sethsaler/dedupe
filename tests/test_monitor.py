"""Event filtering for monitored cleanup jobs."""

from __future__ import annotations

import shutil
import threading
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
    return _JobEventHandler(root, lambda *_args: None, **options)


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
    assert handler.relevant(_event(root / "inbox", directory=True, event_type="created"))
    assert not handler.relevant(_event(root / "inbox", directory=True))
    assert handler.relevant(_event(root / "old.txt", dest_path=str(root / "renamed.mov")))
    assert not handler.relevant(_event(root / "notes.txt"))
    assert not handler.relevant(_event(root / ".hidden" / "photo.jpg"))
    assert not handler.relevant(_event(root / "ignored-photo.jpg"))
    assert not handler.relevant(_event(root / "ignored" / "photo.jpg"))
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
        lambda job_id, _change: events.append(job_id), debounce_seconds=0.02, health_seconds=0.02
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
    monitor = FolderMonitor(lambda *_args: None)
    try:
        with pytest.raises(OSError, match="emitter failed"):
            monitor.start("job", [tmp_path], {})
        assert stopped == [True]
        assert not monitor.running("job")
    finally:
        monitor.stop_all()


def test_missing_drive_is_retried_and_attached_when_it_returns(tmp_path) -> None:
    missing_root = tmp_path / "drive" / "inbox"
    monitor = FolderMonitor(lambda *_args: None, health_seconds=60.0)
    try:
        with pytest.raises(ValueError, match="No existing folders"):
            monitor.start("job", [missing_root], {})
        assert "job" in monitor._specs
        assert not monitor.running("job")

        missing_root.mkdir(parents=True)
        monitor.refresh("job")

        assert monitor.running("job")
    finally:
        monitor.stop_all()


def test_unreadable_root_is_missing_without_killing_health_thread(tmp_path) -> None:
    restricted = tmp_path / "restricted"
    restricted.mkdir()
    missing_inside = restricted / "inbox"
    restricted.chmod(0)
    monitor = FolderMonitor(lambda *_args: None, health_seconds=0.01)
    try:
        assert monitor._watch_roots([missing_inside]) == []
        monitor._specs["job"] = ((str(missing_inside),), {})
        monitor.refresh("job")
        time.sleep(0.05)
        assert monitor._health_thread.is_alive()
    finally:
        restricted.chmod(0o700)
        monitor.stop_all()


def test_stop_waits_for_startup_and_cleans_observer(tmp_path, monkeypatch) -> None:
    import dedupe.monitor as monitor_module

    entered = threading.Event()
    release = threading.Event()
    instances = []

    class BlockingObserver:
        def __init__(self):
            self.running = False
            instances.append(self)

        def schedule(self, *_args, **_kwargs):
            return None

        def start(self):
            entered.set()
            assert release.wait(timeout=2)
            self.running = True

        def stop(self):
            self.running = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.running

    monkeypatch.setattr(monitor_module, "Observer", BlockingObserver)
    monitor = FolderMonitor(lambda *_args: None, health_seconds=60.0)
    try:
        starter = threading.Thread(
            target=monitor.start,
            args=("job", [tmp_path], {}),
        )
        starter.start()
        assert entered.wait(timeout=1)
        stopper = threading.Thread(target=monitor.stop, args=("job",))
        stopper.start()
        time.sleep(0.02)
        assert stopper.is_alive()
        release.set()
        starter.join(timeout=2)
        stopper.join(timeout=2)
        assert not starter.is_alive()
        assert not stopper.is_alive()
        assert not monitor.running("job")
        assert "job" not in monitor._specs
        assert instances and not instances[0].is_alive()
    finally:
        release.set()
        monitor.stop_all()


def test_nested_watch_roots_are_collapsed_to_one_recursive_watch(tmp_path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    monitor = FolderMonitor(lambda *_args: None, health_seconds=60.0)
    try:
        assert monitor._watch_roots([child, parent]) == [str(parent.resolve())]
    finally:
        monitor.stop_all()


def test_continuous_events_fire_within_a_bounded_batch_window() -> None:
    fired_at = []
    monitor = FolderMonitor(
        lambda *_args: fired_at.append(time.monotonic()),
        debounce_seconds=0.05,
        health_seconds=60.0,
    )
    try:
        class IdleObserver:
            def stop(self):
                return None

            def join(self, timeout=None):
                return None

        monitor._observers["job"] = IdleObserver()
        start = time.monotonic()
        monitor._record_change("job", ["/tmp/one.jpg"], False)
        while time.monotonic() - start < 0.4 and not fired_at:
            monitor._record_change("job", ["/tmp/two.jpg"], False)
            time.sleep(0.02)
        assert fired_at, "continuous events starved the monitor scan"
        assert fired_at[0] - start < 0.4
    finally:
        monitor.stop_all()
