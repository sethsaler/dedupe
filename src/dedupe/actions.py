"""Safe file actions: trash, quarantine, or isolate groups for review."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import DuplicateGroup, GroupKind, ScanResult


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
            "items": [asdict(i) for i in self.items],
        }


def collect_selected_paths(groups: list[DuplicateGroup]) -> list[str]:
    """Collect selected paths; enforce keep-at-least-one per group."""
    selected: list[str] = []
    for g in groups:
        members = {m.path for m in g.members}
        picks = [p for p in g.selected_for_removal if p in members]
        # Never remove every member
        if len(picks) >= len(members):
            keep = g.suggested_keep or next(iter(members))
            picks = [p for p in picks if p != keep]
        selected.extend(picks)
    # unique, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for p in selected:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


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
    log_base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_base / f"action-{stamp}.json"
    try:
        log_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        result.log_path = str(log_path)
    except OSError:
        pass


def apply_actions(
    groups: list[DuplicateGroup],
    *,
    action: str = "trash",
    quarantine_dir: str | Path | None = None,
    dry_run: bool = True,
    log_dir: str | Path | None = None,
) -> ActionResult:
    """
    action: 'trash' | 'quarantine'
    dry_run: if True, only report what would happen
    """
    action = action.lower().strip()
    if action not in ("trash", "quarantine"):
        raise ValueError("action must be 'trash' or 'quarantine'")

    paths = collect_selected_paths(groups)
    result = ActionResult(dry_run=dry_run, action=action)

    qdir: Path | None = None
    if action == "quarantine":
        if not quarantine_dir:
            raise ValueError("quarantine_dir is required for quarantine action")
        qdir = Path(quarantine_dir).expanduser().resolve()
        if not dry_run:
            qdir.mkdir(parents=True, exist_ok=True)

    for path_str in paths:
        src = Path(path_str)
        if action == "trash":
            dest_note = "Trash"
            if dry_run:
                result.items.append(
                    ActionItem(path=path_str, ok=True, action="trash", destination=dest_note)
                )
                continue
            try:
                from send2trash import send2trash

                if not src.exists():
                    raise FileNotFoundError(path_str)
                send2trash(str(src))
                result.items.append(
                    ActionItem(path=path_str, ok=True, action="trash", destination=dest_note)
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

    _write_action_log(result, log_dir)
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
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        os.symlink(src.resolve(), dest)
        return
    if mode == "hardlink":
        try:
            if dest.exists():
                dest.unlink()
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
        root = default_review_dir(roots, groups)
    else:
        root = Path(review_dir).expanduser().resolve()
    result = ActionResult(dry_run=dry_run, action=f"isolate:{mode}", review_root=str(root))

    filtered = []
    for g in groups:
        if kinds and g.kind.value not in kinds:
            continue
        if len(g.members) < 2:
            continue
        filtered.append(g)

    if not filtered:
        _write_action_log(result, log_dir)
        return result

    # Separate counters per kind for friendly numbering
    counters: dict[str, int] = {GroupKind.EXACT.value: 0, GroupKind.SIMILAR.value: 0}
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
                "unless mode=move."
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
                "review_root": str(root),
                "group_count": len(index_rows),
                "groups": index_rows,
            }
            (root / "_review_index.json").write_text(
                json.dumps(index, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

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
        f"Reclaimable: {format_bytes(result.reclaimable_bytes)}",
    ]
    if result.errors:
        lines.append(f"Errors: {len(result.errors)}")
    return "\n".join(lines)
