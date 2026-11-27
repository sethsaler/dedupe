"""Event filtering for monitored cleanup jobs."""

from __future__ import annotations

import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_move_events_only_include_endpoints_inside_watch(tmp_path: Path) -> None:
    root = tmp_path / "media"
    handler = _handler(root)
    inside = root / "photo.jpg"
    outside = tmp_path / "archive" / "photo.jpg"
    for source, destination in ((inside, outside), (outside, inside)):
        move = _event(source, event_type="moved", dest_path=str(destination))
        assert handler.relevant_paths(move) == [str(inside)]


def test_individual_files_share_a_parent_watch_limited_to_the_selected_names(tmp_path) -> None:
    one = tmp_path / "one.jpg"
    two = tmp_path / "two.jpg"
    album = tmp_path / "album"
    one.touch()
    two.touch()
    album.mkdir()
    plan = FolderMonitor._watch_plan([one, two, album])
    assert plan == {str(tmp_path): frozenset({one.name, two.name}), str(album): None}
    handler = _handler(tmp_path, child_names=plan[str(tmp_path)])
    assert handler.relevant(_event(one))
    assert not handler.relevant(_event(tmp_path / "sibling.jpg"))
    assert not handler.relevant(_event(album / "one.jpg"))
    assert not handler.relevant(_event(album, directory=True, event_type="created"))
    assert handler.relevant_paths(_event(tmp_path / "temp.jpg", dest_path=str(two))) == [str(two)]


def test_selected_name_never_admits_distinct_case_sensitive_sibling(tmp_path, monkeypatch):
    # On a case-sensitive filesystem the two names resolve with their own spelling.
    monkeypatch.setattr("dedupe.monitor._event_path", lambda path: path)
    handler = _handler(tmp_path, child_names=frozenset({"Photo.jpg"}))
    assert handler.relevant(_event(tmp_path / "Photo.jpg"))
    assert not handler.relevant(_event(tmp_path / "photo.jpg"))


@pytest.mark.skipif(sys.platform != "darwin", reason="requires native macOS FSEvents")
@pytest.mark.parametrize("selection", ["directory", "file", "missing_file"])
def test_native_events_use_selected_case_for_baseline(tmp_path, monkeypatch, selection):
    actual = tmp_path / "MixedCase"
    actual.mkdir()
    selected_dir = tmp_path / "mixedcase"
    if not selected_dir.is_dir():
        pytest.skip("requires a case-insensitive volume")
    actual_file = actual / "Example.jpg"
    selected = selected_dir if selection == "directory" else selected_dir / "example.jpg"
    expected = selected_dir / actual_file.name if selection == "directory" else selected
    if selection == "file":
        actual_file.touch()

    delivered = threading.Event()
    paths = set()

    def record(_job, changed_paths, directory):
        if not directory:
            paths.update(changed_paths)
            if str(expected) in paths:
                delivered.set()

    monitor = FolderMonitor(lambda *_args: None, health_seconds=60)
    monkeypatch.setattr(monitor, "_record_change", record)
    try:
        monitor.start("job", [selected], {})
        (actual / "sibling.jpg").touch()
        actual_file.write_bytes(b"new arrival")
        assert delivered.wait(timeout=3), paths
        if selection != "directory":
            assert paths == {str(expected)}
    finally:
        monitor.stop_all()


