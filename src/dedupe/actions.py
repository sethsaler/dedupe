"""Safe file actions: trash, quarantine, or isolate groups for review."""

from __future__ import annotations

import errno
import json
import os
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISLNK, S_ISREG

from .exact import file_sha256
from .models import (
    DuplicateGroup,
    FileRecord,
    GroupKind,
    ReviewPolicy,
    ScanResult,
    effective_selected_paths,
)
from .parallel import map_parallel, resolve_workers
from .receipts import receipt_filename, resolve_log_dir, resolve_receipt_path
from .similar_image import DEFAULT_THRESHOLD as IMG_THRESHOLD
from .similar_image import compute_image_hashes
from .similar_video import (
    DEFAULT_THRESHOLD as VID_THRESHOLD,
)
from .similar_video import (
    compute_video_fingerprint,
    video_fingerprint_distances,
)

# Destructive file work is I/O bound; a small pool hides move/copy latency
# without saturating the disk or the system Trash service.
DEFAULT_ACTION_WORKERS_CAP = 4


class CrossDeviceError(OSError):
    """Raised when an action would cross filesystems without explicit consent."""


@dataclass
class ActionItem:
    path: str
    ok: bool
    action: str
    destination: str | None = None
    error: str | None = None
    group_id: str | None = None
    size: int | None = None


@dataclass
class ActionResult:
    dry_run: bool
    action: str
    items: list[ActionItem] = field(default_factory=list)
    log_path: str | None = None
    review_root: str | None = None
    group_dirs: list[str] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    completed_at: str | None = None
    log_error: str | None = None

    @property
    def success_count(self) -> int:
        return sum(1 for i in self.items if i.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for i in self.items if not i.ok)

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "action": self.action,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "log_path": self.log_path,
            "review_root": self.review_root,
            "group_dirs": list(self.group_dirs),
            "session_id": self.session_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "log_error": self.log_error,
            "items": [asdict(i) for i in self.items],
        }


def collect_selected_paths(groups: list[DuplicateGroup]) -> list[str]:
    """Collect selected paths; keep one only for duplicate groups."""
    return effective_selected_paths(groups)


def _unique_dest(dest_dir: Path, name: str, reserved: set[str] | None = None) -> Path:
    """Free destination path. ``reserved`` holds names already handed out this batch."""
    taken = reserved or set()
    candidate = dest_dir / name
    if not candidate.exists() and str(candidate) not in taken:
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    n = 1
    while True:
        candidate = dest_dir / f"{stem}_{n}{suffix}"
        if not candidate.exists() and str(candidate) not in taken:
            return candidate
        n += 1


def _write_action_log(
    result: ActionResult,
    log_dir: str | Path | None = None,
) -> None:
    log_base = resolve_log_dir(log_dir)
    try:
        log_base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        log_path = log_base / receipt_filename(
            dry_run=result.dry_run, stamp=stamp, session_id=result.session_id
        )
        result.log_path = str(log_path)
        temp_path = log_path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        temp_path.replace(log_path)
    except OSError as exc:
        result.log_path = None
        result.log_error = str(exc)


def _path_in_roots(path: Path, roots: list[str] | None) -> bool:
    if not roots:
        return True
    resolved = path.resolve(strict=True)
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve(strict=False)
        if resolved == root or root in resolved.parents:
            return True
    return False


def _volume_root(path: Path) -> Path:
    """Best-effort mount point of the filesystem holding ``path``."""
    resolved = path.resolve(strict=False)
    cur = resolved.parent
    try:
        cur_dev = cur.stat().st_dev
    except OSError:
        return Path(resolved.anchor)
    while cur.parent != cur:
        try:
            parent_dev = cur.parent.stat().st_dev
        except OSError:
            break
        if parent_dev != cur_dev:
            break
        cur = cur.parent
    return cur


def _device_of(path: Path) -> int | None:
    """``st_dev`` of ``path`` or of its nearest existing ancestor."""
    current = path
    while True:
        try:
            return os.stat(current).st_dev
        except OSError:
            parent = current.parent
            if parent == current:
                return None
            current = parent


def _device_for_dir(directory: Path, cache: dict[str, int | None]) -> int | None:
    """Cached ``st_dev`` lookup; every file in a directory shares its filesystem."""
    key = str(directory)
    if key not in cache:
        cache[key] = _device_of(directory)
    return cache[key]


def cross_device_error(
    src: Path,
    dest_dir: Path,
    cache: dict[str, int | None] | None = None,
) -> str | None:
    """Return an actionable message when moving ``src`` into ``dest_dir`` crosses volumes."""
    cache = {} if cache is None else cache
    src_device = _device_for_dir(src.parent, cache)
    dest_device = _device_for_dir(dest_dir, cache)
    if src_device is None or dest_device is None or src_device == dest_device:
        return None
    return (
        f"cross-device move refused: {src} is on a different volume than {dest_dir}. "
        "Pick a destination on the same volume, or re-run with cross-device moves "
        "allowed (copy, verify, then delete the original)."
    )


