"""Directory-inventory tests for scanner.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dedupe.models import MediaType
from dedupe.scanner import (
    BUILTIN_EXCLUSIONS,
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
