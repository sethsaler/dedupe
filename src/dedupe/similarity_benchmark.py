"""Labeled benchmark harness for image and video similarity detection."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import FileRecord, MediaType
from .scanner import inventory
from .similar_image import DEFAULT_THRESHOLD as IMAGE_THRESHOLD
from .similar_image import find_similar_image_groups
from .similar_video import DEFAULT_THRESHOLD as VIDEO_THRESHOLD
from .similar_video import ffmpeg_available, find_similar_video_groups


@dataclass(frozen=True)
class SimilarityBenchmarkPair:
    path_a: Path
    path_b: Path
    similar: bool
    media_type: MediaType


def load_similarity_manifest(path: str | Path) -> list[SimilarityBenchmarkPair]:
    """Load a JSON list (or ``{"pairs": [...]}``) of labeled media pairs."""
    manifest_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read benchmark manifest {manifest_path}: {exc}") from exc

    raw_pairs = data.get("pairs") if isinstance(data, dict) else data
    if not isinstance(raw_pairs, list) or not raw_pairs:
        raise ValueError("benchmark manifest must contain a non-empty JSON pairs list")

    pairs: list[SimilarityBenchmarkPair] = []
    for index, raw in enumerate(raw_pairs, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"benchmark pair {index} must be an object")
        expected = raw.get("similar")
        if not isinstance(expected, bool):
            raise ValueError(f"benchmark pair {index} similar must be true or false")

        paths: list[Path] = []
        for field in ("path_a", "path_b"):
            value = raw.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"benchmark pair {index} needs a non-empty {field}")
            media_path = Path(value).expanduser()
            if not media_path.is_absolute():
                media_path = manifest_path.parent / media_path
            media_path = media_path.resolve()
            records = inventory([media_path])
            if len(records) != 1:
                raise ValueError(
                    f"benchmark pair {index} is missing or unsupported media: {media_path}"
                )
            paths.append(media_path)

        records = inventory(paths)
        if len(records) != 2:
            raise ValueError(f"benchmark pair {index} paths must identify two media files")
        types = {record.media_type for record in records}
        image_types = {MediaType.IMAGE, MediaType.GIF}
        if types <= image_types:
            media_type = MediaType.IMAGE
        elif types == {MediaType.VIDEO}:
            media_type = MediaType.VIDEO
        else:
            raise ValueError(f"benchmark pair {index} must contain the same media kind")
        pairs.append(SimilarityBenchmarkPair(paths[0], paths[1], expected, media_type))
    return pairs


def summarize_similarity_predictions(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate similarity precision and recall from per-pair predictions."""
    evaluated = [p for p in predictions if p.get("predicted_similar") is not None]
    tp = sum(p["similar"] and p["predicted_similar"] for p in evaluated)
    tn = sum(not p["similar"] and not p["predicted_similar"] for p in evaluated)
    fp = sum(not p["similar"] and p["predicted_similar"] for p in evaluated)
    fn = sum(p["similar"] and not p["predicted_similar"] for p in evaluated)

    def ratio(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    def paths(items: list[dict[str, Any]]) -> list[list[str]]:
        return [[item["path_a"], item["path_b"]] for item in items]

    false_positives = [p for p in evaluated if not p["similar"] and p["predicted_similar"]]
    false_negatives = [p for p in evaluated if p["similar"] and not p["predicted_similar"]]
    return {
        "total": len(predictions),
        "evaluated": len(evaluated),
        "errors": len(predictions) - len(evaluated),
        "true_positives": tp,
        "true_negatives": tn,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "false_positive_pairs": paths(false_positives),
        "false_negative_pairs": paths(false_negatives),
    }


def run_similarity_benchmark(
    manifest: str | Path,
    *,
    image_threshold: int = IMAGE_THRESHOLD,
    video_threshold: int = VIDEO_THRESHOLD,
    workers: int | None = None,
) -> dict[str, Any]:
    """Run existing similarity detectors against labeled pairs."""
    manifest_path = Path(manifest).expanduser().resolve()
    pairs = load_similarity_manifest(manifest_path)
    started = time.perf_counter()
    predictions: list[dict[str, Any]] = []

    for pair in pairs:
        pair_started = time.perf_counter()
        records: list[FileRecord] = inventory([pair.path_a, pair.path_b])
        error: str | None = None
        predicted: bool | None
        try:
            if pair.media_type == MediaType.VIDEO:
                if not ffmpeg_available():
                    groups = []
                    error = "ffmpeg and ffprobe are required to benchmark video similarity"
                else:
                    groups = find_similar_video_groups(
                        records, threshold=video_threshold, workers=workers
                    )
            else:
                groups = find_similar_image_groups(
                    records, threshold=image_threshold, workers=workers
                )
            record_error = next((record.error for record in records if record.error), None)
            if error or record_error:
                error = error or record_error
                predicted = None
            else:
                predicted = bool(groups)
        except Exception as exc:
            error = str(exc)
            predicted = None
        predictions.append(
            {
                "path_a": str(pair.path_a),
                "path_b": str(pair.path_b),
                "media_type": pair.media_type.value,
                "similar": pair.similar,
                "predicted_similar": predicted,
                "latency_seconds": time.perf_counter() - pair_started,
                "error": error,
            }
        )

    elapsed = time.perf_counter() - started
    report = summarize_similarity_predictions(predictions)
    report.update(
        {
            "manifest": str(manifest_path),
            "image_threshold": image_threshold,
            "video_threshold": video_threshold,
            "elapsed_seconds": elapsed,
            "pairs_per_second": len(predictions) / elapsed if elapsed else None,
            "predictions": predictions,
        }
    )
    return report
