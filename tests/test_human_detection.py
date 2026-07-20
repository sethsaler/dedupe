"""Optional offline person-candidate detector tests."""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from dedupe.human_detection import (
    _EnsemblePersonDetector,
    _OpenCVPersonDetector,
    create_person_detector,
    find_no_human_files,
    human_detection_signature,
)
from dedupe.scanner import inventory


def test_blank_landscape_is_review_candidate(tmp_path: Path) -> None:
    pytest.importorskip("cv2")
    path = tmp_path / "landscape.jpg"
    Image.new("RGB", (320, 180), (40, 120, 70)).save(path)
    record = inventory([path])[0]

    found = find_no_human_files([record])

    assert found == [record]
    assert record.human_detection_status == "no_person_detected"
    assert record.human_frames_analyzed == 1
    assert record.human_detector == "opencv_yunet_hog"


def test_opencv_detector_fails_closed_without_face_model(
    tmp_path: Path, monkeypatch
) -> None:
    pytest.importorskip("cv2")
    monkeypatch.setattr(
        "dedupe.human_detection.YUNET_MODEL_PATH", tmp_path / "missing.onnx"
    )

    with pytest.raises(RuntimeError, match="refusing to classify"):
        create_person_detector("opencv")


def test_opencv_detector_fails_closed_with_corrupt_face_model(
    tmp_path: Path, monkeypatch
) -> None:
    pytest.importorskip("cv2")
    corrupt = tmp_path / "corrupt.onnx"
    corrupt.write_bytes(b"not an ONNX model")
    monkeypatch.setattr("dedupe.human_detection.YUNET_MODEL_PATH", corrupt)

    with pytest.raises(RuntimeError, match="integrity check"):
        create_person_detector("opencv")


def test_opencv_detector_short_circuits_full_body_pass_after_face_hit() -> None:
    class FakeFace:
        def setInputSize(self, _size):
            return None

        def detect(self, _frame):
            face = np.zeros((1, 15), dtype=np.float32)
            face[0, -1] = 0.92
            return 1, face

    class FailingHog:
        def detectMultiScale(self, *_args, **_kwargs):
            raise AssertionError("HOG should not run after a YuNet face hit")

    class FakeCV2:
        COLOR_RGB2BGR = 1
        INTER_AREA = 2

        @staticmethod
        def cvtColor(frame, _code):
            return frame

        @staticmethod
        def resize(frame, _size, interpolation=None):
            return frame

    detector = _OpenCVPersonDetector.__new__(_OpenCVPersonDetector)
    detector.cv2 = FakeCV2()
    detector.confidence = 0.25
    detector.face = FakeFace()
    detector.hog = FailingHog()
    detector.backend = "opencv_yunet_hog"

    assert detector.score(np.zeros((240, 320, 3), dtype=np.uint8)) == pytest.approx(
        0.92
    )


def test_cached_person_decisions_skip_detector_and_keep_only_non_human(
    tmp_path: Path, monkeypatch
) -> None:
    signature = human_detection_signature("opencv")
    human_path = tmp_path / "human.jpg"
    landscape_path = tmp_path / "landscape.jpg"
    Image.new("RGB", (40, 40), "white").save(human_path)
    Image.new("RGB", (40, 40), "green").save(landscape_path)
    human, landscape = inventory([human_path, landscape_path])
    human.human_detection_status = "person_detected"
    human.human_detection_signature = signature
    landscape.human_detection_status = "no_person_detected"
    landscape.human_detection_signature = signature

    def fail_if_created(*_args, **_kwargs):
        raise AssertionError("cached files must not create or run the detector")

    monkeypatch.setattr(
        "dedupe.human_detection.create_person_detector", fail_if_created
    )

    assert find_no_human_files([human, landscape]) == [landscape]


def test_detector_signature_change_forces_reanalysis(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (40, 40), "white").save(path)
    record = inventory([path])[0]
    record.human_detection_status = "person_detected"
    record.human_detection_signature = "old-detector"
    calls = 0

    class FakeDetector:
        backend = "opencv-test"

        def score(self, _frame):
            nonlocal calls
            calls += 1
            return 0.0

        def close(self):
            return None

    monkeypatch.setattr(
        "dedupe.human_detection.create_person_detector",
        lambda *_args, **_kwargs: FakeDetector(),
    )

    assert find_no_human_files([record]) == [record]
    assert calls == 1
    assert record.human_detection_status == "no_person_detected"
    assert record.human_detection_signature == human_detection_signature("opencv")


def test_manual_human_confirmation_skips_every_detector_version(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (40, 40), "white").save(path)
    record = inventory([path])[0]
    record.human_detection_status = "person_confirmed"
    record.human_detector = "manual_review"
    record.human_detection_signature = None

    def fail_if_created(*_args, **_kwargs):
        raise AssertionError("manually confirmed files must not run the detector")

    monkeypatch.setattr(
        "dedupe.human_detection.create_person_detector", fail_if_created
    )

    assert find_no_human_files([record], backend="photon") == []


def test_photon_detector_loads_local_model_and_checks_person_then_face(monkeypatch) -> None:
    calls: dict = {"targets": []}

    class FakeModel:
        def detect(self, _image, target):
            calls["targets"].append(target)
            return {"objects": [] if target == "person" else [{"x_min": 0.1}]}

    def fake_vl(**kwargs):
        calls["init"] = kwargs
        return FakeModel()

    monkeypatch.setitem(sys.modules, "moondream", SimpleNamespace(vl=fake_vl))
    detector = create_person_detector("photon", photon_model="test-model")

    assert detector.score(np.zeros((24, 24, 3), dtype=np.uint8)) == 1.0
    assert calls["init"] == {"local": True, "model": "test-model"}
    assert calls["targets"] == ["person", "face"]
    assert detector.backend == "photon:test-model"


def test_ensemble_short_circuits_photon_after_opencv_positive() -> None:
    class FakeDetector:
        def __init__(self, backend, score):
            self.backend = backend
            self.value = score
            self.calls = 0

        def score(self, _frame):
            self.calls += 1
            return self.value

        def close(self):
            return None

    opencv = FakeDetector("opencv", 0.75)
    photon = FakeDetector("photon", 1.0)
    detector = _EnsemblePersonDetector(
        0.25,
        "unused",
        opencv=opencv,
        photon=photon,
    )

    assert detector.score(np.zeros((8, 8, 3), dtype=np.uint8)) == 0.75
    assert opencv.calls == 1
    assert photon.calls == 0
