"""Filesystem watching for monitored cleanup jobs."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .scanner import BUILTIN_EXCLUSIONS, _exclusion_matcher, is_in_photos_library, media_extensions


class _JobEventHandler(FileSystemEventHandler):
    """Trigger once for events that can affect a job's media inventory."""

    def __init__(
        self,
        root: Path,
        callback: Callable[[], None],
        *,
        include_images: bool,
        include_gifs: bool,
        include_videos: bool,
        include_hidden: bool,
        exclusions: Iterable[str],
    ) -> None:
        self.root = root
        self.callback = callback
        self.extensions = media_extensions(include_images, include_gifs, include_videos)
        self.include_hidden = include_hidden
        patterns = {e.strip().lower() for e in exclusions if str(e).strip()}
        patterns |= BUILTIN_EXCLUSIONS
        self.excluded = _exclusion_matcher(patterns)

    def on_any_event(self, event: FileSystemEvent) -> None:
        if self.relevant(event):
            self.callback()

    def relevant(self, event: FileSystemEvent) -> bool:
        # Watchdog also reports read-only open/close events on some platforms.
        # Those cannot change the media inventory and scanning them would make
        # the monitor re-scan its own analysis reads.
        if event.event_type not in {"created", "modified", "deleted", "moved"}:
            return False
        paths = [event.src_path]
        dest_path = getattr(event, "dest_path", "")
        if dest_path:
            paths.append(dest_path)
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
                return True
        return False


class FolderMonitor:
    """Manage one recursive observer per monitored cleanup job."""

    def __init__(
        self,
        on_change: Callable[[str], None],
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
        self._watch_identities: dict[str, dict[str, tuple[int, int]]] = {}
        self._stopped = threading.Event()
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    def start(
        self,
        job_id: str,
        roots: Iterable[str],
        settings: dict,
    ) -> list[str]:
        """Start watching a job and return the watched directory paths."""
        self.stop(job_id)
        exclusions = settings.get("exclusions") or []
        if isinstance(exclusions, str):
            exclusions = exclusions.split(",")
        include_images = bool(settings.get("include_images", True))
        include_gifs = bool(settings.get("include_gifs", True))
        include_videos = bool(settings.get("include_videos", True))
        include_hidden = bool(settings.get("include_hidden", False))
        with self._lock:
            self._specs[job_id] = (tuple(str(root) for root in roots), dict(settings))
        observer = Observer()
        watched: list[str] = []
        seen: set[str] = set()
        for raw_root in roots:
            root = Path(raw_root).expanduser().resolve(strict=False)
            watch_root = root if root.is_dir() else root.parent
            key = str(watch_root)
            if key in seen or not watch_root.is_dir() or is_in_photos_library(watch_root):
                continue
            seen.add(key)
            handler = _JobEventHandler(
                watch_root,
                lambda job_id=job_id: self._debounce(job_id),
                include_images=include_images,
                include_gifs=include_gifs,
                include_videos=include_videos,
                include_hidden=include_hidden,
                exclusions=exclusions,
            )
            observer.schedule(handler, key, recursive=True)
            watched.append(key)
        if not watched:
            raise ValueError("No existing folders to monitor")
        observer.start()
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

    def stop(self, job_id: str) -> None:
        with self._lock:
            observer = self._observers.pop(job_id, None)
            timer = self._timers.pop(job_id, None)
            self._specs.pop(job_id, None)
            self._watch_identities.pop(job_id, None)
        if timer is not None:
            timer.cancel()
        if observer is not None:
            observer.stop()
            observer.join(timeout=2.0)

    def stop_all(self) -> None:
        self._stopped.set()
        if self._health_thread.is_alive():
            self._health_thread.join(timeout=2.0)
        with self._lock:
            job_ids = list(self._observers) + [
                job_id for job_id in self._timers if job_id not in self._observers
            ]
        for job_id in job_ids:
            self.stop(job_id)

    def running(self, job_id: str) -> bool:
        with self._lock:
            observer = self._observers.get(job_id)
        return bool(observer and observer.is_alive())

    def watched_jobs(self) -> set[str]:
        with self._lock:
            return set(self._observers)

    def _debounce(self, job_id: str) -> None:
        with self._lock:
            timer = self._timers.get(job_id)
            if timer is not None:
                timer.cancel()
            timer = threading.Timer(
                self.debounce_seconds,
                self._fire,
                args=(job_id,),
            )
            timer.daemon = True
            self._timers[job_id] = timer
            timer.start()

    def refresh(self, job_id: str) -> None:
        """Reattach watches when a watched directory was replaced."""
        with self._lock:
            spec = self._specs.get(job_id)
            identities = dict(self._watch_identities.get(job_id, {}))
        if spec is None or not identities:
            return
        missing = False
        changed = False
        for raw_path, identity in identities.items():
            try:
                stat = Path(raw_path).stat()
            except OSError:
                missing = True
                break
            if (stat.st_dev, stat.st_ino) != identity:
                changed = True
        # Do not detach the other watches while one root is absent. Once every
        # root exists again, any changed inode means the replacement directory
        # is not covered by the old recursive watch.
        if missing or not changed:
            return
        roots, settings = spec
        try:
            self.start(job_id, roots, settings)
        except (OSError, ValueError):
            return
        self._debounce(job_id)

    def _health_loop(self) -> None:
        while not self._stopped.wait(self.health_seconds):
            with self._lock:
                job_ids = list(self._specs)
            for job_id in job_ids:
                self.refresh(job_id)

    def _fire(self, job_id: str) -> None:
        with self._lock:
            self._timers.pop(job_id, None)
            active = job_id in self._observers
        if active:
            self.on_change(job_id)
