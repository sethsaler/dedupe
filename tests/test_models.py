"""Media classification and extension-table tests."""

from __future__ import annotations

from pathlib import Path

from dedupe.models import (
    GIF_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    MediaType,
    classify_media,
)


def test_common_image_extensions_classify_as_image() -> None:
    for ext in sorted(IMAGE_EXTS):
        assert classify_media(Path(f"/media/photo{ext}")) == MediaType.IMAGE, ext


def test_common_video_extensions_classify_as_video() -> None:
    for ext in sorted(VIDEO_EXTS):
        assert classify_media(Path(f"/media/clip{ext}")) == MediaType.VIDEO, ext


def test_gif_classifies_as_gif() -> None:
    assert classify_media(Path("/media/anim.gif")) == MediaType.GIF
    assert GIF_EXTS == {".gif"}


def test_extension_tables_are_disjoint() -> None:
    assert not (IMAGE_EXTS & VIDEO_EXTS)
    assert not (IMAGE_EXTS & GIF_EXTS)
    assert not (VIDEO_EXTS & GIF_EXTS)


def test_recently_added_formats_are_inventoried() -> None:
    # Regression: `.avif` was served as browser-safe in the web UI while the
    # scanner never picked it up; mpeg-1/2 containers were missing entirely.
    for ext in (".avif",):
        assert ext in IMAGE_EXTS
    for ext in (".mpg", ".mpeg", ".ts", ".vob"):
        assert ext in VIDEO_EXTS


def test_classification_is_case_insensitive() -> None:
    assert classify_media(Path("/media/PHOTO.JPG")) == MediaType.IMAGE
    assert classify_media(Path("/media/CLIP.MP4")) == MediaType.VIDEO
    assert classify_media(Path("/media/ANIM.GIF")) == MediaType.GIF


def test_unknown_and_extensionless_paths_are_other() -> None:
    assert classify_media(Path("/media/notes.txt")) == MediaType.OTHER
    assert classify_media(Path("/media/no_extension")) == MediaType.OTHER


def test_web_video_table_mirrors_the_scanner() -> None:
    # web.media generates ffmpeg thumbnails for exactly the scanner's videos.
    from dedupe.web.media import VIDEO_EXTENSIONS

    assert VIDEO_EXTENSIONS == VIDEO_EXTS
