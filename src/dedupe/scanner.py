"""Directory inventory for media files."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from fnmatch import fnmatch
from pathlib import Path

from .models import (
    GIF_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    FileRecord,
    MediaType,
    classify_media,
)

ProgressCb = Callable[[str, int, int], None]
CancelCb = Callable[[], bool]

#: Common junk / system folders and our own review output, always skipped.
BUILTIN_EXCLUSIONS = {
    ".git",
    ".dedupe",
    "*.photoslibrary",
    "node_modules",
    ".trash",
    ".ds_store",
    "__macosx",
    "for deletion",
    "_dedupe review",
    "dedupe review",
    "_dedupe quarantine",
    "dedupe quarantine",
}


def is_in_photos_library(path: str | Path) -> bool:
    """Return whether a path is a Photos-managed package or one of its descendants."""
    resolved = Path(path).expanduser().resolve(strict=False)
    return any(part.lower().endswith(".photoslibrary") for part in resolved.parts)


def media_extensions(
    include_images: bool = True,
    include_gifs: bool = True,
    include_videos: bool = True,
) -> set[str]:
    exts: set[str] = set()
    if include_images:
        exts |= IMAGE_EXTS
    if include_gifs:
        exts |= GIF_EXTS
    if include_videos:
        exts |= VIDEO_EXTS
    return exts


def inventory(
    roots: Iterable[str | Path],
    *,
    include_images: bool = True,
    include_gifs: bool = True,
    include_videos: bool = True,
    include_hidden: bool = False,
    follow_symlinks: bool = False,
    exclusions: Iterable[str] | None = None,
    progress: ProgressCb | None = None,
    cancelled: CancelCb | None = None,
) -> list[FileRecord]:
    """Walk roots and return FileRecords for matching media."""
    exts = media_extensions(include_images, include_gifs, include_videos)
    if not exts:
        return []

    exclusion_patterns = {e.strip().lower() for e in (exclusions or []) if e.strip()}
    # Always skip common junk / system folders + our own review output
    exclusion_patterns |= BUILTIN_EXCLUSIONS

    records: list[FileRecord] = []
    seen_inodes: set[tuple[int, int]] = set()
    found = 0

    def excluded(name: str, relative: Path) -> bool:
        name_lower = name.lower()
        rel_lower = relative.as_posix().lower()
        return any(
            fnmatch(name_lower, pattern) or fnmatch(rel_lower, pattern)
            for pattern in exclusion_patterns
        )

    for root in roots:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        root_path = Path(root).expanduser().resolve()
        if not root_path.exists():
            continue
        if is_in_photos_library(root_path):
            continue
        if root_path.is_file():
            rec = _record_for_file(root_path, exts)
            if rec:
                records.append(rec)
                found += 1
                if progress:
                    progress("inventory", found, found)
            continue

        seen_dir_inodes: set[tuple[int, int]] = set()
        try:
            root_stat = root_path.stat()
            seen_dir_inodes.add((root_stat.st_dev, root_stat.st_ino))
        except OSError:
            continue

        for dirpath, dirnames, filenames in os.walk(
            root_path, followlinks=follow_symlinks
        ):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            # Prune excluded / hidden dirs in-place
            pruned: list[str] = []
            for d in dirnames:
                if not include_hidden and d.startswith("."):
                    continue
                candidate = Path(dirpath) / d
                try:
                    relative = candidate.relative_to(root_path)
                except ValueError:
                    continue
                if excluded(d, relative):
                    continue
                try:
                    if candidate.is_symlink():
                        if not follow_symlinks:
                            continue
                        resolved = candidate.resolve(strict=True)
                        if resolved != root_path and root_path not in resolved.parents:
                            continue
                        st = resolved.stat()
                    else:
                        st = candidate.stat()
                    inode_key = (st.st_dev, st.st_ino)
                    if inode_key in seen_dir_inodes:
                        continue
                    seen_dir_inodes.add(inode_key)
                except OSError:
                    continue
                pruned.append(d)
            dirnames[:] = pruned

            for name in filenames:
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                if not include_hidden and name.startswith("."):
                    continue
                path = Path(dirpath) / name
                try:
                    relative = path.relative_to(root_path)
                except ValueError:
                    continue
                if excluded(name, relative):
                    continue
                if path.suffix.lower() not in exts:
                    continue
                try:
                    if path.is_symlink():
                        if not follow_symlinks:
                            continue
                        resolved = path.resolve(strict=True)
                        if resolved != root_path and root_path not in resolved.parents:
                            continue
                        path = resolved
                    st = path.stat()
                    inode_key = (st.st_dev, st.st_ino)
                    if inode_key in seen_inodes:
                        continue
                    seen_inodes.add(inode_key)
                except OSError:
                    continue

                rec = _record_for_file(path, exts, stat_result=st)
                if rec:
                    records.append(rec)
                    found += 1
                    if progress and found % 50 == 0:
                        progress("inventory", found, found)

    if progress:
        progress("inventory", found, found)
    return records


def preview_exclusions(
    roots: Iterable[str | Path],
    exclusions: Iterable[str],
    *,
    max_visits: int = 100_000,
    max_matches: int = 1_000,
) -> dict:
    """Count what each user exclusion pattern would match under ``roots``.

    Walks the tree with the same rules as a real scan (hidden files skipped,
    excluded dirs pruned, built-in patterns always applied) so a pattern that
    matches nothing — usually a typo — is visible before the scan starts.
    Bounded by ``max_visits`` entries; ``truncated`` reports when the cap hit.
    """
    user_patterns = [e.strip().lower() for e in exclusions if e.strip()]
    all_patterns = set(user_patterns) | BUILTIN_EXCLUSIONS
    counts = {pattern: 0 for pattern in user_patterns}
    examples: dict[str, list[str]] = {pattern: [] for pattern in user_patterns}
    visited = 0
    truncated = False

    def note(name: str, relative: Path, root_path: Path) -> None:
        name_lower = name.lower()
        rel_lower = relative.as_posix().lower()
        for pattern in user_patterns:
            if fnmatch(name_lower, pattern) or fnmatch(rel_lower, pattern):
                if counts[pattern] < max_matches:
                    counts[pattern] += 1
                if len(examples[pattern]) < 3:
                    examples[pattern].append(str(root_path / relative))

    def excluded(name: str, relative: Path) -> bool:
        name_lower = name.lower()
        rel_lower = relative.as_posix().lower()
        return any(
            fnmatch(name_lower, pattern) or fnmatch(rel_lower, pattern)
            for pattern in all_patterns
        )

    for root in roots:
        if truncated:
            break
        root_path = Path(root).expanduser().resolve()
        if not root_path.exists() or is_in_photos_library(root_path):
            continue
        if root_path.is_file():
            note(root_path.name, Path(root_path.name), root_path.parent)
            continue
        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            pruned: list[str] = []
            for d in dirnames:
                if d.startswith("."):
                    continue
                candidate = Path(dirpath) / d
                try:
                    relative = candidate.relative_to(root_path)
                except ValueError:
                    continue
                note(d, relative, root_path)
                visited += 1
                if excluded(d, relative) or candidate.is_symlink():
                    continue
                pruned.append(d)
            dirnames[:] = pruned
            for name in filenames:
                if name.startswith("."):
                    continue
                path = Path(dirpath) / name
                try:
                    relative = path.relative_to(root_path)
                except ValueError:
                    continue
                note(name, relative, root_path)
                visited += 1
            if visited >= max_visits:
                truncated = True
                break

    return {
        "patterns": [
            {"pattern": pattern, "matches": counts[pattern], "examples": examples[pattern]}
            for pattern in user_patterns
        ],
        "visited": visited,
        "truncated": truncated,
    }


def _record_for_file(
    path: Path,
    exts: set[str],
    stat_result: os.stat_result | None = None,
) -> FileRecord | None:
    try:
        if path.suffix.lower() not in exts:
            return None
        st = stat_result or path.stat()
        if not path.is_file():
            return None
        media = classify_media(path)
        if media == MediaType.OTHER:
            return None
        return FileRecord(
            path=str(path.resolve()),
            size=int(st.st_size),
            mtime=float(st.st_mtime),
            media_type=media,
            extension=path.suffix.lower(),
            device=int(st.st_dev),
            inode=int(st.st_ino),
            mtime_ns=int(st.st_mtime_ns),
        )
    except OSError:
        return None