def _move_file(src: Path, dest: Path, *, allow_cross_device: bool = False) -> None:
    """Move ``src`` onto ``dest``.

    Same-volume moves are an atomic rename. Cross-volume moves are refused unless
    ``allow_cross_device`` is set, in which case the file is copied, verified by
    size, and only then unlinked, so a failure never loses the original.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dest)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    if not allow_cross_device:
        raise CrossDeviceError(
            errno.EXDEV,
            cross_device_error(src, dest.parent) or "cross-device move refused",
        )
    size = src.stat().st_size
    shutil.copy2(src, dest)
    copied = dest.stat().st_size
    if copied != size:
        dest.unlink(missing_ok=True)
        raise OSError(
            f"cross-device copy verification failed for {src} ({size} -> {copied} bytes)"
        )
    src.unlink()


def _trash_dirs_for(path: Path) -> list[Path]:
    """Plausible system Trash directories that may receive ``path`` (best effort).

    send2trash picks the correct Trash internally; this is only used to locate the
    trashed copy afterwards so an in-app undo can move it back. On macOS that is
    ``~/.Trash`` plus a per-volume ``.Trashes/<uid>``; on Linux the XDG trash and a
    per-mount ``.Trash-<uid>``. Unknown platforms return an empty list, in which case
    the caller reports the file as trashed without a recoverable destination.
    """
    import sys

    dirs: list[Path] = []
    if sys.platform == "darwin":
        home_trash = Path.home() / ".Trash"
        if home_trash.exists():
            dirs.append(home_trash)
        try:
            dev = path.stat().st_dev
            home_dev = home_trash.stat().st_dev if home_trash.exists() else None
        except OSError:
            return dirs
        if dev != home_dev:
            vol_trash = _volume_root(path) / ".Trashes" / str(os.getuid())
            if vol_trash.exists():
                dirs.append(vol_trash)
    elif sys.platform.startswith("linux"):
        home_trash = Path.home() / ".local" / "share" / "Trash" / "files"
        # send2trash creates this directory on first use, so include it even
        # when it does not exist yet — _list_names and _locate handle missing
        # directories gracefully, and the file will be present after the send.
        dirs.append(home_trash)
        try:
            dev = path.stat().st_dev
            home_dev = Path.home().stat().st_dev
        except OSError:
            return dirs
        if dev != home_dev:
            mount_trash = _volume_root(path) / f".Trash-{os.getuid()}"
            if mount_trash.exists():
                dirs.append(mount_trash)
    return dirs


def _list_names(directory: Path) -> set[str]:
    try:
        return {p.name for p in directory.iterdir()}
    except OSError:
        return set()


def _snapshot_trash(dirs: list[Path]) -> dict[Path, set[str]]:
    return {d: _list_names(d) for d in dirs}


class TrashBatch:
    """Trash many files while listing each Trash directory as rarely as possible.

    Each Trash directory is enumerated once, when it is first used. Every send
    then tries the predicted destination (``<trash>/<name>``) and only falls back
    to a directory listing when that misses (e.g. send2trash had to rename because
    of a collision); the listing refreshes the snapshot, so repeated collisions
    still cost one listing each rather than one per file.

    ``send2trash`` and the destination lookup run under one lock, so concurrent
    workers cannot claim each other's freshly trashed entries.
    """

    def __init__(self) -> None:
        self._dirs_by_parent: dict[str, list[Path]] = {}
        self._known: dict[Path, set[str]] = {}
        self._lock = threading.Lock()

    def _dirs_for(self, src: Path) -> list[Path]:
        key = str(src.parent)
        dirs = self._dirs_by_parent.get(key)
        if dirs is None:
            dirs = _trash_dirs_for(src)
            self._dirs_by_parent[key] = dirs
            for directory in dirs:
                self._known.setdefault(directory, _list_names(directory))
        return dirs

    def send(self, src: Path) -> Path | None:
        """Trash ``src`` and return where it landed (``None`` if undeterminable)."""
        from send2trash import send2trash

        if not src.exists():
            raise FileNotFoundError(str(src))
        size = src.stat().st_size
        with self._lock:
            dirs = self._dirs_for(src)
            send2trash(str(src))
            return self._locate(dirs, src.name, src.stem, size)

    def _locate(self, dirs: list[Path], name: str, stem: str, size: int) -> Path | None:
        for directory in dirs:
            known = self._known.setdefault(directory, set())
            if name in known:
                continue
            candidate = directory / name
            try:
                if candidate.stat().st_size == size:
                    known.add(name)
                    return candidate
            except OSError:
                continue

        for directory in dirs:
            current = _list_names(directory)
            added = current - self._known.get(directory, set())
            self._known[directory] = current
            for entry in sorted(added):
                candidate = directory / entry
                try:
                    if candidate.stat().st_size == size:
                        return candidate
                except OSError:
                    continue

        # Last resort: newest entry whose name matches, in case send2trash chose a
        # Trash directory we never enumerated.
        best: tuple[float, Path] | None = None
        for directory in dirs:
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for p in entries:
                if p.name == name or p.name.startswith(f"{stem} "):
                    try:
                        mtime = p.stat().st_mtime
                    except OSError:
                        continue
                    if best is None or mtime > best[0]:
                        best = (mtime, p)
        return best[1] if best else None


def _send_to_trash(src: Path, batch: TrashBatch | None = None) -> Path | None:
    """Move ``src`` to the system Trash and return its resulting Trash path.

    Uses send2trash so the file lands in the OS Trash (Finder Trash on macOS,
    the XDG/FreeDesktop trash on Linux). Returns ``None`` when the file was trashed
    successfully but its precise destination could not be determined, so callers can
    still report success without a recoverable path. Pass a shared :class:`TrashBatch`
    when trashing many files so the Trash directories are only enumerated once.
    """
    return (batch or TrashBatch()).send(src)


SYMLINK_REFUSAL = "refusing to act on a symbolic link"


def validate_record_with_stat(
    record: FileRecord, roots: list[str] | None = None
) -> tuple[str | None, os.stat_result | None]:
    """Validate ``record`` from a single ``lstat`` and return that stat alongside.

    One ``lstat`` answers "is it a symlink", "is it a regular file", and every
    identity comparison, so callers never need to stat the same file again.
    """
    path = Path(record.path)
    try:
        st = os.lstat(path)
        if S_ISLNK(st.st_mode):
            return SYMLINK_REFUSAL, None
        if not S_ISREG(st.st_mode):
            return "file no longer exists", None
        if not _path_in_roots(path, roots):
            return "file is outside the scanned roots", st
    except OSError as exc:
        return str(exc), None

    if int(st.st_size) != int(record.size):
        return f"file changed since scan (size {record.size} -> {st.st_size})", st
    if record.device is not None and int(st.st_dev) != int(record.device):
        return "file changed since scan (device differs)", st
    if record.inode is not None and int(st.st_ino) != int(record.inode):
        return "file changed since scan (inode differs)", st
    if record.mtime_ns is not None:
        if int(st.st_mtime_ns) != int(record.mtime_ns):
            return "file changed since scan (modified time differs)", st
    elif abs(float(st.st_mtime) - float(record.mtime)) >= 0.001:
        return "file changed since scan (modified time differs)", st
    return None, st


def validate_file_record(record: FileRecord, roots: list[str] | None = None) -> str | None:
    """Side-effect-free validation of a scanned file's identity and root scope."""
    return validate_record_with_stat(record, roots)[0]


