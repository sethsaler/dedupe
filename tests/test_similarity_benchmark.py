"""Labeled similarity benchmark tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from dedupe.similarity_benchmark import (
    load_similarity_manifest,
    run_similarity_benchmark,
    summarize_similarity_predictions,
)


def test_summary_reports_precision_recall_and_mistake_pairs() -> None:
    predictions = [
        {"path_a": "a", "path_b": "b", "similar": True, "predicted_similar": True},
        {"path_a": "c", "path_b": "d", "similar": True, "predicted_similar": False},
        {"path_a": "e", "path_b": "f", "similar": False, "predicted_similar": True},
        {"path_a": "g", "path_b": "h", "similar": False, "predicted_similar": None},
    ]

    result = summarize_similarity_predictions(predictions)

    assert result["precision"] == 0.5
    assert result["recall"] == 0.5
    assert result["errors"] == 1
    assert result["false_positive_pairs"] == [["e", "f"]]
    assert result["false_negative_pairs"] == [["c", "d"]]


def test_benchmark_runs_image_primitive_and_is_json_serializable(
    tmp_path: Path, monkeypatch
) -> None:
    for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
        Image.new("RGB", (20, 20), "white").save(tmp_path / name)
    manifest = tmp_path / "pairs.json"
    manifest.write_text(json.dumps({"pairs": [
        {"path_a": "a.jpg", "path_b": "b.jpg", "similar": True},
        {"path_a": "c.jpg", "path_b": "d.jpg", "similar": False},
    ]}), encoding="utf-8")

    calls = []

    def fake_compare(records, **kwargs):
        calls.append((records, kwargs))
        return [records] if Path(records[0].path).name == "a.jpg" else []

    monkeypatch.setattr(
        "dedupe.similarity_benchmark.find_similar_image_groups", fake_compare
    )
    report = run_similarity_benchmark(manifest, image_threshold=7, workers=1)

    assert report["precision"] == report["recall"] == 1.0
    assert report["false_positive_pairs"] == []
    assert report["elapsed_seconds"] >= 0
    assert len(report["predictions"]) == len(calls) == 2
    assert calls[0][1] == {"threshold": 7, "workers": 1}
    json.dumps(report)


def test_manifest_rejects_mixed_media_pair(tmp_path: Path) -> None:
    Image.new("RGB", (10, 10)).save(tmp_path / "image.jpg")
    (tmp_path / "video.mp4").write_bytes(b"not needed for inventory")
    manifest = tmp_path / "pairs.json"
    manifest.write_text(json.dumps([
        {"path_a": "image.jpg", "path_b": "video.mp4", "similar": False}
    ]), encoding="utf-8")

    with pytest.raises(ValueError, match="same media kind"):
        load_similarity_manifest(manifest)


def test_video_benchmark_is_unevaluated_without_ffmpeg(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "a.mp4").write_bytes(b"first video")
    (tmp_path / "b.mp4").write_bytes(b"second video")
    manifest = tmp_path / "pairs.json"
    manifest.write_text(
        json.dumps([
            {"path_a": "a.mp4", "path_b": "b.mp4", "similar": True}
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr("dedupe.similarity_benchmark.ffmpeg_available", lambda: False)

    report = run_similarity_benchmark(manifest)

    assert report["evaluated"] == 0
    assert report["errors"] == 1
    assert report["false_negatives"] == 0
    assert report["predictions"][0]["predicted_similar"] is None
    assert "ffmpeg" in report["predictions"][0]["error"]
