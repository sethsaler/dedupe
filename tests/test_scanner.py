"""Directory-inventory tests for scanner.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dedupe.models import FileRecord, MediaType
from dedupe.scanner import (
    BUILTIN_EXCLUSIONS,
    EXTRA_RAW_EXTS,
    RAW_IMAGE_EXTS,
    detect_live_photo_pairs,
    inventory,
    is_in_photos_library,
    media_extensions,
    preview_exclusions,
)


def _touch(path: Path, size: int = 4) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_media_extensions_respects_the_kind_flags() -> None:
    assert ".jpg" in media_extensions()
    assert media_extensions(False, False, False) == set()
    only_videos = media_extensions(include_images=False, include_gifs=False)
    assert ".mp4" in only_videos
    assert ".jpg" not in only_videos
    assert ".gif" not in only_videos


def test_inventory_collects_media_and_skips_the_rest(tmp_path: Path) -> None:
    _touch(tmp_path / "photo.jpg")
    _touch(tmp_path / "clip.mpg")
    _touch(tmp_path / "still.avif")
    _touch(tmp_path / "notes.txt")
    _touch(tmp_path / "no_extension")

    records = inventory([tmp_path])

    by_name = {Path(record.path).name: record for record in records}
    assert set(by_name) == {"photo.jpg", "clip.mpg", "still.avif"}
    assert by_name["photo.jpg"].media_type == MediaType.IMAGE
    assert by_name["clip.mpg"].media_type == MediaType.VIDEO
    assert by_name["still.avif"].media_type == MediaType.IMAGE
    assert by_name["photo.jpg"].size == 4


def test_inventory_skips_hidden_files_unless_asked(tmp_path: Path) -> None:
    _touch(tmp_path / ".hidden.jpg")
    _touch(tmp_path / ".hidden_dir" / "nested.jpg")
    _touch(tmp_path / "visible.jpg")

    assert [Path(r.path).name for r in inventory([tmp_path])] == ["visible.jpg"]
    assert {
        Path(r.path).name for r in inventory([tmp_path], include_hidden=True)
    } == {"visible.jpg", ".hidden.jpg", "nested.jpg"}


def test_inventory_always_applies_builtin_exclusions(tmp_path: Path) -> None:
    _touch(tmp_path / "_Dedupe Quarantine" / "staged.jpg")
    _touch(tmp_path / "node_modules" / "junk.jpg")
    _touch(tmp_path / "keep.jpg")

    names = [Path(r.path).name for r in inventory([tmp_path])]
    assert names == ["keep.jpg"]
    # The builtin list must keep covering our own output folders.
    assert "_dedupe quarantine" in BUILTIN_EXCLUSIONS
    assert "_dedupe review" in BUILTIN_EXCLUSIONS


def test_inventory_applies_user_exclusions_to_names_and_paths(tmp_path: Path) -> None:
    _touch(tmp_path / "screenshot-1.png")
    _touch(tmp_path / "screenshot-2.png")
    _touch(tmp_path / "portrait.png")

    records = inventory([tmp_path], exclusions=["screenshot-*"])
    assert [Path(r.path).name for r in records] == ["portrait.png"]


def test_inventory_followed_links_use_canonical_paths_and_lexical_exclusions(tmp_path):
    root = tmp_path / "media"
    target = root / "target"
    kept = _touch(target / "keep.jpg")
    _touch(target / "skip1.jpg")
    outside = _touch(tmp_path / "elsewhere" / "outside.jpg")
    (root / "alias").symlink_to(target, target_is_directory=True)
    (target / "outside-link.jpg").symlink_to(outside)
    (root / "external").symlink_to(outside.parent, target_is_directory=True)

    records = inventory([root], follow_symlinks=True, exclusions=["TARGET", "alias/SKIP[0-9].jpg"])

    assert [r.path for r in records] == [str(kept)]


def test_inventory_skips_missing_roots_and_accepts_a_file_root(tmp_path: Path) -> None:
    single = _touch(tmp_path / "single.jpg")

    records = inventory([tmp_path / "does-not-exist", single])

    assert [Path(r.path).name for r in records] == ["single.jpg"]


def test_inventory_never_enters_a_photos_library(tmp_path: Path) -> None:
    library = tmp_path / "Photos Library.photoslibrary"
    _touch(library / "originals" / "img.jpg")
    assert is_in_photos_library(library)
    assert inventory([library]) == []


def test_inventory_dedupes_hardlinked_files_by_inode(tmp_path: Path) -> None:
    original = _touch(tmp_path / "original.jpg")
    link = tmp_path / "hardlink.jpg"
    os.link(original, link)

    records = inventory([tmp_path])

    assert len(records) == 1


@pytest.mark.parametrize("file_first", [False, True])
def test_inventory_dedupes_file_roots_with_their_folders(tmp_path, file_first):
    original = _touch(tmp_path / "nested" / "original.jpg")
    second = _touch(tmp_path / "second.jpg")
    roots = [original, tmp_path, original.parent, original]
    if not file_first:
        roots = [tmp_path, original.parent, original, original]
    progress = []

    records = inventory(roots, progress=lambda *args: progress.append(args))

    assert len(records) == 2
    assert {record.path for record in records} == {str(original), str(second)}
    assert progress[-1] == ("inventory", 2, 2)


def test_inventory_dedupes_hardlinks_supplied_as_file_roots(tmp_path):
    original = _touch(tmp_path / "original.jpg")
    link = tmp_path / "other" / "hardlink.jpg"
    link.parent.mkdir()
    os.link(original, link)

    records = inventory([original, link, link.parent])

    assert len(records) == 1
    assert records[0].path == str(original)


def test_inventory_cancel_raises_interrupted(tmp_path: Path) -> None:
    _touch(tmp_path / "a.jpg")

    with pytest.raises(InterruptedError):
        inventory([tmp_path], cancelled=lambda: True)


def test_inventory_reports_progress(tmp_path: Path) -> None:
    for index in range(3):
        _touch(tmp_path / f"f{index}.jpg")
    calls: list[tuple[str, int, int]] = []

    inventory([tmp_path], progress=lambda *args: calls.append(args))

    assert calls[-1] == ("inventory", 3, 3)


def test_preview_exclusions_counts_matches_and_flags_dead_patterns(tmp_path: Path) -> None:
    _touch(tmp_path / "screenshot-1.png")
    _touch(tmp_path / "screenshot-2.png")
    _touch(tmp_path / "portrait.png")

    report = preview_exclusions([tmp_path], ["screenshot-*", "zzz-*"])

    patterns = {entry["pattern"]: entry for entry in report["patterns"]}
    assert patterns["screenshot-*"]["matches"] == 2
    assert patterns["zzz-*"]["matches"] == 0
    assert report["truncated"] is False


def test_inventory_honors_hidden_and_exclusions_on_explicit_file_roots(tmp_path: Path) -> None:
    hidden = _touch(tmp_path / ".secret.jpg")
    excluded = _touch(tmp_path / "skip_me.jpg")
    kept = _touch(tmp_path / "normal.jpg")

    hidden_off = inventory([hidden, excluded, kept], include_hidden=False, exclusions=["skip_*"])
    assert [r.path for r in hidden_off] == [str(kept.resolve())]

    hidden_on = inventory([hidden, kept], include_hidden=True)
    assert {r.path for r in hidden_on} == {str(hidden.resolve()), str(kept.resolve())}


def test_inventory_extra_raw_extensions_classified_as_images(tmp_path: Path) -> None:
    assert EXTRA_RAW_EXTS <= RAW_IMAGE_EXTS
    assert EXTRA_RAW_EXTS <= media_extensions()
    raw_files = [_touch(tmp_path / f"shot{ext}") for ext in sorted(EXTRA_RAW_EXTS)]
    records = inventory([tmp_path])
    assert {r.extension for r in records} == set(EXTRA_RAW_EXTS)
    assert all(r.media_type == MediaType.IMAGE for r in records)
    assert len(records) == len(raw_files)


def test_detect_live_photo_pairs_from_duration_and_quicktime_identifier(tmp_path: Path) -> None:
    heic = tmp_path / "IMG_1001.heic"
    mov = tmp_path / "IMG_1001.mov"
    long_still = tmp_path / "IMG_2002.jpg"
    long_mov = tmp_path / "IMG_2002.mov"
    qt_still = tmp_path / "IMG_3003.jpg"
    qt_mov = tmp_path / "IMG_3003.mov"
    for p in (heic, mov, long_still, long_mov, qt_still, qt_mov):
        p.write_bytes(b"dummy")

    # Embed QuickTime moov/meta keys+ilst atoms carrying com.apple.quicktime.content.identifier
    key_name = b"com.apple.quicktime.content.identifier"
    key_entry = (8 + len(key_name)).to_bytes(4, "big") + b"mdta" + key_name
    keys_atom = (
        (16 + len(key_entry)).to_bytes(4, "big")
        + b"keys"
        + (0).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + key_entry
    )
    val_bytes = b"LIVE-ASSET-UUID-1"
    data_atom = (16 + len(val_bytes)).to_bytes(4, "big") + b"data" + (1).to_bytes(8, "big") + val_bytes
    item_atom = (8 + len(data_atom)).to_bytes(4, "big") + (1).to_bytes(4, "big") + data_atom
    ilst_atom = (8 + len(item_atom)).to_bytes(4, "big") + b"ilst" + item_atom
    moov_payload = keys_atom + ilst_atom
    moov_box = (8 + len(moov_payload)).to_bytes(4, "big") + b"moov" + moov_payload
    qt_mov.write_bytes(moov_box)

    records = [
        FileRecord(path=str(heic), size=5, mtime=1.0, media_type=MediaType.IMAGE, extension=".heic"),
        FileRecord(
            path=str(mov),
            size=5,
            mtime=1.0,
            media_type=MediaType.VIDEO,
            extension=".mov",
            duration=2.5,
        ),
        FileRecord(
            path=str(long_still),
            size=5,
            mtime=1.0,
            media_type=MediaType.IMAGE,
            extension=".jpg",
        ),
        FileRecord(
            path=str(long_mov),
            size=5,
            mtime=1.0,
            media_type=MediaType.VIDEO,
            extension=".mov",
            duration=12.0,
        ),
        FileRecord(
            path=str(qt_still),
            size=5,
            mtime=1.0,
            media_type=MediaType.IMAGE,
            extension=".jpg",
        ),
        FileRecord(
            path=str(qt_mov),
            size=len(moov_box),
            mtime=1.0,
            media_type=MediaType.VIDEO,
            extension=".mov",
            duration=None,
        ),
    ]
    pairs = detect_live_photo_pairs(records)
    paired_names = {(Path(s.path).name, Path(m.path).name) for s, m, _cid in pairs}
    assert ("IMG_1001.heic", "IMG_1001.mov") in paired_names
    assert ("IMG_3003.jpg", "IMG_3003.mov") in paired_names
    assert ("IMG_2002.jpg", "IMG_2002.mov") not in paired_names
