"""Filesystem watching for monitored cleanup jobs."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .scanner import BUILTIN_EXCLUSIONS, _exclusion_matcher, is_in_photos_library, media_extensions

# A recursive directory watch uses None. A parent watch for individual files
# or absent roots accepts only events for the named immediate children.
_WatchPlan = dict[str, frozenset[str] | None]


def _event_path(path: Path) -> Path:
    """Return the spelling macOS uses in FSEvents, including for parent watches."""
    if sys.platform != "darwin":
        return path
    import fcntl

    try:
        fd = os.open(path, getattr(os, "O_EVTONLY", os.O_RDONLY | os.O_NONBLOCK))
        try:
            native = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        finally:
            os.close(fd)
    except OSError:
        # Individual file watches may be installed while the file is absent.
        return path
    return Path(os.fsdecode(native.split(b"\0", 1)[0]))


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFD", name).casefold()


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


@dataclass
class _PendingBatch:
    deadline: float
    latest_deadline: float
    change: MonitorChange = field(default_factory=MonitorChange)
    cancelled: threading.Event = field(default_factory=threading.Event)


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
        child_names: frozenset[str] | None = None,
    ) -> None:
        self.root = root
        self.event_root = _event_path(root)
        self.child_names = child_names
        self._child_spellings: dict[str, str] = {}
        self._child_aliases: dict[str, list[str]] = {}
        for name in sorted(child_names or ()):
            self._child_spellings[_event_path(root / name).name] = name
            self._child_aliases.setdefault(_name_key(name), []).append(name)
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
            and Path(event.src_path) == self.event_root
        ):
            self.root_event_callback()

    def relevant(self, event: FileSystemEvent) -> bool:
        return bool(self.relevant_paths(event))

    def _selected_child(self, name: str) -> str | None:
        selected = self._child_spellings.get(name)
        if selected is None and sys.platform == "darwin":
            # An absent or renamed selection may now have a different native
            # spelling. Verify the filesystem lookup before accepting the event
            # so distinct siblings on a case-sensitive volume stay independent.
            for candidate in self._child_aliases.get(_name_key(name), ()):
                if _event_path(self.root / candidate).name == name:
                    selected = self._child_spellings[name] = candidate
                    break
        return selected

    def relevant_paths(self, event: FileSystemEvent) -> list[str]:
        # Watchdog also reports read-only open/close events on some platforms.
        # Those cannot change the media inventory and scanning them would make
        # the monitor re-scan its own analysis reads.
        if event.event_type not in {"created", "modified", "deleted", "moved"}:
            return []
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
                relative = path.relative_to(self.event_root)
            except ValueError:
                # A move can have one endpoint outside this job.
                continue
            if self.child_names is not None and relative.parts:
                if len(relative.parts) != 1:
                    continue
                selected = self._selected_child(relative.name)
                if selected is None:
                    continue
                relative = Path(selected)
            if not self.include_hidden and any(
                part.startswith(".") for part in relative.parts if part not in (".", "")
            ):
                continue
            excluded = False
            current = Path()
            for part in relative.parts:
                if part in (".", ""):
                    continue
                current /= part
                if self.excluded(part, current):
                    excluded = True
                    break
            if excluded:
                continue
            # A directory event can represent many media files moving at once;
            # file events are cheap to filter by the scan's enabled extensions.
            if event.is_directory or path.suffix.lower() in self.extensions:
                # The scan baseline uses the job's selected root spelling.
                matching.append(str(self.root / relative))
        return matching


class FolderMonitor:
    """Manage the filesystem watches for each monitored cleanup job."""

    def __init__(
        self,
        on_change: Callable[[str, MonitorChange | None], None],
        *,
        debounce_seconds: float = 2.0,
        health_seconds: float = 10.0,
    ) -> None:
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self.health_seconds = health_seconds
        self._lock = threading.RLock()
        self._observers: dict[str, Observer] = {}
        self._batches: dict[str, _PendingBatch] = {}
        self._specs: dict[str, tuple[tuple[str, ...], dict]] = {}
        self._runtime_locks: dict[str, threading.Lock] = {}
        self._watch_identities: dict[str, dict[str, tuple[int, int]]] = {}
        self._watch_scopes: dict[str, _WatchPlan] = {}
        self._watch_errors: dict[str, str] = {}
        self._root_losses: dict[str, set[str]] = {}
        self._health_condition = threading.Condition(self._lock)
        self._health_thread: threading.Thread | None = None
        self._closed = False

    @staticmethod
    def _watch_plan(roots: Iterable[str], settings: dict | None = None) -> _WatchPlan:
        settings = settings or {}
        exclusions = settings.get("exclusions") or []
        if isinstance(exclusions, str):
            exclusions = exclusions.split(",")
        patterns = {e.strip().lower() for e in exclusions if str(e).strip()}
        patterns |= BUILTIN_EXCLUSIONS
        excluded = _exclusion_matcher(patterns)
        include_hidden = bool(settings.get("include_hidden", False))

        def same_filter_semantics(relative: Path) -> bool:
            current = Path()
            for part in relative.parts:
                if part in (".", ""):
                    continue
                if not include_hidden and part.startswith("."):
                    return False
                current /= part
                if excluded(part, current):
                    return False
            return True

        directories: set[Path] = set()
        children: dict[Path, set[str]] = {}
        for raw_root in roots:
            try:
                root = Path(raw_root).expanduser().resolve(strict=False)
                if is_in_photos_library(root):
                    continue
                directory = root.is_dir()
                watch_root = root if directory else root.parent
                if not directory and not watch_root.is_dir():
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
            if directory:
                directories.add(root)
            else:
                children.setdefault(watch_root, set()).add(root.name)

        plan: _WatchPlan = {}
        recursive_roots: list[Path] = []
        anchors = sorted(directories | children.keys(), key=lambda path: (len(path.parts), path))
        for anchor in anchors:
            targets = [anchor] if anchor in directories else [anchor / n for n in children[anchor]]
            if any(
                all(target.is_relative_to(parent) and same_filter_semantics(target.relative_to(parent))
                    for target in targets)
                for parent in recursive_roots
            ):
                continue
            if anchor in directories:
                recursive_roots.append(anchor)
                plan[str(anchor)] = None
            else:
                plan[str(anchor)] = frozenset(children[anchor])
        return plan

    def _make_observer(
        self,
        job_id: str,
        watched: _WatchPlan,
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
        for key, child_names in watched.items():
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
                child_names=child_names,
            )
            observer.schedule(handler, str(handler.event_root), recursive=child_names is None)
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
                if self._closed:
                    raise ValueError("Folder monitoring has shut down")
                # Keep the spec even when a removable drive is fully absent. The
                # health loop uses it to attach a watcher when the drive returns.
                self._specs[job_id] = spec
                self._watch_errors.pop(job_id, None)
                self._root_losses.pop(job_id, None)
                if self._health_thread is None:
                    self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
                    self._health_thread.start()
            watched = self._watch_plan(roots, settings)
            if not watched:
                raise ValueError("No existing folders to monitor")
            self._attach(job_id, watched, settings)
            return list(watched)

    def _attach(self, job_id: str, watched: _WatchPlan, settings: dict) -> None:
        observer = self._make_observer(job_id, watched, settings)
        try:
            observer.start()
        except (OSError, ValueError, SystemError) as exc:
            self._shutdown_observer(observer)
            if isinstance(exc, SystemError):
                # The macOS FSEvents extension uses SystemError for a stream
                # that cannot start. Report it through the normal watch failure path.
                raise OSError(f"Could not start folder monitoring: {exc}") from exc
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
            self._watch_scopes[job_id] = watched
            self._watch_errors.pop(job_id, None)

    @staticmethod
    def _shutdown_observer(observer: Observer) -> None:
        observer.stop()
        with contextlib.suppress(RuntimeError):
            observer.join(timeout=2.0)

    def _stop_runtime(self, job_id: str) -> None:
        with self._lock:
            observer = self._observers.pop(job_id, None)
            batch = self._batches.pop(job_id, None)
            self._watch_identities.pop(job_id, None)
            self._watch_scopes.pop(job_id, None)
            if batch is not None:
                batch.cancelled.set()
        if observer is not None:
            self._shutdown_observer(observer)

    def stop(self, job_id: str) -> None:
        runtime_lock = self._runtime_lock(job_id)
        with runtime_lock:
            self._stop_runtime(job_id)
            with self._lock:
                self._specs.pop(job_id, None)
                self._watch_errors.pop(job_id, None)
                self._root_losses.pop(job_id, None)
                if not self._specs:
                    self._health_condition.notify_all()

    def stop_all(self) -> None:
        with self._lock:
            self._closed = True
            self._health_condition.notify_all()
            thread = self._health_thread
            job_ids = set(self._observers) | set(self._batches) | set(self._specs)
        for job_id in job_ids:
            self.stop(job_id)
        if thread is not None:
            thread.join(timeout=2.0)

    def watch_error(self, job_id: str) -> str | None:
        with self._lock:
            return self._watch_errors.get(job_id)

    def watched_roots(self, job_id: str) -> list[str]:
        with self._lock:
            return list(self._watch_scopes.get(job_id, {}))

    def running(self, job_id: str) -> bool:
        with self._lock:
            observer = self._observers.get(job_id)
            return bool(
                observer and observer.is_alive()
                and all(emitter.is_alive() for emitter in observer.emitters)
            )

    def watched_jobs(self) -> set[str]:
        with self._lock:
            return set(self._observers)

    def _record_change(
        self, job_id: str, paths: list[str], directory: bool, *, offline: bool = False
    ) -> None:
        with self._lock:
            if job_id not in self._observers and not offline:
                return
            now = time.monotonic()
            quiet_until = now + self.debounce_seconds
            batch = self._batches.get(job_id)
            if batch is not None:
                batch.change.add(paths, directory)
                batch.deadline = min(quiet_until, batch.latest_deadline)
                return
            batch = _PendingBatch(quiet_until, now + self.debounce_seconds * 5)
            batch.change.add(paths, directory)
            self._batches[job_id] = batch
            threading.Thread(target=self._deliver_batch, args=(job_id, batch), daemon=True).start()

    def _deliver_batch(self, job_id: str, batch: _PendingBatch) -> None:
        # A burst only extends the deadline. The same worker checks it again at
        # the end of each quiet period, and never waits past the maximum delay.
        while True:
            with self._lock:
                if self._batches.get(job_id) is not batch:
                    return
                remaining = batch.deadline - time.monotonic()
                if remaining <= 0:
                    del self._batches[job_id]
                    break
            if batch.cancelled.wait(remaining):
                return
        self.on_change(job_id, batch.change)

    def _mark_root_lost(self, job_id: str, root: str) -> None:
        with self._lock:
            self._root_losses.setdefault(job_id, set()).add(root)

    def refresh(self, job_id: str) -> None:
        """Reconfigure watches after roots disappear or are replaced."""
        runtime_lock = self._runtime_lock(job_id)
        with runtime_lock:
            with self._lock:
                spec = self._specs.get(job_id)
                if self._closed or spec is None:
                    return
                identities = dict(self._watch_identities.get(job_id, {}))
                previous_plan = self._watch_scopes.get(job_id, {})
                had_observer = job_id in self._observers
                previous_error = self._watch_errors.get(job_id)
                lost = set(self._root_losses.get(job_id, set()))
            roots, settings = spec
            desired = self._watch_plan(roots, settings)
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
                or desired != previous_plan
                or set(desired) != set(identities)
                or (bool(desired) and not self.running(job_id))
                or (not desired and previous_error is not None)
            )
            if not needs_restart:
                return

            if had_observer:
                self._stop_runtime(job_id)
            with self._lock:
                self._root_losses.pop(job_id, None)
            if not desired:
                with self._lock:
                    self._watch_errors.pop(job_id, None)
                # The old observer's pending loss event was cancelled during
                # teardown. Notify the app once even if no folder can be watched.
                self._record_change(job_id, [], directory=True, offline=True)
                return
            try:
                self._attach(job_id, desired, settings)
            except (OSError, ValueError) as exc:
                message = str(exc)
                if message != previous_error:
                    with self._lock:
                        self._watch_errors[job_id] = message
                    self._record_change(job_id, [], directory=True, offline=True)
                return
            # New events after reattachment must not narrow the catch-up to a
            # single path: other arrivals may have occurred while disconnected.
            self._record_change(job_id, [], directory=True)

    def _health_loop(self) -> None:
        while True:
            with self._health_condition:
                if self._health_condition.wait_for(
                    lambda: self._closed or not self._specs, timeout=self.health_seconds
                ):
                    self._health_thread = None
                    return
                job_ids = list(self._specs)
            for job_id in job_ids:
                try:
                    self.refresh(job_id)
                except (OSError, ValueError):
                    continue
