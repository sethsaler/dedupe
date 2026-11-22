"""Directory inventory for media files."""

from __future__ import annotations

import os
import re
import stat
import struct
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from fnmatch import fnmatch, translate
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

# Additional RAW formats beyond models.IMAGE_EXTS.
EXTRA_RAW_EXTS = frozenset({".cr3", ".orf", ".rw2", ".raf", ".pef", ".srw"})
RAW_IMAGE_EXTS = frozenset({".raw", ".cr2", ".nef", ".arw", ".dng"}) | EXTRA_RAW_EXTS
LIVE_PHOTO_MAX_DURATION = 5.0

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
        exts |= IMAGE_EXTS | EXTRA_RAW_EXTS
    if include_gifs:
        exts |= GIF_EXTS
    if include_videos:
        exts |= VIDEO_EXTS
    return exts


def _exclusion_matcher(patterns: set[str]) -> Callable[[str, Path], bool]:
    # Match all globs with one regex, using the same platform normalization
    # as fnmatch for both the basename and the root-relative path.
    match = re.compile("|".join(translate(os.path.normcase(p)) for p in patterns)).match

    def excluded(name: str, relative: Path) -> bool:
        return bool(
            match(os.path.normcase(name.lower()))
            or match(os.path.normcase(relative.as_posix().lower()))
        )

    return excluded


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
    """Walk roots with ``os.scandir`` and return FileRecords for matching media."""
    exts = media_extensions(include_images, include_gifs, include_videos)
    if not exts:
        return []

    exclusion_patterns = {e.strip().lower() for e in (exclusions or []) if e.strip()}
    # Always skip common junk / system folders + our own review output
    exclusion_patterns |= BUILTIN_EXCLUSIONS
    excluded = _exclusion_matcher(exclusion_patterns)

    records: list[FileRecord] = []
    seen_inodes: set[tuple[int, int]] = set()
    # Share visited directory inodes across all roots so overlapping or
    # protected subtrees are never walked twice in one inventory pass.
    seen_dir_inodes: set[tuple[int, int]] = set()
    found = 0

    for root in roots:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        # Resolve each root once; directory entries never resolve symlinks
        # unless follow_symlinks=True.
        root_path = Path(root).expanduser().resolve(strict=False)
        try:
            root_stat = root_path.stat()
        except OSError:
            continue
        if is_in_photos_library(root_path):
            continue
        if stat.S_ISREG(root_stat.st_mode):
            if not include_hidden and root_path.name.startswith("."):
                continue
            if excluded(root_path.name, Path(root_path.name)):
                continue
            rec = _record_for_file(root_path, exts, stat_result=root_stat)
            if rec and (rec.device, rec.inode) not in seen_inodes:
                # Explicit file roots share the same inventory as directories.
                # Otherwise a repeated file, a hardlink, or a file plus its
                # parent folder can appear twice and be compared with itself.
                seen_inodes.add((rec.device, rec.inode))
                records.append(rec)
                found += 1
                if progress:
                    progress("inventory", found, found)
            continue
        if not stat.S_ISDIR(root_stat.st_mode):
            continue

        root_dir_inode = (int(root_stat.st_dev), int(root_stat.st_ino))
        if root_dir_inode in seen_dir_inodes:
            continue
        seen_dir_inodes.add(root_dir_inode)

        queue: deque[tuple[Path, Path]] = deque([(root_path, Path())])
        while queue:
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            directory, relative_dir = queue.popleft()
            try:
                scan_iter = os.scandir(directory)
            except OSError:
                continue
            with scan_iter as entries:
                for entry in entries:
                    if cancelled and cancelled():
                        raise InterruptedError("scan cancelled")
                    name = entry.name
                    if not include_hidden and name.startswith("."):
                        continue
                    relative_entry = relative_dir / name
                    try:
                        is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                    except OSError:
                        continue
                    if is_dir:
                        if excluded(name, relative_entry):
                            continue
                        try:
                            if follow_symlinks and entry.is_symlink():
                                resolved = Path(entry.path).resolve(strict=True)
                                if resolved != root_path and root_path not in resolved.parents:
                                    continue
                                st = resolved.stat()
                                next_dir = resolved
                            else:
                                st = entry.stat(follow_symlinks=False)
                                next_dir = Path(entry.path)
                        except (OSError, RuntimeError):
                            continue
                        dir_inode = (int(st.st_dev), int(st.st_ino))
                        if dir_inode in seen_dir_inodes:
                            continue
                        seen_dir_inodes.add(dir_inode)
                        queue.append((next_dir, relative_entry))
                        continue

                    ext = os.path.splitext(name)[1].lower()
                    if ext not in exts or excluded(name, relative_entry):
                        continue
                    try:
                        if follow_symlinks and entry.is_symlink():
                            resolved = Path(entry.path).resolve(strict=True)
                            if resolved != root_path and root_path not in resolved.parents:
                                continue
                            st = resolved.stat()
                            if not stat.S_ISREG(st.st_mode):
                                continue
                            file_path = resolved
                        else:
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            st = entry.stat(follow_symlinks=False)
                            if not stat.S_ISREG(st.st_mode):
                                continue
                            file_path = Path(entry.path)
                    except (OSError, RuntimeError):
                        continue

                    inode_key = (int(st.st_dev), int(st.st_ino))
                    if inode_key in seen_inodes:
                        continue
                    seen_inodes.add(inode_key)

                    rec = _record_for_file(file_path, exts, stat_result=st)
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
    excluded = _exclusion_matcher(set(user_patterns) | BUILTIN_EXCLUSIONS)
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

    for root in roots:
        if truncated:
            break
        root_path = Path(root).expanduser().resolve()
        if not root_path.exists() or is_in_photos_library(root_path):
            continue
        if root_path.is_file():
            note(root_path.name, Path(root_path.name), root_path.parent)
            continue
        queue: deque[tuple[Path, Path]] = deque([(root_path, Path())])
        while queue and not truncated:
            directory, relative_dir = queue.popleft()
            try:
                scan_iter = os.scandir(directory)
            except OSError:
                continue
            with scan_iter as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith("."):
                        continue
                    relative = relative_dir / name
                    note(name, relative, root_path)
                    visited += 1
                    try:
                        if (
                            entry.is_dir(follow_symlinks=False)
                            and not excluded(name, relative)
                        ):
                            queue.append((Path(entry.path), relative))
                    except OSError:
                        pass
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


