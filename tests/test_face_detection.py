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
    """Deterministic counter that returns queued counts per frame."""

    backend = "stub_counter"

    def __init__(self, counts: list[int]) -> None:
        self.counts = list(counts)
        self.calls = 0

    def count(self, _rgb_frame) -> int:
        self.calls += 1
        return self.counts.pop(0) if self.counts else 0


def _image_record(path: Path) -> FileRecord:
    Image.new("RGB", (64, 64), (20, 90, 40)).save(path)
    return inventory([path])[0]


def test_signature_pins_version_and_model() -> None:
    signature = face_detection_signature()
    assert signature.startswith(FACE_COUNT_CACHE_VERSION + "|")
    assert "yunet=" in signature
    assert "face-confidence=" in signature


def test_blank_image_counts_zero_faces(tmp_path: Path) -> None:
    pytest.importorskip("cv2")
    record = _image_record(tmp_path / "blank.jpg")

    analyzed = count_faces_in_files([record], workers=1)

    assert analyzed == [record]
    assert record.face_count == 0
    assert record.face_detector == "opencv_yunet"
    assert record.face_detection_signature == face_detection_signature()


def test_analyze_face_count_keeps_max_across_gif_frames(tmp_path: Path) -> None:
    path = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (48, 48), (value, 0, 0)) for value in (10, 120, 240)]
    frames[0].save(path, save_all=True, append_images=frames[1:])
    record = inventory([path])[0]
    assert record.media_type == MediaType.GIF
    counter = _StubCounter([1, 3, 2])

    count = analyze_face_count(record, counter, cache_signature="sig")

    assert count == 3
    assert record.face_count == 3
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
        "dedupe.face_detection._YuNetFaceCounter", lambda: _StubCounter([1])
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
    assert record.face_detection_signature is None
    assert "face counting failed" in (record.error or "")


def test_videos_are_not_face_candidates(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video bytes")
    record = inventory([video])[0]
    assert record.media_type == MediaType.VIDEO

    assert count_faces_in_files([record], workers=1) == []
    assert record.face_count is None