def _is_metadata_drift_error(error: str, stat, record: FileRecord) -> bool:
    """True when ``error`` only reports mtime/inode/device drift and size matches."""
    if stat is None:
        return False
    lowered = error.lower()
    has_drift = any(
        phrase in lowered
        for phrase in ("modified time", "inode", "device")
    )
    if not has_drift:
        return False
    return int(stat.st_size) == int(record.size)


def _refresh_keeper_stats(keep: FileRecord, st: os.stat_result | None = None) -> None:
    """Update ``keep`` stat fields from the file's current metadata."""
    st = st if st is not None else Path(keep.path).stat()
    keep.size = int(st.st_size)
    keep.mtime_ns = int(st.st_mtime_ns)
    keep.mtime = float(st.st_mtime)
    keep.inode = int(st.st_ino)
    keep.device = int(st.st_dev)


def _revalidate_keeper(
    keep: FileRecord,
    group: DuplicateGroup,
    st: os.stat_result | None = None,
) -> tuple[bool, str | None]:
    """Rehash ``keep`` to tolerate mtime/inode/device drift.

    Returns ``(valid, error)``. ``error`` is set when the keeper cannot be
    hashed (e.g., an evicted iCloud dataless file), so the caller can skip
    the group rather than abort the whole batch. ``st`` reuses a stat the
    caller already took.
    """
    path = keep.path
    try:
        if keep.sha256:
            if file_sha256(path) == keep.sha256:
                _refresh_keeper_stats(keep, st)
                return True, None
            return False, "retained member content no longer matches its scan hash"

        if keep.phash:
            current_phash, *_ = compute_image_hashes(path)
            if current_phash:
                import imagehash

                distance = imagehash.hex_to_hash(keep.phash) - imagehash.hex_to_hash(current_phash)
                if distance <= IMG_THRESHOLD:
                    _refresh_keeper_stats(keep, st)
                    return True, None
            return False, "retained member perceptual hash no longer matches"

        if keep.video_fingerprint:
            current_fp, *_ = compute_video_fingerprint(path)
            if current_fp:
                distances = video_fingerprint_distances(keep.video_fingerprint, current_fp)
                if distances and all(d <= VID_THRESHOLD for d in distances):
                    _refresh_keeper_stats(keep, st)
                    return True, None
            return False, "retained member video fingerprint no longer matches"

        # No hashes available: the keeper exists and size matches, which is
        # sufficient because the retained copy is never being deleted.
        _refresh_keeper_stats(keep, st)
        return True, None
    except Exception as exc:
        return False, f"retained member is unavailable: {exc}"


