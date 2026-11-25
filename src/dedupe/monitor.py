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
    ) -> None:
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self._lock = threading.RLock()
        self._observers: dict[str, Observer] = {}
        self._timers: dict[str, threading.Timer] = {}

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
        with self._lock:
            self._observers[job_id] = observer
        return watched

    def stop(self, job_id: str) -> None:
        with self._lock:
            observer = self._observers.pop(job_id, None)
            timer = self._timers.pop(job_id, None)
        if timer is not None:
            timer.cancel()
        if observer is not None:
            observer.stop()
            observer.join(timeout=2.0)

    def stop_all(self) -> None:
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

    def _fire(self, job_id: str) -> None:
        with self._lock:
            self._timers.pop(job_id, None)
            active = job_id in self._observers
        if active:
            self.on_change(job_id)
