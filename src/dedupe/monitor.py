"""Filesystem watching for monitored cleanup jobs."""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .scanner import BUILTIN_EXCLUSIONS, _exclusion_matcher, is_in_photos_library, media_extensions


@dataclass
class MonitorChange:
    """The file events collected during one debounce window."""

    paths: set[str] = field(default_factory=set)
    directory: bool = False
    overflow: bool = False

    def add(self, paths: Iterable[str], directory: bool) -> None:
        if directory:
            self.directory = True
            self.paths.clear()
            return
        if self.directory or self.overflow:
            return
        self.paths.update(paths)
        if len(self.paths) > 512:
            # A huge batch is cheaper to verify with one inventory than with
            # unbounded per-path bookkeeping.
            self.overflow = True
            self.paths.clear()


class _JobEventHandler(FileSystemEventHandler):
    """Trigger once for events that can affect a job's media inventory."""

    def __init__(
        self,
        root: Path,
        callback: Callable[[list[str], bool], None],
        *,
        include_images: bool,
        include_gifs: bool,
        include_videos: bool,
        include_hidden: bool,
        exclusions: Iterable[str],
        root_event_callback: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self.callback = callback
        self.root_event_callback = root_event_callback
        self.extensions = media_extensions(include_images, include_gifs, include_videos)
        self.include_hidden = include_hidden
        patterns = {e.strip().lower() for e in exclusions if str(e).strip()}
        patterns |= BUILTIN_EXCLUSIONS
        self.excluded = _exclusion_matcher(patterns)

    def on_any_event(self, event: FileSystemEvent) -> None:
        paths = self.relevant_paths(event)
        if paths:
            self.callback(paths, bool(event.is_directory))
        if (
            self.root_event_callback is not None
            and event.is_directory
            and event.event_type in {"deleted", "moved"}
            and Path(event.src_path) == self.root
        ):
            self.root_event_callback()

    def relevant(self, event: FileSystemEvent) -> bool:
        return bool(self.relevant_paths(event))

    def relevant_paths(self, event: FileSystemEvent) -> list[str]:
        # Watchdog also reports read-only open/close events on some platforms.
        # Those cannot change the media inventory and scanning them would make
        # the monitor re-scan its own analysis reads.
        if event.event_type not in {"created", "modified", "deleted", "moved"}:
            return False
        if event.is_directory and event.event_type == "modified":
            # Directory mtime updates accompany child create/delete events and
            # do not themselves describe media inventory. Treating them as a
            # directory change would force a full-library precheck every time.
            return []
        paths = [event.src_path]
        dest_path = getattr(event, "dest_path", "")
        if dest_path:
            paths.append(dest_path)
        matching = []
        for raw_path in paths:
            path = Path(raw_path)
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                relative = Path(path.name)
            if not self.include_hidden and any(
                part.startswith(".") for part in relative.parts if part not in (".", "")
            ):
                continue
            if self.excluded(path.name, relative):
                continue
            # A directory event can represent many media files moving at once;
            # file events are cheap to filter by the scan's enabled extensions.
            if event.is_directory or path.suffix.lower() in self.extensions:
                matching.append(str(path))
        return matching


class FolderMonitor:
    """Manage one recursive observer per monitored cleanup job."""

    def __init__(
        self,
        on_change: Callable[[str, MonitorChange | None], None],
        *,
        debounce_seconds: float = 2.0,
        health_seconds: float = 2.0,
    ) -> None:
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self.health_seconds = health_seconds
        self._lock = threading.RLock()
        self._observers: dict[str, Observer] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._specs: dict[str, tuple[tuple[str, ...], dict]] = {}
        self._runtime_locks: dict[str, threading.Lock] = {}
        self._watch_identities: dict[str, dict[str, tuple[int, int]]] = {}
        self._root_losses: dict[str, set[str]] = {}
        self._changes: dict[str, MonitorChange] = {}
        self._pending_since: dict[str, float] = {}
        self._stopped = threading.Event()
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    @staticmethod
    def _watch_roots(roots: Iterable[str]) -> list[str]:
        watched: list[str] = []
        seen: set[str] = set()
        for raw_root in roots:
            try:
                root = Path(raw_root).expanduser().resolve(strict=False)
                watch_root = root if root.is_dir() else root.parent
                if not watch_root.is_dir():
                    continue
            except OSError:
                # An unreadable mount point is missing for watch purposes; the
                # health retry must not kill recovery for every other job.
                continue
            # Newer Pythons report an unreadable location as "not a directory"
            # instead of raising, which would fall back to watching a parent
            # the observer cannot look inside.
            if not os.access(watch_root, os.R_OK | os.X_OK):
                continue
            key = str(watch_root)
            if key in seen or is_in_photos_library(watch_root):
                continue
            seen.add(key)
            watched.append(key)
        collapsed: list[str] = []
        candidates = sorted((Path(key) for key in watched), key=lambda path: len(path.parts))
        for candidate in candidates:
            if any(
                candidate == existing or candidate.is_relative_to(existing)
                for existing in (Path(key) for key in collapsed)
            ):
                continue
            collapsed.append(str(candidate))
        return collapsed

    def _make_observer(
        self,
        job_id: str,
        watched: list[str],
        settings: dict,
    ) -> Observer:
        exclusions = settings.get("exclusions") or []
        if isinstance(exclusions, str):
            exclusions = exclusions.split(",")
        include_images = bool(settings.get("include_images", True))
        include_gifs = bool(settings.get("include_gifs", True))
        include_videos = bool(settings.get("include_videos", True))
        include_hidden = bool(settings.get("include_hidden", False))
        observer = Observer()
        for key in watched:
            handler = _JobEventHandler(
                Path(key),
                lambda paths, directory, job_id=job_id: self._record_change(
                    job_id, paths, directory
                ),
                root_event_callback=lambda job_id=job_id, key=key: self._mark_root_lost(
                    job_id, key
                ),
                include_images=include_images,
                include_gifs=include_gifs,
                include_videos=include_videos,
                include_hidden=include_hidden,
                exclusions=exclusions,
            )
            observer.schedule(handler, key, recursive=True)
        return observer

    def _runtime_lock(self, job_id: str) -> threading.Lock:
        with self._lock:
            return self._runtime_locks.setdefault(job_id, threading.Lock())

    def start(
        self,
        job_id: str,
        roots: Iterable[str],
        settings: dict,
    ) -> list[str]:
        """Start watching a job and return the watched directory paths."""
        roots = tuple(str(root) for root in roots)
        spec = (roots, dict(settings))
        runtime_lock = self._runtime_lock(job_id)
        with runtime_lock:
            self._stop_runtime(job_id)
            with self._lock:
                # Keep the spec even when a removable drive is fully absent. The
                # health loop uses it to attach a watcher when the drive returns.
                self._specs[job_id] = spec
                self._root_losses.pop(job_id, None)
            watched = self._watch_roots(roots)
            if not watched:
                raise ValueError("No existing folders to monitor")
            observer = self._make_observer(job_id, watched, settings)
            try:
                observer.start()
            except (OSError, ValueError):
                self._shutdown_observer(observer)
                raise
            identities = {}
            for key in watched:
                try:
                    stat = Path(key).stat()
                    identities[key] = (stat.st_dev, stat.st_ino)
                except OSError:
                    pass
            with self._lock:
                self._observers[job_id] = observer
                self._watch_identities[job_id] = identities
            return watched

    @staticmethod
    def _shutdown_observer(observer: Observer) -> None:
        observer.stop()
        with contextlib.suppress(RuntimeError):
            observer.join(timeout=2.0)

    def _stop_runtime(self, job_id: str) -> None:
        with self._lock:
            observer = self._observers.pop(job_id, None)
            timer = self._timers.pop(job_id, None)
            self._watch_identities.pop(job_id, None)
            self._changes.pop(job_id, None)
            self._pending_since.pop(job_id, None)
        if timer is not None:
            timer.cancel()
        if observer is not None:
            self._shutdown_observer(observer)

    def stop(self, job_id: str) -> None:
        runtime_lock = self._runtime_lock(job_id)
        with runtime_lock:
            self._stop_runtime(job_id)
            with self._lock:
                self._specs.pop(job_id, None)
                self._root_losses.pop(job_id, None)

    def stop_all(self) -> None:
        self._stopped.set()
        if self._health_thread.is_alive():
            self._health_thread.join(timeout=2.0)
        with self._lock:
            job_ids = set(self._observers) | set(self._timers) | set(self._specs)
        for job_id in job_ids:
            self.stop(job_id)

    def running(self, job_id: str) -> bool:
        with self._lock:
            observer = self._observers.get(job_id)
        return bool(observer and observer.is_alive())

    def watched_jobs(self) -> set[str]:
        with self._lock:
            return set(self._observers)

    def _record_change(self, job_id: str, paths: list[str], directory: bool) -> None:
        with self._lock:
            self._pending_since.setdefault(job_id, time.monotonic())
            change = self._changes.setdefault(job_id, MonitorChange())
            change.add(paths, directory)
        self._debounce(job_id)

    def _debounce(self, job_id: str) -> None:
        with self._lock:
            now = time.monotonic()
            timer = self._timers.get(job_id)
            if timer is not None:
                first_pending = self._pending_since.get(job_id)
                if first_pending is not None:
                    # Resetting every event can postpone a scan forever while a
                    # folder is actively written. Keep the existing timer once
                    # it is the last quiet period inside a bounded batch window.
                    max_delay = self.debounce_seconds * 5
                    if first_pending + max_delay - now <= self.debounce_seconds:
                        return
                timer.cancel()
            timer = threading.Timer(
                self.debounce_seconds,
                self._fire,
                args=(job_id,),
            )
            timer.daemon = True
            self._timers[job_id] = timer
            timer.start()

    def _mark_root_lost(self, job_id: str, root: str) -> None:
        with self._lock:
            self._root_losses.setdefault(job_id, set()).add(root)

    def refresh(self, job_id: str) -> None:
        """Reconfigure watches after roots disappear or are replaced."""
        runtime_lock = self._runtime_lock(job_id)
        with runtime_lock:
            with self._lock:
                spec = self._specs.get(job_id)
                identities = dict(self._watch_identities.get(job_id, {}))
                lost = set(self._root_losses.get(job_id, set()))
            if spec is None:
                return
            roots, settings = spec
            desired = self._watch_roots(roots)
            changed = False
            missing = set()
            for raw_path, identity in identities.items():
                try:
                    stat = Path(raw_path).stat()
                except OSError:
                    missing.add(raw_path)
                    continue
                if (stat.st_dev, stat.st_ino) != identity:
                    changed = True
            lost |= missing
            needs_restart = (
                bool(lost)
                or changed
                or set(desired) != set(identities)
            )
            if not needs_restart:
                return

            self._stop_runtime(job_id)
            with self._lock:
                self._root_losses.pop(job_id, None)
            if not desired:
                return
            observer = None
            try:
                observer = self._make_observer(job_id, desired, settings)
                observer.start()
            except (OSError, ValueError):
                if observer is not None:
                    self._shutdown_observer(observer)
                return
            new_identities = {}
            for key in desired:
                try:
                    stat = Path(key).stat()
                    new_identities[key] = (stat.st_dev, stat.st_ino)
                except OSError:
                    pass
            with self._lock:
                if self._specs.get(job_id) != spec:
                    self._shutdown_observer(observer)
                    return
                self._observers[job_id] = observer
                self._watch_identities[job_id] = new_identities
            self._debounce(job_id)

    def _health_loop(self) -> None:
        while not self._stopped.wait(self.health_seconds):
            with self._lock:
                job_ids = list(self._specs)
            for job_id in job_ids:
                try:
                    self.refresh(job_id)
                except (OSError, ValueError):
                    continue

    def _fire(self, job_id: str) -> None:
        with self._lock:
            self._timers.pop(job_id, None)
            active = job_id in self._observers
            change = self._changes.pop(job_id, None)
            self._pending_since.pop(job_id, None)
        if active:
            self.on_change(job_id, change)
