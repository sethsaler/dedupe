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


def test_file_record_serialization_preserves_fields_and_round_trips() -> None:
    from dataclasses import asdict

    from dedupe.models import FileRecord

    record = FileRecord(
        path="/photos/image.jpg", size=1234, mtime=123.5,
        media_type=MediaType.IMAGE, extension=".jpg", width=1920,
        mtime_ns=123500000000, face_count=0, phash="abcdef", error="unreadable",
        non_person_coverage=95.25, non_person_coverage_signature="coverage-v1",
    )
    expected = asdict(record)
    expected["media_type"] = "image"
    payload = record.to_dict()
    assert payload == expected
    assert FileRecord.from_dict(payload) == record
    payload["path"] = "/another.jpg"
    assert record.path == "/photos/image.jpg"

    del payload["non_person_coverage"]
    del payload["non_person_coverage_signature"]
    del payload["sharpness"]
    del payload["video_codec"]
    del payload["video_bitrate"]
    del payload["has_audio"]
    legacy = FileRecord.from_dict(payload)
    assert legacy.non_person_coverage is None
    assert legacy.non_person_coverage_signature is None
    assert legacy.sharpness is None
    assert legacy.video_codec is None
    assert legacy.video_bitrate is None
    assert legacy.has_audio is None


def test_file_record_new_fields_and_effective_pixels() -> None:
    from dedupe.models import FileRecord

    rec = FileRecord(
        path="/photos/shot.mp4",
        size=1000,
        mtime=1.0,
        media_type=MediaType.VIDEO,
        extension=".mp4",
        width=1920,
        height=1080,
        sharpness=42.5,
        video_codec="h264",
        video_bitrate=5_000_000,
        has_audio=True,
        capture_metadata=True,
    )
    restored = FileRecord.from_dict(rec.to_dict(compact=True))
    assert restored == rec
    assert rec.effective_pixels == 1920 * 1080

    # 7-part border_crop with chrome flag and exact dimensions
    rec.border_crop = "0,100,1920,980,1920,1080,top+bottom"
    assert rec.effective_pixels == 1920 * 880

    # Scaled border_crop measured at half-resolution
    rec.border_crop = "0,50,960,490,960,540"
    assert rec.effective_pixels == 1920 * 880

    # Dismissed or invalid crops fall back to raw pixels
    rec.border_crop = "dismissed"
    assert rec.effective_pixels == 1920 * 1080
    rec.border_crop = "invalid"
    assert rec.effective_pixels == 1920 * 1080


def test_review_group_new_fields_round_trip_and_legacy_defaults() -> None:
    from dedupe.models import FileRecord, GroupKind, ReviewGroup

    a = FileRecord("/a.jpg", 100, 1.0, MediaType.IMAGE, ".jpg")
    b = FileRecord("/b.jpg", 100, 2.0, MediaType.IMAGE, ".jpg")
    group = ReviewGroup(
        id="grp1",
        kind=GroupKind.SIMILAR,
        media_type=MediaType.IMAGE,
        members=[a, b],
        suggested_keep=a.path,
        anchor_path=a.path,
        reviewed=True,
        match_explanations={b.path: "98% perceptual match"},
    )
    payload = group.to_dict()
    loaded = ReviewGroup.from_dict(payload)
    assert loaded.anchor_path == a.path
    assert loaded.reviewed is True
    assert loaded.match_explanations == {b.path: "98% perceptual match"}

    del payload["anchor_path"]
    del payload["reviewed"]
    del payload["match_explanations"]
    legacy = ReviewGroup.from_dict(payload)
    assert legacy.anchor_path is None
    assert legacy.reviewed is False
    assert legacy.match_explanations == {}