def _preflight_action(
    groups: list[DuplicateGroup],
    paths: list[str],
    roots: list[str] | None,
    *,
    check_paths: set[str] | None = None,
    dest_dir: Path | None = None,
    device_cache: dict[str, int | None] | None = None,
) -> dict[str, str]:
    """Return path -> error for stale, out-of-scope, or no-longer-exact selections.

    When ``dest_dir`` is given, selections that would have to cross a filesystem
    boundary to reach it are rejected here, before anything is touched.
    """
    records = {member.path: member for group in groups for member in group.members}
    errors: dict[str, str] = {}
    paths_to_check = paths if check_paths is None else [p for p in paths if p in check_paths]
    for path in paths_to_check:
        record = records.get(path)
        if record is None:
            errors[path] = "selection is not present in the scan result"
            continue
        error = validate_file_record(record, roots)
        if not error and dest_dir is not None:
            error = cross_device_error(Path(path), dest_dir, device_cache)
        if error:
            errors[path] = error

    selected = set(paths)
    for group in groups:
        member_paths = {member.path for member in group.members}
        touched = selected.intersection(member_paths)
        if not touched or (check_paths is not None and not touched.intersection(check_paths)):
            continue
        if group.policy == ReviewPolicy.INDEPENDENT_CANDIDATES:
            continue
        retained = [member for member in group.members if member.path not in selected]
        keep = next(
            (member for member in retained if member.path == group.suggested_keep),
            retained[0] if retained else None,
        )
        if keep is None:
            for path in touched:
                if check_paths is None or path in check_paths:
                    errors[path] = "refusing to remove every member of a duplicate group"
            continue
        keep_error = validate_file_record(keep, roots)
        if keep_error:
            try:
                current_stat = Path(keep.path).stat()
            except OSError:
                current_stat = None
            if _is_metadata_drift_error(keep_error, current_stat, keep):
                valid, rehash_error = _revalidate_keeper(keep, group, current_stat)
                if valid:
                    keep_error = None
                else:
                    keep_error = rehash_error
            if keep_error:
                for path in touched:
                    if check_paths is None or path in check_paths:
                        errors[path] = f"retained member is stale: {keep_error}"
                continue
        if group.kind != GroupKind.EXACT:
            continue
        current_hashes: dict[str, str] = {}
        members_to_verify = [
            member
            for member in group.members
            if member.path in touched
            and (check_paths is None or member.path in check_paths)
        ] + [keep]
        try:
            for member in members_to_verify:
                if member.path in errors:
                    continue
                current = current_hashes.setdefault(
                    member.path, file_sha256(member.path)
                )
                if member.sha256 and current != member.sha256:
                    errors[member.path] = "file content no longer matches its scan hash"
            keep_hash = current_hashes.setdefault(keep.path, file_sha256(keep.path))
            for member in members_to_verify:
                if member.path == keep.path or member.path in errors:
                    continue
                if current_hashes[member.path] != keep_hash:
                    errors[member.path] = "file is no longer an exact duplicate of the keeper"
        except OSError as exc:
            for member in members_to_verify:
                if member.path in selected:
                    errors.setdefault(member.path, str(exc))
    return errors


