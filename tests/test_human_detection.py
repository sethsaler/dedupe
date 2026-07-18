"""Optional offline person-candidate detector tests."""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from dedupe.human_detection import (
    _EnsemblePersonDetector,
    create_person_detector,
    find_no_human_files,
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
    assert record.human_detector in {"opencv_hog", "opencv_face_hog"}


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