def _read_quicktime_live_info(path: Path) -> tuple[float | None, str | None]:
    """Read QuickTime ``mvhd`` duration and ``com.apple.quicktime.content.identifier``."""
    try:
        file_size = path.stat().st_size
    except OSError:
        return None, None
    if file_size < 16:
        return None, None
    moov_bytes: bytes | None = None
    try:
        with open(path, "rb") as f:
            pos = 0
            while pos + 8 <= file_size:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                atom_size, atom_type = struct.unpack(">I4s", hdr)
                header_len = 8
                if atom_size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    atom_size = struct.unpack(">Q", ext)[0]
                    header_len = 16
                elif atom_size == 0:
                    atom_size = file_size - pos
                if atom_size < header_len or pos + atom_size > file_size:
                    break
                if atom_type == b"moov":
                    payload_size = atom_size - header_len
                    # Guard against malformed huge moov boxes.
                    if payload_size <= 8 * 1024 * 1024:
                        moov_bytes = f.read(payload_size)
                    break
                pos += atom_size
    except OSError:
        return None, None
    if not moov_bytes:
        return None, None

    duration: float | None = None
    mvhd_idx = moov_bytes.find(b"mvhd")
    if mvhd_idx >= 4 and mvhd_idx + 28 <= len(moov_bytes):
        version = moov_bytes[mvhd_idx + 4]
        if version == 0 and mvhd_idx + 24 <= len(moov_bytes):
            timescale, dur_units = struct.unpack(
                ">II", moov_bytes[mvhd_idx + 16 : mvhd_idx + 24]
            )
            if timescale > 0 and dur_units > 0:
                duration = dur_units / timescale
        elif version == 1 and mvhd_idx + 36 <= len(moov_bytes):
            timescale, dur_units = struct.unpack(
                ">IQ", moov_bytes[mvhd_idx + 24 : mvhd_idx + 36]
            )
            if timescale > 0 and dur_units > 0:
                duration = dur_units / timescale

    content_id = _extract_quicktime_mdta_tag(
        moov_bytes, b"com.apple.quicktime.content.identifier"
    )
    return duration, content_id