def apply_actions(
    groups: list[DuplicateGroup],
    *,
    action: str = "trash",
    quarantine_dir: str | Path | None = None,
    dry_run: bool = True,
    log_dir: str | Path | None = None,
    roots: list[str] | None = None,
    kinds: set[str] | None = None,
    safety_groups: list[DuplicateGroup] | None = None,
    allow_cross_device: bool = False,
    workers: int | None = None,
) -> ActionResult:
    """
    action: 'trash' | 'quarantine'
    dry_run: if True, only report what would happen
    kinds: optional set of group kinds (exact/similar/no_humans) to act on;
        None acts on every group.
    allow_cross_device: quarantine onto another filesystem by copying, verifying
        the copy, then unlinking the original. Off by default, in which case
        cross-device selections fail in preflight before anything is touched.
    workers: bounded thread pool size for the per-file work (None/0 = auto).
        Each file is still revalidated immediately before its own operation and
        results stay in input order.
    """
    action = action.lower().strip()
    if action not in ("trash", "quarantine"):
        raise ValueError("action must be 'trash' or 'quarantine'")

    all_safety_groups = safety_groups if safety_groups is not None else groups
    candidate_groups = groups
    if kinds:
        candidate_groups = [g for g in groups if g.kind.value in kinds]

    paths = effective_selected_paths(
        candidate_groups,
        protection_groups=all_safety_groups,
    )
    # Candidate scope decides what may be acted on; every duplicate group decides
    # whether doing so would still leave a retained copy (including overlaps).
    selected = set(paths)
    for group in all_safety_groups:
        if group.policy == ReviewPolicy.INDEPENDENT_CANDIDATES or not group.members:
            continue
        member_paths = {member.path for member in group.members}
        if member_paths <= selected:
            keep = group.suggested_keep if group.suggested_keep in member_paths else group.members[0].path
            selected.discard(keep)
    paths = [path for path in paths if path in selected]
    result = ActionResult(dry_run=dry_run, action=action)

    qdir: Path | None = None
    if action == "quarantine":
        if not quarantine_dir:
            raise ValueError("quarantine_dir is required for quarantine action")
        qdir = Path(quarantine_dir).expanduser().resolve()
        if not dry_run:
            qdir.mkdir(parents=True, exist_ok=True)

    device_cache: dict[str, int | None] = {}
    guarded_dest = qdir if (qdir is not None and not allow_cross_device) else None
    preflight_errors = _preflight_action(
        all_safety_groups,
        paths,
        roots,
        dest_dir=guarded_dest,
        device_cache=device_cache,
    )

    sizes = {
        member.path: member.size
        for group in all_safety_groups
        for member in group.members
    }

    # Names are reserved up front so concurrent workers never pick the same
    # quarantine destination.
    destinations: dict[str, Path] = {}
    if qdir is not None:
        reserved: set[str] = set()
        for path_str in paths:
            dest = _unique_dest(qdir, Path(path_str).name, reserved)
            reserved.add(str(dest))
            destinations[path_str] = dest

    trash_batch = TrashBatch()

    def _apply_one(path_str: str) -> ActionItem:
        src = Path(path_str)
        size = sizes.get(path_str)
        if path_str in preflight_errors:
            return ActionItem(
                path=path_str,
                ok=False,
                action=action,
                error=preflight_errors[path_str],
                size=size,
            )
        if dry_run:
            destination = "Trash" if action == "trash" else str(destinations[path_str])
            return ActionItem(
                path=path_str, ok=True, action=action, destination=destination, size=size
            )
        # Revalidate immediately before this file's destructive operation.
        immediate_errors = _preflight_action(
            all_safety_groups,
            paths,
            roots,
            check_paths={path_str},
            dest_dir=guarded_dest,
            device_cache=device_cache,
        )
        if path_str in immediate_errors:
            return ActionItem(
                path=path_str,
                ok=False,
                action=action,
                error=immediate_errors[path_str],
                size=size,
            )
        try:
            if action == "trash":
                trashed = _send_to_trash(src, trash_batch)
                destination = str(trashed) if trashed else "Trash"
            else:
                dest = destinations[path_str]
                _move_file(src, dest, allow_cross_device=allow_cross_device)
                destination = str(dest)
            return ActionItem(
                path=path_str, ok=True, action=action, destination=destination, size=size
            )
        except Exception as exc:
            return ActionItem(path=path_str, ok=False, action=action, error=str(exc), size=size)

    if dry_run:
        result.items = [_apply_one(path_str) for path_str in paths]
    else:
        result.items = map_parallel(
            _apply_one,
            paths,
            workers=resolve_workers(workers, cap=DEFAULT_ACTION_WORKERS_CAP),
        )

    result.completed_at = datetime.now(UTC).isoformat()
    _write_action_log(result, log_dir)
    return result


