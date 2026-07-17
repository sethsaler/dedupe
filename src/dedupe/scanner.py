"""Directory inventory for media files."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
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
) -> list[FileRecord]:
    """Walk roots and return FileRecords for matching media."""
    exts = media_extensions(include_images, include_gifs, include_videos)
    if not exts:
        return []

    exclusion_names = {e.strip().lower() for e in (exclusions or []) if e.strip()}
    # Always skip common junk / system folders + our own review output
    exclusion_names |= {
        ".git",
        ".dedupe",
        "node_modules",
        ".trash",
        ".ds_store",
        "__macosx",
        "for deletion",
        "_dedupe review",
        "dedupe review",
    }

    records: list[FileRecord] = []
    seen_inodes: set[tuple[int, int]] = set()
    found = 0

    for root in roots:
        root_path = Path(root).expanduser().resolve()
        if not root_path.exists():
            continue
        if root_path.is_file():
            rec = _record_for_file(root_path, exts)
            if rec:
                records.append(rec)
                found += 1
                if progress:
                    progress("inventory", found, found)
            continue

        for dirpath, dirnames, filenames in os.walk(
            root_path, followlinks=follow_symlinks
        ):
            # Prune excluded / hidden dirs in-place
            pruned: list[str] = []
            for d in dirnames:
                if not include_hidden and d.startswith("."):
                    continue
                if d.lower() in exclusion_names:
                    continue
                pruned.append(d)
            dirnames[:] = pruned

            for name in filenames:
                if not include_hidden and name.startswith("."):
                    continue
                path = Path(dirpath) / name
                if path.suffix.lower() not in exts:
                    continue
                try:
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
        )
    except OSError:
        return None