def _extract_quicktime_mdta_tag(moov_bytes: bytes, target_key: bytes) -> str | None:
    """Parse QuickTime ``meta -> keys/ilst`` for a UTF-8 ``mdta`` tag value."""
    target_idx: int | None = None
    keys_pos = moov_bytes.find(b"keys")
    if keys_pos >= 4 and keys_pos + 12 <= len(moov_bytes):
        keys_start = keys_pos - 4
        keys_size = struct.unpack(">I", moov_bytes[keys_start : keys_start + 4])[0]
        keys_end = min(len(moov_bytes), keys_start + keys_size)
        if keys_size >= 16:
            entry_count = struct.unpack(">I", moov_bytes[keys_start + 12 : keys_start + 16])[0]
            cursor = keys_start + 16
            for idx in range(1, min(entry_count, 512) + 1):
                if cursor + 8 > keys_end:
                    break
                entry_size, ns = struct.unpack(">I4s", moov_bytes[cursor : cursor + 8])
                if entry_size < 8 or cursor + entry_size > keys_end:
                    break
                key_bytes = moov_bytes[cursor + 8 : cursor + entry_size]
                if ns == b"mdta" and key_bytes == target_key:
                    target_idx = idx
                    break
                cursor += entry_size

    if target_idx is not None:
        ilst_pos = moov_bytes.find(b"ilst")
        if ilst_pos >= 4 and ilst_pos + 8 <= len(moov_bytes):
            ilst_start = ilst_pos - 4
            ilst_size = struct.unpack(">I", moov_bytes[ilst_start : ilst_start + 4])[0]
            ilst_end = min(len(moov_bytes), ilst_start + ilst_size)
            cursor = ilst_start + 8
            while cursor + 8 <= ilst_end:
                item_size, item_idx = struct.unpack(">II", moov_bytes[cursor : cursor + 8])
                if item_size < 8 or cursor + item_size > ilst_end:
                    break
                if item_idx == target_idx:
                    sub = cursor + 8
                    sub_end = cursor + item_size
                    while sub + 8 <= sub_end:
                        d_size, d_type = struct.unpack(">I4s", moov_bytes[sub : sub + 8])
                        if d_size < 8 or sub + d_size > sub_end:
                            break
                        if d_type == b"data" and d_size > 16:
                            val = moov_bytes[sub + 16 : sub + d_size].decode(
                                "utf-8", errors="ignore"
                            ).strip("\x00 \t\r\n")
                            if val:
                                return val
                        sub += d_size
                cursor += item_size

    return None


def detect_live_photo_pairs(
    records: Sequence[FileRecord],
    *,
    check_quicktime: bool = True,
) -> list[tuple[FileRecord, FileRecord, str | None]]:
    """Identify Live Photo companion pairs ``(still, mov, content_identifier)``.

    Matches a still image and a short ``.mov`` (``<= 5s``) in the same folder
    with matching case-insensitive stem, inspecting QuickTime
    ``com.apple.quicktime.content.identifier`` when available.
    """
    stills: dict[tuple[Path, str], list[FileRecord]] = {}
    movs: dict[tuple[Path, str], list[FileRecord]] = {}
    for rec in records:
        p = Path(rec.path)
        key = (p.parent, p.stem.lower())
        if rec.media_type == MediaType.IMAGE:
            stills.setdefault(key, []).append(rec)
        elif rec.media_type == MediaType.VIDEO and (
            rec.extension.lower() == ".mov" or p.suffix.lower() == ".mov"
        ):
            movs.setdefault(key, []).append(rec)

    pairs: list[tuple[FileRecord, FileRecord, str | None]] = []
    for key, mov_list in movs.items():
        still_list = stills.get(key)
        if not still_list:
            continue
        still = still_list[0]
        for mov in mov_list:
            duration = mov.duration
            content_id: str | None = None
            if check_quicktime:
                qt_duration, content_id = _read_quicktime_live_info(Path(mov.path))
                if duration is None and qt_duration is not None:
                    duration = qt_duration
                    mov.duration = qt_duration
            if duration is not None and duration > LIVE_PHOTO_MAX_DURATION:
                continue
            if content_id or (duration is not None and 0 < duration <= LIVE_PHOTO_MAX_DURATION):
                pairs.append((still, mov, content_id))
    return pairs


def _record_for_file(
    path: Path,
    exts: set[str],
    stat_result: os.stat_result | None = None,
) -> FileRecord | None:
    try:
        ext = path.suffix.lower()
        if ext not in exts:
            return None
        st = stat_result or path.stat()
        if not stat.S_ISREG(st.st_mode):
            return None
        media = MediaType.IMAGE if ext in EXTRA_RAW_EXTS else classify_media(path)
        if media == MediaType.OTHER:
            return None
        return FileRecord(
            path=str(path),
            size=int(st.st_size),
            mtime=float(st.st_mtime),
            media_type=media,
            extension=ext,
            device=int(st.st_dev),
            inode=int(st.st_ino),
            mtime_ns=int(st.st_mtime_ns),
            ctime_ns=int(st.st_ctime_ns),
        )
    except OSError:
        return None
