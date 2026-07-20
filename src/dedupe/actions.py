"""Safe file actions: trash, quarantine, or isolate groups for review."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .exact import file_sha256
from .models import (
    DuplicateGroup,
    FileRecord,
    GroupKind,
    ScanResult,
    effective_selected_paths,
)


@dataclass
class ActionItem:
    path: str
    ok: bool
    action: str
    destination: str | None = None
    error: str | None = None
    group_id: str | None = None


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
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
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


def _unique_dest(dest_dir: Path, name: str) -> Path:
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    n = 1
    while True:
        candidate = dest_dir / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _write_action_log(
    result: ActionResult,
    log_dir: str | Path | None = None,
) -> None:
    log_base = Path(log_dir) if log_dir else Path.home() / ".cache" / "dedupe" / "logs"
    try:
        log_base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        log_path = log_base / f"action-{stamp}-{result.session_id[:8]}.json"
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
        if home_trash.exists():
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


def _snapshot_trash(dirs: list[Path]) -> dict[Path, set[str]]:
    listing: dict[Path, set[str]] = {}
    for d in dirs:
        try:
            listing[d] = {p.name for p in d.iterdir()}
        except OSError:
            listing[d] = set()
    return listing


def _send_to_trash(src: Path) -> Path | None:
    """Move ``src`` to the system Trash and return its resulting Trash path.

    Uses send2trash so the file lands in the OS Trash (Finder Trash on macOS,
    the XDG/FreeDesktop trash on Linux). Returns ``None`` when the file was trashed
    successfully but its precise destination could not be determined, so callers can
    still report success without a recoverable path.
    """
    from send2trash import send2trash

    if not src.exists():
        raise FileNotFoundError(str(src))
    size = src.stat().st_size
    name = src.name
    stem = src.stem
    dirs = _trash_dirs_for(src)
    before = _snapshot_trash(dirs)
    send2trash(str(src))
    after = _snapshot_trash(dirs)

    # The newly added entry (matched by size to avoid Finder's stray .DS_Store and
    # concurrent trashes of same-named files) is our file.
    for d in dirs:
        for added in after.get(d, set()) - before.get(d, set()):
            candidate = d / added
            try:
                if candidate.stat().st_size == size:
                    return candidate
            except OSError:
                continue

    # Fallback: most recently added matching entry by name, in case the size match
    # missed it (e.g. send2trash chose a Trash dir we did not enumerate).
    best: tuple[float, Path] | None = None
    for d in dirs:
        try:
            entries = list(d.iterdir())
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


def _validate_record(record: FileRecord, roots: list[str] | None) -> str | None:
    path = Path(record.path)
    try:
        if path.is_symlink():
            return "refusing to act on a symbolic link"
        if not path.is_file():
            return "file no longer exists"
        if not _path_in_roots(path, roots):
            return "file is outside the scanned roots"
        stat = path.stat()
    except OSError as exc:
        return str(exc)

    if int(stat.st_size) != int(record.size):
        return f"file changed since scan (size {record.size} -> {stat.st_size})"
    if record.device is not None and int(stat.st_dev) != int(record.device):
        return "file changed since scan (device differs)"
    if record.inode is not None and int(stat.st_ino) != int(record.inode):
        return "file changed since scan (inode differs)"
    if record.mtime_ns is not None:
        if int(stat.st_mtime_ns) != int(record.mtime_ns):
            return "file changed since scan (modified time differs)"
    elif abs(float(stat.st_mtime) - float(record.mtime)) >= 0.001:
        return "file changed since scan (modified time differs)"
    return None


def _preflight_action(
    groups: list[DuplicateGroup], paths: list[str], roots: list[str] | None
) -> dict[str, str]:
    """Return path -> error for stale, out-of-scope, or no-longer-exact selections."""
    records = {member.path: member for group in groups for member in group.members}
    errors: dict[str, str] = {}
    for path in paths:
        record = records.get(path)
        if record is None:
            errors[path] = "selection is not present in the scan result"
            continue
        error = _validate_record(record, roots)
        if error:
            errors[path] = error

    current_hashes: dict[str, str] = {}
    selected = set(paths)
    for group in groups:
        if group.kind != GroupKind.EXACT or not selected.intersection(
            member.path for member in group.members
        ):
            continue
        keep = next(
            (member for member in group.members if member.path == group.suggested_keep),
            group.members[0] if group.members else None,
        )
        if keep is None:
            continue
        keep_error = _validate_record(keep, roots)
        if keep_error:
            for member in group.members:
                if member.path in selected:
                    errors[member.path] = f"keeper is stale: {keep_error}"
            continue
        members_to_verify = [
            member for member in group.members if member.path in selected
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
) -> ActionResult:
    """
    action: 'trash' | 'quarantine'
    dry_run: if True, only report what would happen
    kinds: optional set of group kinds (exact/similar/no_humans) to act on;
        None acts on every group.
    """
    action = action.lower().strip()
    if action not in ("trash", "quarantine"):
        raise ValueError("action must be 'trash' or 'quarantine'")

    if kinds:
        groups = [g for g in groups if g.kind.value in kinds]

    paths = collect_selected_paths(groups)
    result = ActionResult(dry_run=dry_run, action=action)
    preflight_errors = _preflight_action(groups, paths, roots)

    qdir: Path | None = None
    if action == "quarantine":
        if not quarantine_dir:
            raise ValueError("quarantine_dir is required for quarantine action")
        qdir = Path(quarantine_dir).expanduser().resolve()
        if not dry_run:
            qdir.mkdir(parents=True, exist_ok=True)

    if preflight_errors and not dry_run:
        for path_str in paths:
            result.items.append(
                ActionItem(
                    path=path_str,
                    ok=False,
                    action=action,
                    error=preflight_errors.get(
                        path_str, "action cancelled because another selected file failed preflight"
                    ),
                )
            )
        result.completed_at = datetime.now(timezone.utc).isoformat()
        _write_action_log(result, log_dir)
        return result

    for path_str in paths:
        src = Path(path_str)
        if path_str in preflight_errors:
            result.items.append(
                ActionItem(
                    path=path_str,
                    ok=False,
                    action=action,
                    error=preflight_errors[path_str],
                )
            )
            continue
        if action == "trash":
            if dry_run:
                result.items.append(
                    ActionItem(path=path_str, ok=True, action="trash", destination="Trash")
                )
                continue
            try:
                destination = _send_to_trash(src)
                result.items.append(
                    ActionItem(
                        path=path_str,
                        ok=True,
                        action="trash",
                        destination=str(destination) if destination else "Trash",
                    )
                )
            except Exception as exc:
                result.items.append(
                    ActionItem(path=path_str, ok=False, action="trash", error=str(exc))
                )
        else:
            assert qdir is not None
            dest = _unique_dest(qdir, src.name)
            if dry_run:
                result.items.append(
                    ActionItem(
                        path=path_str,
                        ok=True,
                        action="quarantine",
                        destination=str(dest),
                    )
                )
                continue
            try:
                if not src.exists():
                    raise FileNotFoundError(path_str)
                shutil.move(str(src), str(dest))
                result.items.append(
                    ActionItem(
                        path=path_str,
                        ok=True,
                        action="quarantine",
                        destination=str(dest),
                    )
                )
            except Exception as exc:
                result.items.append(
                    ActionItem(
                        path=path_str,
                        ok=False,
                        action="quarantine",
                        error=str(exc),
                    )
                )

    result.completed_at = datetime.now(timezone.utc).isoformat()
    _write_action_log(result, log_dir)
    return result


def undo_quarantine(
    action_log: str | Path,
    *,
    dry_run: bool = True,
    log_dir: str | Path | None = None,
) -> ActionResult:
    """Restore a completed quarantine action from its receipt.

    Trash restoration remains a Finder operation because send2trash does not expose
    the final per-volume Trash destination reliably across platforms.
    """
    log_path = Path(action_log).expanduser().resolve()
    data = json.loads(log_path.read_text(encoding="utf-8"))
    if data.get("action") != "quarantine" or data.get("dry_run"):
        raise ValueError("only executed quarantine receipts can be undone")

    result = ActionResult(dry_run=dry_run, action="undo:quarantine")
    planned: list[tuple[Path, Path]] = []
    for item in reversed(data.get("items") or []):
        if not item.get("ok") or not item.get("destination"):
            continue
        quarantined = Path(item["destination"])
        original = Path(item["path"])
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
                )
            )
        else:
            planned.append((quarantined, original))

    if result.fail_count and not dry_run:
        for quarantined, original in planned:
            result.items.append(
                ActionItem(
                    path=str(quarantined),
                    ok=False,
                    action="undo:quarantine",
                    destination=str(original),
                    error="undo cancelled because another item failed preflight",
                )
            )
    else:
        for quarantined, original in planned:
            try:
                if not dry_run:
                    original.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(quarantined), str(original))
                result.items.append(
                    ActionItem(
                        path=str(quarantined),
                        ok=True,
                        action="undo:quarantine",
                        destination=str(original),
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
                    )
                )

    result.completed_at = datetime.now(timezone.utc).isoformat()
    _write_action_log(result, log_dir or log_path.parent)
    return result


def _safe_name(name: str, max_len: int = 80) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._- ()[]" else "_" for c in name)
    cleaned = cleaned.strip(" ._") or "file"
    return cleaned[:max_len]


def _group_folder_name(index: int, group: DuplicateGroup) -> str:
    keep_name = Path(group.suggested_keep or group.members[0].path).stem
    return f"{index:03d}_{group.kind.value}_{group.media_type.value}_n{len(group.members)}_{_safe_name(keep_name, 40)}_{group.id}"


def _link_or_copy(src: Path, dest: Path, mode: str) -> None:
    """
    mode: 'copy' | 'hardlink' | 'symlink' | 'move'
    Falls back to copy if hardlink fails (e.g. cross-volume).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "copy":
        shutil.copy2(src, dest)
        return
    if mode == "move":
        shutil.move(str(src), str(dest))
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
) -> ActionResult:
    """
    Place each duplicate group into its own subfolder under review_dir for human review.

    Default mode is 'copy' (non-destructive). Also supports hardlink, symlink, move.

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
    session_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    session_id = uuid.uuid4().hex
    root = base_root / f"session-{session_stamp}-{session_id[:8]}"
    result = ActionResult(dry_run=dry_run, action=f"isolate:{mode}", review_root=str(root))
    result.session_id = session_id

    filtered = []
    for g in groups:
        if kinds and g.kind.value not in kinds:
            continue
        if len(g.members) < 2 and g.kind != GroupKind.NO_HUMANS:
            continue
        filtered.append(g)

    if not filtered:
        result.completed_at = datetime.now(timezone.utc).isoformat()
        _write_action_log(result, log_dir)
        return result

    validation_errors: dict[str, str] = {}
    for group in filtered:
        for member in group.members:
            error = _validate_record(member, roots)
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
        result.completed_at = datetime.now(timezone.utc).isoformat()
        _write_action_log(result, log_dir)
        return result

    # Separate counters per kind for friendly numbering
    counters: dict[str, int] = {
        GroupKind.EXACT.value: 0,
        GroupKind.SIMILAR.value: 0,
        GroupKind.NO_HUMANS.value: 0,
    }
    index_rows: list[dict] = []

    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)

    for group in filtered:
        counters[group.kind.value] = counters.get(group.kind.value, 0) + 1
        idx = counters[group.kind.value]
        kind_dir = root / group.kind.value
        folder_name = _group_folder_name(idx, group)
        group_dir = kind_dir / folder_name
        result.group_dirs.append(str(group_dir))

        if not dry_run:
            group_dir.mkdir(parents=True, exist_ok=True)

        member_rows: list[dict] = []
        for member in group.members:
            src = Path(member.path)
            is_keep = member.path == group.suggested_keep
            base = src.name
            if mark_keep and is_keep:
                dest_name = f"KEEP__{base}"
            else:
                dest_name = base
            dest = _unique_dest(group_dir, dest_name) if not dry_run else group_dir / dest_name

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

            try:
                if not src.exists():
                    raise FileNotFoundError(member.path)
                _link_or_copy(src, dest, mode)
                result.items.append(
                    ActionItem(
                        path=member.path,
                        ok=True,
                        action=f"isolate:{mode}",
                        destination=str(dest),
                        group_id=group.id,
                    )
                )
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
            except Exception as exc:
                result.items.append(
                    ActionItem(
                        path=member.path,
                        ok=False,
                        action=f"isolate:{mode}",
                        error=str(exc),
                        group_id=group.id,
                    )
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
                "created_at": datetime.now(timezone.utc).isoformat(),
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

    result.completed_at = datetime.now(timezone.utc).isoformat()
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
        f"Non-human files: {result.no_human_files}",
        f"Reclaimable: {format_bytes(result.reclaimable_bytes)}",
    ]
    if result.errors:
        lines.append(f"Errors: {len(result.errors)}")
    return "\n".join(lines)
