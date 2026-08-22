"""Face counting tests: caching, frame aggregation, and failure handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from dedupe.face_detection import (
    FACE_COUNT_CACHE_VERSION,
    analyze_face_count,
    count_faces_in_files,
    face_detection_signature,
)
from dedupe.models import FileRecord, MediaType
from dedupe.scanner import inventory


class _StubCounter:
    """Deterministic counter returning queued (total, male, female) per frame."""

    backend = "stub_counter"

    def __init__(self, frames: list[tuple[int, int, int]]) -> None:
        self.frames = list(frames)
        self.calls = 0

    def analyze_frame(self, _rgb_frame) -> tuple[int, int, int]:
        self.calls += 1
        return self.frames.pop(0) if self.frames else (0, 0, 0)


def _image_record(path: Path) -> FileRecord:
    Image.new("RGB", (64, 64), (20, 90, 40)).save(path)
    return inventory([path])[0]


def test_signature_pins_version_and_model() -> None:
    signature = face_detection_signature()
    assert signature.startswith(FACE_COUNT_CACHE_VERSION + "|")
    assert "yunet=" in signature
    assert "genderage=" in signature
    assert "face-confidence=" in signature


def test_blank_image_counts_zero_faces(tmp_path: Path) -> None:
    pytest.importorskip("cv2")
    record = _image_record(tmp_path / "blank.jpg")

    analyzed = count_faces_in_files([record], workers=1)

    assert analyzed == [record]
    assert record.face_count == 0
    assert record.male_face_count == 0
    assert record.female_face_count == 0
    assert record.face_detector == "opencv_yunet"
    assert record.face_detection_signature == face_detection_signature()


def test_analyze_face_count_keeps_max_across_gif_frames(tmp_path: Path) -> None:
    path = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (48, 48), (value, 0, 0)) for value in (10, 120, 240)]
    frames[0].save(path, save_all=True, append_images=frames[1:])
    record = inventory([path])[0]
    assert record.media_type == MediaType.GIF
    counter = _StubCounter([(1, 0, 1), (3, 2, 1), (2, 1, 1)])

    count = analyze_face_count(record, counter, cache_signature="sig")

    assert count == 3
    assert record.face_count == 3
    assert record.male_face_count == 2
    assert record.female_face_count == 1
    assert record.face_detection_signature == "sig"


def test_cached_count_skips_reanalysis(tmp_path: Path, monkeypatch) -> None:
    record = _image_record(tmp_path / "cached.jpg")
    record.face_count = 2
    record.face_detection_signature = face_detection_signature()

    def explode() -> None:
        raise AssertionError("cached counts must not build a detector")

    monkeypatch.setattr("dedupe.face_detection._YuNetFaceCounter", explode)

    analyzed = count_faces_in_files([record], workers=1)

    assert analyzed == [record]
    assert record.face_count == 2


def test_stale_signature_forces_reanalysis(tmp_path: Path, monkeypatch) -> None:
    record = _image_record(tmp_path / "stale.jpg")
    record.face_count = 5
    record.face_detection_signature = "face-count-v0|old"
    monkeypatch.setattr(
        "dedupe.face_detection._YuNetFaceCounter", lambda: _StubCounter([(1, 0, 1)])
    )

    count_faces_in_files([record], workers=1)

    assert record.face_count == 1
    assert record.face_detection_signature == face_detection_signature()


def test_decode_failure_clears_count_and_records_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    record = inventory([broken])[0]
    record.face_count = 4
    record.face_detection_signature = "sig"
    counter = _StubCounter([])

    count = analyze_face_count(record, counter, cache_signature="sig")

    assert count is None
    assert record.face_count is None
    assert record.male_face_count is None
    assert record.female_face_count is None
    assert record.face_detection_signature is None
    assert "face counting failed" in (record.error or "")


def _video_record(path: Path) -> FileRecord:
    path.write_bytes(b"fake video bytes")
    record = inventory([path])[0]
    assert record.media_type == MediaType.VIDEO
    return record


def _ppm_bytes() -> bytes:
    return b"P6\n1 1\n255\n\x00\x00\x00"


def _stub_video_frames(monkeypatch, timestamps: list[float]) -> None:
    monkeypatch.setattr("dedupe.face_detection.ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        "dedupe.face_detection._sample_timestamps",
        lambda duration, max_frames: list(timestamps),
    )
    monkeypatch.setattr(
        "dedupe.face_detection._extract_seek_frame_ppm",
        lambda path, timestamp, frame_width: _ppm_bytes(),
    )


def test_video_face_count_is_max_across_sampled_frames(
    tmp_path: Path, monkeypatch
) -> None:
    record = _video_record(tmp_path / "clip.mp4")
    record.duration = 6.0
    _stub_video_frames(monkeypatch, [0.0, 3.0, 5.9])
    counter = _StubCounter([(0, 0, 0), (2, 1, 1), (1, 0, 1)])

    count = analyze_face_count(record, counter, cache_signature="sig")

    assert count == 2
    assert record.face_count == 2
    assert record.male_face_count == 1
    assert record.female_face_count == 1
    assert record.face_detection_signature == "sig"
    assert counter.calls == 3


def test_video_frame_decode_failure_clears_count(tmp_path: Path, monkeypatch) -> None:
    record = _video_record(tmp_path / "clip.mp4")
    record.duration = 4.0
    record.face_count = 3
    record.face_detection_signature = "sig"
    monkeypatch.setattr("dedupe.face_detection.ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        "dedupe.face_detection._sample_timestamps",
        lambda duration, max_frames: [0.0, 2.0],
    )
    decoded = iter([_ppm_bytes(), None])
    monkeypatch.setattr(
        "dedupe.face_detection._extract_seek_frame_ppm",
        lambda path, timestamp, frame_width: next(decoded),
    )
    counter = _StubCounter([(1, 1, 0)])

    count = analyze_face_count(record, counter, cache_signature="sig")

    assert count is None
    assert record.face_count is None
    assert record.face_detection_signature is None
    assert "face counting failed" in (record.error or "")


def test_video_face_count_requires_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    record = _video_record(tmp_path / "clip.mp4")
    record.duration = 6.0
    monkeypatch.setattr("dedupe.face_detection.ffmpeg_available", lambda: False)
    counter = _StubCounter([])

    count = analyze_face_count(record, counter, cache_signature="sig")

    assert count is None
    assert record.face_count is None
    assert "ffmpeg" in (record.error or "")


def test_video_duration_probed_when_missing(tmp_path: Path, monkeypatch) -> None:
    record = _video_record(tmp_path / "clip.mp4")
    assert record.duration is None
    monkeypatch.setattr("dedupe.face_detection.ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        "dedupe.face_detection.probe_video", lambda path: (4.0, 320, 240)
    )
    _stub_video_frames(monkeypatch, [1.0])
    counter = _StubCounter([(0, 0, 0)])

    count = analyze_face_count(record, counter, cache_signature="sig")

    assert count == 0
    assert record.duration == 4.0
    assert record.width == 320
    assert record.height == 240


def test_count_faces_in_files_includes_videos(tmp_path: Path, monkeypatch) -> None:
    record = _video_record(tmp_path / "clip.mp4")
    record.duration = 2.0
    _stub_video_frames(monkeypatch, [0.0])
    monkeypatch.setattr(
        "dedupe.face_detection._YuNetFaceCounter", lambda: _StubCounter([(0, 0, 0)])
    )

    analyzed = count_faces_in_files([record], workers=1)

    assert analyzed == [record]
    assert record.face_count == 0
    assert record.face_detection_signature == face_detection_signature()