def undo_quarantine(
    action_log: str | Path,
    *,
    dry_run: bool = True,
    log_dir: str | Path | None = None,
    receipt_dir: str | Path | None = None,
) -> ActionResult:
    """Restore a completed quarantine action from its receipt.

    ``action_log`` may be a path to a receipt or a receipt id (see
    :mod:`dedupe.receipts`); ids are resolved against ``receipt_dir`` (default:
    the standard log directory).

    Trash restoration remains a Finder operation because send2trash does not expose
    the final per-volume Trash destination reliably across platforms.
    """
    log_path = resolve_receipt_path(action_log, receipt_dir)
    data = json.loads(log_path.read_text(encoding="utf-8"))
    if data.get("action") != "quarantine" or data.get("dry_run"):
        raise ValueError("only executed quarantine receipts can be undone")

    result = ActionResult(dry_run=dry_run, action="undo:quarantine")
    planned: list[tuple[Path, Path, int | None]] = []
    for item in reversed(data.get("items") or []):
        if not item.get("ok") or not item.get("destination"):
            continue
        quarantined = Path(item["destination"])
        original = Path(item["path"])
        size = item.get("size")
        error = None
        if not quarantined.is_file():
            error = "quarantined file no longer exists"
        elif original.exists() or original.is_symlink():
            error = "original path is already occupied"
        if error:
            result.items.append(
                ActionItem(
                    path=str(quarantined),
                    ok=False,
                    action="undo:quarantine",
                    destination=str(original),
                    error=error,
                    size=size,
                )
            )
        else:
            planned.append((quarantined, original, size))

    if result.fail_count and not dry_run:
        for quarantined, original, size in planned:
            result.items.append(
                ActionItem(
                    path=str(quarantined),
                    ok=False,
                    action="undo:quarantine",
                    destination=str(original),
                    error="undo cancelled because another item failed preflight",
                    size=size,
                )
            )
    else:
        for quarantined, original, size in planned:
            try:
                if not dry_run:
                    original.parent.mkdir(parents=True, exist_ok=True)
                    # Restores are always allowed to cross volumes: the quarantine
                    # directory may legitimately live on another disk.
                    _move_file(quarantined, original, allow_cross_device=True)
                result.items.append(
                    ActionItem(
                        path=str(quarantined),
                        ok=True,
                        action="undo:quarantine",
                        destination=str(original),
                        size=size,
                    )
                )
            except OSError as exc:
                result.items.append(
                    ActionItem(
                        path=str(quarantined),
                        ok=False,
                        action="undo:quarantine",
                        destination=str(original),
                        error=str(exc),
                        size=size,
                    )
                )

    result.completed_at = datetime.now(UTC).isoformat()
    _write_action_log(result, log_dir or log_path.parent)
    return result


def _safe_name(name: str, max_len: int = 80) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._- ()[]" else "_" for c in name)
    cleaned = cleaned.strip(" ._") or "file"
    return cleaned[:max_len]


def _group_folder_name(index: int, group: DuplicateGroup) -> str:
    keep_name = Path(group.suggested_keep or group.members[0].path).stem
    return f"{index:03d}_{group.kind.value}_{group.media_type.value}_n{len(group.members)}_{_safe_name(keep_name, 40)}_{group.id}"


def _link_or_copy(src: Path, dest: Path, mode: str, *, allow_cross_device: bool = False) -> None:
    """
    mode: 'copy' | 'hardlink' | 'symlink' | 'move'
    Falls back to copy if hardlink fails (e.g. cross-volume).

    Symlinked sources are refused, exactly like the destructive actions: copying
    or moving one would silently act on the link's target instead.
    """
    if src.is_symlink():
        raise ValueError(SYMLINK_REFUSAL)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "copy":
        shutil.copy2(src, dest)
        return
    if mode == "move":
        _move_file(src, dest, allow_cross_device=allow_cross_device)
        return
    if mode == "symlink":
        os.symlink(src.resolve(), dest)
        return
    if mode == "hardlink":
        try:
            os.link(src, dest)
            return
        except OSError:
            shutil.copy2(src, dest)
            return
    raise ValueError(f"unknown isolate mode: {mode}")


def default_review_dir(roots: list[str] | None, groups: list[DuplicateGroup] | None = None) -> Path:
    """
    Prefer a folder *inside the scanned source*, not Desktop/repo.

    Order:
      1. First scan root (if it's a directory)
      2. Parent of first scan root (if root was a file)
      3. Common parent of group member paths
      4. cwd fallback
    """
    for r in roots or []:
        p = Path(r).expanduser()
        try:
            p = p.resolve()
        except OSError:
            p = p.absolute()
        if p.is_dir():
            return p / "_Dedupe Review"
        if p.parent.is_dir():
            return p.parent / "_Dedupe Review"

    # Infer from group member paths
    paths: list[Path] = []
    for g in groups or []:
        for m in g.members:
            paths.append(Path(m.path))
    if paths:
        try:
            common = Path(os.path.commonpath([str(p.parent) for p in paths]))
            if common.is_dir():
                return common / "_Dedupe Review"
        except ValueError:
            pass
        return paths[0].parent / "_Dedupe Review"

    return Path.cwd() / "_Dedupe Review"


