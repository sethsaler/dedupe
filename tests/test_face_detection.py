"""Face counting tests: caching, frame aggregation, and failure handling."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from dedupe.face_detection import (
    FACE_COUNT_CACHE_VERSION,
    _face_video_max_frames,
    _YuNetFaceCounter,
    analyze_face_count,
    count_faces_in_files,
    face_detection_signature,
    protect_no_person_candidates,
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


def test_protect_no_person_overrides_when_female_face_is_counted(
    tmp_path: Path, monkeypatch
) -> None:
    kept = _image_record(tmp_path / "woman.jpg")
    kept.human_detection_status = "no_person_detected"
    empty = _image_record(tmp_path / "empty.jpg")
    empty.human_detection_status = "no_person_detected"

    def fake_count(records, **_kwargs):
        for record in records:
            if record.path.endswith("woman.jpg"):
                record.face_count = 1
                record.female_face_count = 1
                record.male_face_count = 0
            else:
                record.face_count = 0
                record.female_face_count = 0
                record.male_face_count = 0
        return list(records)

    monkeypatch.setattr("dedupe.face_detection.count_faces_in_files", fake_count)

    protect_no_person_candidates([kept, empty])

    assert kept.human_detection_status == "person_detected"
    assert empty.human_detection_status == "no_person_detected"


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
        "dedupe.face_detection._extract_seek_frames_ppm",
        lambda path, ts, frame_width: [_ppm_bytes() for _ in ts],
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
    # The batched pass fails, then the per-frame fallback hits a bad frame.
    monkeypatch.setattr(
        "dedupe.face_detection._extract_seek_frames_ppm",
        lambda path, ts, frame_width: [],
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


def test_video_face_count_extracts_all_frames_in_one_ffmpeg_call(
    tmp_path: Path, monkeypatch
) -> None:
    record = _video_record(tmp_path / "clip.mp4")
    record.duration = 30.0
    monkeypatch.setattr("dedupe.face_detection.ffmpeg_available", lambda: True)
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        targets = [cmd[index + 1] for index, arg in enumerate(cmd) if arg == "-y"]
        for target in targets:
            Path(target).write_bytes(_ppm_bytes())
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr("dedupe.similar_video.subprocess.run", fake_run)

    def fail_per_frame(*_args, **_kwargs):
        raise AssertionError("per-frame extraction must not run after a full batch")

    monkeypatch.setattr(
        "dedupe.face_detection._extract_seek_frame_ppm", fail_per_frame
    )
    counter = _StubCounter([(1, 0, 1)] * 16)

    count = analyze_face_count(record, counter, cache_signature="sig")

    assert count == 1
    assert len(calls) == 1
    # 30s at ~1 frame per 5s → 6 sampled frames, all in one ffmpeg process.
    seeks = [
        float(calls[0][index + 1])
        for index, arg in enumerate(calls[0])
        if arg == "-ss"
    ]
    assert len(seeks) == 6
    assert counter.calls == 6


def test_video_face_count_falls_back_to_per_frame_seeks(
    tmp_path: Path, monkeypatch
) -> None:
    record = _video_record(tmp_path / "clip.mp4")
    record.duration = 10.0
    monkeypatch.setattr("dedupe.face_detection.ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        "dedupe.face_detection._sample_timestamps",
        lambda duration, max_frames: [0.0, 5.0],
    )
    monkeypatch.setattr(
        "dedupe.face_detection._extract_seek_frames_ppm",
        lambda path, ts, frame_width: [],
    )
    per_frame_calls: list[float] = []

    def fake_seek(_path, timestamp, frame_width):
        per_frame_calls.append(timestamp)
        return _ppm_bytes()

    monkeypatch.setattr(
        "dedupe.face_detection._extract_seek_frame_ppm", fake_seek
    )
    counter = _StubCounter([(0, 0, 0), (2, 1, 1)])

    count = analyze_face_count(record, counter, cache_signature="sig")

    assert count == 2
    assert per_frame_calls == [0.0, 5.0]


def test_face_video_frame_budget_scales_with_duration() -> None:
    assert _face_video_max_frames(3.0) == 4
    assert _face_video_max_frames(60.0) == 12
    assert _face_video_max_frames(600.0) == 16  # capped
    assert _face_video_max_frames(None) == 16
    assert _face_video_max_frames(0) == 16


class _FakeFaceDetector:
    def __init__(self) -> None:
        self.detect_calls: list[tuple[int, int]] = []

    def setInputSize(self, _size) -> None:
        return None

    def detect(self, frame):
        height, width = frame.shape[:2]
        self.detect_calls.append((width, height))
        return 1, None


class _FakeCV2:
    INTER_AREA = 1

    @staticmethod
    def resize(frame, size, interpolation=None):
        return np.zeros((size[1], size[0], 3), dtype=np.uint8)


def _fake_counter() -> tuple[_YuNetFaceCounter, _FakeFaceDetector]:
    counter = _YuNetFaceCounter.__new__(_YuNetFaceCounter)
    counter.cv2 = _FakeCV2()
    counter.face = _FakeFaceDetector()
    return counter, counter.face


def test_detect_best_skips_second_pass_when_short_side_is_small() -> None:
    counter, face = _fake_counter()

    faces, _candidate = counter._detect_best(np.zeros((240, 320, 3), dtype=np.uint8))

    assert faces is None
    # Short side 240 ≤ 480: the 480px pass would only upscale the same faces.
    assert face.detect_calls == [(320, 240)]


def test_detect_best_still_runs_second_pass_on_large_images() -> None:
    counter, face = _fake_counter()

    counter._detect_best(np.zeros((1080, 1920, 3), dtype=np.uint8))

    assert face.detect_calls == [(960, 540), (480, 270)]


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