def test_explicit_file_rejected_by_ancestor_glob_has_its_own_watch(tmp_path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    selected = album / "one.jpg"
    selected.touch()
    plan = FolderMonitor._watch_plan([selected, tmp_path], {"exclusions": ["album/*.jpg"]})
    assert plan == {str(tmp_path): None, str(album): frozenset({selected.name})}


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


@pytest.mark.parametrize("error_type", [OSError, SystemError])
def test_start_failure_shuts_down_partial_observer(tmp_path, monkeypatch, error_type) -> None:
    import dedupe.monitor as monitor_module

    stopped = []

    class FailingObserver:
        def __init__(self):
            self.running = False

        def schedule(self, *_args, **_kwargs):
            return None

        def start(self):
            self.running = True
            raise error_type("emitter failed")

        def stop(self):
            stopped.append(True)
            self.running = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.running

    monkeypatch.setattr(monitor_module, "Observer", FailingObserver)
    notified = threading.Event()
    monitor = FolderMonitor(lambda *_args: notified.set(), debounce_seconds=0.01, health_seconds=60)
    try:
        with pytest.raises(OSError, match="emitter failed"):
            monitor.start("job", [tmp_path], {})
        assert stopped == [True]
        assert not monitor.running("job")
        monitor.refresh("job")
        assert stopped == [True, True]
        assert not monitor.running("job")
        assert "emitter failed" in monitor.watch_error("job")
        # Repeated health checks preserve the first notification, even when
        # back-to-back retries occur before the debounced callback fires.
        monitor.refresh("job")
        assert notified.wait(timeout=2)
        notified.clear()
        monitor.refresh("job")
        assert not notified.wait(timeout=0.05)
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
        assert monitor._watch_plan([missing_inside]) == {}
        with pytest.raises(ValueError, match="No existing folders"):
            monitor.start("job", [missing_inside], {})
        monitor.refresh("job")
        time.sleep(0.05)
        assert monitor._health_thread.is_alive()
    finally:
        restricted.chmod(0o700)
        monitor.stop_all()


@pytest.mark.parametrize("shut_down", [False, True])
def test_stop_waits_for_startup_and_cleans_observer(tmp_path, monkeypatch, shut_down) -> None:
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
        stopper = threading.Thread(target=monitor.stop_all if shut_down else lambda: monitor.stop("job"))
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
        if shut_down:
            with pytest.raises(ValueError, match="shut down"):
                monitor.start("job", [tmp_path], {})
    finally:
        release.set()
        monitor.stop_all()


def test_health_worker_only_runs_while_jobs_are_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("dedupe.monitor.Observer", Mock)
    monitor = FolderMonitor(lambda *_args: None, health_seconds=60)
    try:
        assert monitor._health_thread is None
        monitor.start("first", [tmp_path], {})
        first_worker = monitor._health_thread
        assert first_worker.is_alive()
        monitor.start("second", [tmp_path], {})
        monitor.stop("first")
        assert monitor._health_thread is first_worker
        monitor.stop("second")
        first_worker.join(timeout=2)
        assert monitor._health_thread is None

        monitor.start("first", [tmp_path], {})
        assert monitor._health_thread.is_alive()
        assert monitor._health_thread is not first_worker
    finally:
        monitor.stop_all()


def test_nested_watch_roots_are_collapsed_to_one_recursive_watch(tmp_path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    monitor = FolderMonitor(lambda *_args: None, health_seconds=60.0)
    try:
        assert monitor._watch_plan([child, parent]) == {str(parent.resolve()): None}
    finally:
        monitor.stop_all()



def test_hidden_or_excluded_explicit_roots_keep_their_own_watch(tmp_path) -> None:
    parent = tmp_path / "parent"
    hidden = parent / ".hidden"
    excluded = parent / "excluded"
    hidden.mkdir(parents=True)
    excluded.mkdir()
    monitor = FolderMonitor(lambda *_args: None, health_seconds=60.0)
    try:
        assert set(monitor._watch_plan([hidden, parent])) == {
            str(parent.resolve()),
            str(hidden.resolve()),
        }
        assert set(monitor._watch_plan([excluded, parent], {"exclusions": ["excluded"]})) == {
            str(parent.resolve()),
            str(excluded.resolve()),
        }
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


def test_burst_uses_one_worker_and_stop_discards_its_batch(tmp_path, monkeypatch) -> None:
    # Supply the event burst directly so native event history cannot add a
    # directory change to either batch as the same watch is restarted.
    monkeypatch.setattr("dedupe.monitor.Observer", Mock)
    changes = []
    delivered = threading.Event()

    def on_change(_job, change):
        changes.append(change)
        delivered.set()

    monitor = FolderMonitor(on_change, debounce_seconds=0.05, health_seconds=60.0)
    workers = []
    started = threading.Event()
    release = threading.Event()
    deliver = monitor._deliver_batch

    def hold_delivery(job_id, batch):
        workers.append(batch)
        started.set()
        release.wait(timeout=2)
        deliver(job_id, batch)

    monkeypatch.setattr(monitor, "_deliver_batch", hold_delivery)
    try:
        monitor.start("job", [tmp_path], {})
        for index in range(800):
            monitor._record_change("job", [f"old-{index}.jpg"], False)
        assert started.wait(timeout=2)
        assert len(workers) == 1
        assert workers[0].change.overflow

        monitor.stop("job")
        monitor.start("job", [tmp_path], {})
        monitor._record_change("job", ["new.jpg"], False)
        release.set()
        assert delivered.wait(timeout=2)
        assert len(changes) == 1
        assert changes[0].paths == {"new.jpg"}
    finally:
        release.set()
        monitor.stop_all()


@pytest.mark.parametrize("failed_thread", ["observer", "emitter"])
def test_dead_watcher_recovers_and_checks_for_missed_arrivals(tmp_path, failed_thread) -> None:
    changes = []
    delivered = threading.Event()

    def on_change(_job, change):
        changes.append(change)
        delivered.set()

    monitor = FolderMonitor(on_change, debounce_seconds=0.1, health_seconds=60.0)
    try:
        monitor.start("job", [tmp_path], {})
        observer = monitor._observers["job"]
        failed = observer if failed_thread == "observer" else next(iter(observer.emitters))
        failed.stop()
        failed.join(timeout=2)
        assert not monitor.running("job")

        monitor.refresh("job")
        assert monitor.running("job")
        assert monitor._observers["job"] is not observer
        monitor._record_change("job", ["already-known.jpg"], False)
        assert delivered.wait(timeout=2)
        assert changes[0].directory
    finally:
        monitor.stop_all()


def test_losing_last_watch_notifies_once_until_root_returns(tmp_path) -> None:
    root = tmp_path / "volume" / "photos"
    root.mkdir(parents=True)
    changes = []
    delivered = threading.Event()

    def on_change(_job, change):
        changes.append(change)
        delivered.set()

    monitor = FolderMonitor(on_change, debounce_seconds=0.05, health_seconds=60.0)
    try:
        monitor.start("job", [root], {})
        root.parent.rename(tmp_path / "unmounted")
        monitor.refresh("job")
        assert not monitor.running("job")
        assert delivered.wait(timeout=2)
        delivered.clear()
        monitor.refresh("job")
        assert not delivered.wait(timeout=0.1)

        (tmp_path / "unmounted").rename(root.parent)
        monitor.refresh("job")
        assert delivered.wait(timeout=2)
        assert monitor.running("job")
        assert len(changes) == 2
        assert all(change.directory for change in changes)
    finally:
        monitor.stop_all()