def isolate_groups(
    groups: list[DuplicateGroup],
    review_dir: str | Path | None = None,
    *,
    mode: str = "copy",
    kinds: set[str] | None = None,
    dry_run: bool = False,
    log_dir: str | Path | None = None,
    mark_keep: bool = True,
    roots: list[str] | None = None,
    allow_cross_device: bool = False,
    workers: int | None = None,
) -> ActionResult:
    """
    Place each duplicate group into its own subfolder under review_dir for human review.

    Default mode is 'copy' (non-destructive). Also supports hardlink, symlink, move.

    ``mode='move'`` refuses to cross a filesystem boundary unless
    ``allow_cross_device`` is set (then it copies, verifies, and unlinks).
    ``workers`` bounds the thread pool used to place files; results stay ordered.

    Layout:
      review_dir/
        exact/
          001_exact_image_n2_.../
            KEEP__photo.jpg
            photo_copy.jpg
            _group.json
        similar/
          001_similar_image_n2_.../
            ...
        _review_index.json
    """
    mode = mode.lower().strip()
    if mode not in ("copy", "hardlink", "symlink", "move"):
        raise ValueError("mode must be copy, hardlink, symlink, or move")

    if review_dir is None:
        base_root = default_review_dir(roots, groups)
    else:
        base_root = Path(review_dir).expanduser().resolve()
    session_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    session_id = uuid.uuid4().hex
    root = base_root / f"session-{session_stamp}-{session_id[:8]}"
    result = ActionResult(dry_run=dry_run, action=f"isolate:{mode}", review_root=str(root))
    result.session_id = session_id

    filtered = []
    for g in groups:
        if kinds and g.kind.value not in kinds:
            continue
        if len(g.members) < 2 and g.policy != ReviewPolicy.INDEPENDENT_CANDIDATES:
            continue
        filtered.append(g)

    if not filtered:
        result.completed_at = datetime.now(UTC).isoformat()
        _write_action_log(result, log_dir)
        return result

    validation_errors: dict[str, str] = {}
    device_cache: dict[str, int | None] = {}
    for group in filtered:
        for member in group.members:
            error = validate_file_record(member, roots)
            if not error and mode == "move" and not allow_cross_device:
                error = cross_device_error(Path(member.path), root, device_cache)
            if error:
                validation_errors[member.path] = error
    if validation_errors and not dry_run:
        for group in filtered:
            for member in group.members:
                result.items.append(
                    ActionItem(
                        path=member.path,
                        ok=False,
                        action=f"isolate:{mode}",
                        group_id=group.id,
                        error=validation_errors.get(
                            member.path,
                            "isolate cancelled because another file failed preflight",
                        ),
                    )
                )
        result.completed_at = datetime.now(UTC).isoformat()
        _write_action_log(result, log_dir)
        return result

    # Separate counters per kind for friendly numbering
    counters: dict[str, int] = {
        GroupKind.EXACT.value: 0,
        GroupKind.SIMILAR.value: 0,
        GroupKind.NO_HUMANS.value: 0,
        GroupKind.LOW_RESOLUTION.value: 0,
        GroupKind.RANDOM_REVIEW.value: 0,
        GroupKind.FACES.value: 0,
    }
    index_rows: list[dict] = []

    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)

    # Plan every destination serially (folder numbering and name reservation must
    # be deterministic), then place the files with a bounded pool.
    plans: list[tuple[DuplicateGroup, Path, list[tuple[FileRecord, Path, bool]]]] = []
    for group in filtered:
        counters[group.kind.value] = counters.get(group.kind.value, 0) + 1
        idx = counters[group.kind.value]
        kind_dir = root / group.kind.value
        folder_name = _group_folder_name(idx, group)
        group_dir = kind_dir / folder_name
        result.group_dirs.append(str(group_dir))

        if not dry_run:
            group_dir.mkdir(parents=True, exist_ok=True)

        reserved: set[str] = set()
        planned_members: list[tuple[FileRecord, Path, bool]] = []
        for member in group.members:
            src = Path(member.path)
            is_keep = member.path == group.suggested_keep
            base = src.name
            dest_name = f"KEEP__{base}" if (mark_keep and is_keep) else base
            if dry_run:
                dest = group_dir / dest_name
            else:
                dest = _unique_dest(group_dir, dest_name, reserved)
                reserved.add(str(dest))
            planned_members.append((member, dest, is_keep))
        plans.append((group, group_dir, planned_members))

    def _place(task: tuple[DuplicateGroup, FileRecord, Path]) -> ActionItem:
        group, member, dest = task
        try:
            src = Path(member.path)
            if not src.exists():
                raise FileNotFoundError(member.path)
            _link_or_copy(src, dest, mode, allow_cross_device=allow_cross_device)
            return ActionItem(
                path=member.path,
                ok=True,
                action=f"isolate:{mode}",
                destination=str(dest),
                group_id=group.id,
                size=member.size,
            )
        except Exception as exc:
            return ActionItem(
                path=member.path,
                ok=False,
                action=f"isolate:{mode}",
                error=str(exc),
                group_id=group.id,
                size=member.size,
            )

    outcomes: list[ActionItem] = []
    if not dry_run:
        tasks = [
            (group, member, dest)
            for group, _group_dir, planned_members in plans
            for member, dest, _is_keep in planned_members
        ]
        outcomes = map_parallel(
            _place,
            tasks,
            workers=resolve_workers(workers, cap=DEFAULT_ACTION_WORKERS_CAP),
        )
    placed = iter(outcomes)

    for group, group_dir, planned_members in plans:
        member_rows: list[dict] = []
        for member, dest, is_keep in planned_members:
            if dry_run:
                error = validation_errors.get(member.path)
                result.items.append(
                    ActionItem(
                        path=member.path,
                        ok=error is None,
                        action=f"isolate:{mode}",
                        destination=str(dest),
                        group_id=group.id,
                        error=error,
                        size=member.size,
                    )
                )
                member_rows.append(
                    {
                        "source": member.path,
                        "dest": str(dest),
                        "is_keep": is_keep,
                        "size": member.size,
                    }
                )
                continue

            item = next(placed)
            result.items.append(item)
            if item.ok:
                member_rows.append(
                    {
                        "source": member.path,
                        "dest": str(dest),
                        "is_keep": is_keep,
                        "size": member.size,
                        "width": member.width,
                        "height": member.height,
                        "mtime": member.mtime,
                    }
                )

        group_meta = {
            "id": group.id,
            "kind": group.kind.value,
            "media_type": group.media_type.value,
            "suggested_keep": group.suggested_keep,
            "reclaimable_bytes": group.reclaimable_bytes,
            "member_count": len(group.members),
            "folder": str(group_dir),
            "members": member_rows,
            "note": (
                "Suggested keep is prefixed KEEP__. "
                "Originals were left in place (copy/hardlink/symlink) "
                "unless mode=move. Hardlinks share file content with the source; "
                "editing either name edits the same underlying file."
            ),
        }
        index_rows.append(group_meta)

        if not dry_run:
            try:
                (group_dir / "_group.json").write_text(
                    json.dumps(group_meta, indent=2), encoding="utf-8"
                )
                readme = [
                    f"Dedupe review group ({group.kind.value})",
                    f"Media: {group.media_type.value}",
                    f"Members: {len(group.members)}",
                    f"Suggested keep: {Path(group.suggested_keep).name if group.suggested_keep else '?'}",
                    f"Reclaimable if extras removed: {format_bytes(group.reclaimable_bytes)}",
                    "",
                    "Files prefixed KEEP__ are the suggested original to keep.",
                    "Review siblings, then delete extras from the SOURCE paths (listed in _group.json),",
                    "or from this folder if you used mode=move.",
                    "",
                    "Sources:",
                ]
                for row in member_rows:
                    tag = "KEEP" if row.get("is_keep") else "    "
                    readme.append(f"  [{tag}] {row.get('source')}")
                (group_dir / "README.txt").write_text("\n".join(readme) + "\n", encoding="utf-8")
            except OSError:
                pass

    if not dry_run:
        try:
            index = {
                "created_at": datetime.now(UTC).isoformat(),
                "mode": mode,
                "session_id": result.session_id,
                "review_base": str(base_root),
                "review_root": str(root),
                "group_count": len(index_rows),
                "groups": index_rows,
            }
            (root / "_review_index.json").write_text(
                json.dumps(index, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    result.completed_at = datetime.now(UTC).isoformat()
    _write_action_log(result, log_dir)
    return result


def format_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            if u == "B":
                return f"{int(size)} {u}"
            return f"{size:.1f} {u}"
        size /= 1024
    return f"{n} B"


def summarize_scan(result: ScanResult) -> str:
    result.recompute_stats()
    lines = [
        f"Roots: {', '.join(result.roots)}",
        f"Files scanned: {len(result.files)}",
        f"Exact groups: {result.exact_groups}",
        f"Similar groups: {result.similar_groups}",
        f"Low-resolution files: {result.low_resolution_files}",
        f"Random review files: {result.random_review_files}",
        f"Non-human files: {result.no_human_files}",
        f"Faces files: {result.faces_files}",
        f"Reclaimable: {format_bytes(result.reclaimable_bytes)}",
    ]
    if result.errors:
        lines.append(f"Errors: {len(result.errors)}")
    return "\n".join(lines)
