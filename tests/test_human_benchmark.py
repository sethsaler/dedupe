"""Labeled person-detection benchmark tests."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from dedupe.human_benchmark import run_human_benchmark, summarize_predictions


def test_summary_prioritizes_missed_people() -> None:
    metrics = summarize_predictions(
        [
            {"path": "person-hit.jpg", "has_person": True, "predicted_has_person": True},
            {"path": "person-missed.jpg", "has_person": True, "predicted_has_person": False},
            {"path": "empty.jpg", "has_person": False, "predicted_has_person": False},
            {"path": "error.jpg", "has_person": False, "predicted_has_person": None},
        ]
    )

    assert metrics["evaluated"] == 3
    assert metrics["errors"] == 1
    assert metrics["person_recall"] == 0.5
    assert metrics["no_person_precision"] == 0.5
    assert metrics["false_negative_paths"] == ["person-missed.jpg"]


def test_benchmark_runs_one_detector_against_relative_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    Image.new("RGB", (40, 40), "white").save(tmp_path / "person.jpg")
    Image.new("RGB", (40, 40), "black").save(tmp_path / "empty.jpg")
    manifest = tmp_path / "benchmark.json"
    manifest.write_text(
        json.dumps(
            [
                {"path": "person.jpg", "has_person": True},
                {"path": "empty.jpg", "has_person": False},
            ]
        ),
        encoding="utf-8",
    )

    class FakeDetector:
        backend = "fake:opencv"

        def score(self, frame):
            return 1.0 if float(np.asarray(frame).mean()) > 127 else 0.0

        def close(self):
            return None

    monkeypatch.setattr(
        "dedupe.human_benchmark.create_person_detector",
        lambda *_args, **_kwargs: FakeDetector(),
    )

    report = run_human_benchmark(manifest, backends=["opencv"])
    result = report["results"]["opencv"]

    assert result["evaluated"] == 2
    assert result["accuracy"] == 1.0
    assert result["person_recall"] == 1.0
    assert result["no_person_precision"] == 1.0
    assert {Path(p["path"]).name for p in result["predictions"]} == {
        "person.jpg",
        "empty.jpg",
    }
