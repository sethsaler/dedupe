"""Labeled benchmark harness for image and video similarity detection."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import FileRecord, MediaType
from .scanner import inventory
from .semantic import DEFAULT_DUPLICATE_THRESHOLD as SEMANTIC_DUPLICATE_THRESHOLD
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
    category: str = "unspecified"


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
            raise TypeError(f"benchmark pair {index} must be an object")
        expected = raw.get("similar")
        if not isinstance(expected, bool):
            raise TypeError(f"benchmark pair {index} similar must be true or false")

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
        pairs.append(SimilarityBenchmarkPair(
            paths[0], paths[1], expected, media_type, str(raw.get("category", "unspecified"))
        ))
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


def _cosine_similarity(vec_a: bytes, vec_b: bytes) -> float:
    import numpy as np

    a = np.frombuffer(vec_a, dtype="<f4")
    b = np.frombuffer(vec_b, dtype="<f4")
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


def run_similarity_benchmark(
    manifest: str | Path,
    *,
    image_threshold: int = IMAGE_THRESHOLD,
    video_threshold: int = VIDEO_THRESHOLD,
    workers: int | None = None,
    semantic: bool = False,
    semantic_duplicate_threshold: float = SEMANTIC_DUPLICATE_THRESHOLD,
    decider: bool = False,
) -> dict[str, Any]:
    """Run existing similarity detectors against labeled pairs."""
    manifest_path = Path(manifest).expanduser().resolve()
    pairs = load_similarity_manifest(manifest_path)
    started = time.perf_counter()
    predictions: list[dict[str, Any]] = []
    judge = None
    if decider:
        from .decider import DeciderJudge

        judge = DeciderJudge(use_cache=False)

    perceptual_seconds_total = 0.0
    video_seconds_total = 0.0
    semantic_embed_seconds = 0.0
    semantic_verify_seconds = 0.0
    pending_semantic: list[tuple[int, list[FileRecord]]] = []

    try:
        for idx, pair in enumerate(pairs):
            pair_started = time.perf_counter()
            records: list[FileRecord] = inventory([pair.path_a, pair.path_b])
            error: str | None = None
            predicted: bool | None = None
            matched_stage = "none"
            perceptual_secs = 0.0
            try:
                if pair.media_type == MediaType.VIDEO:
                    if not ffmpeg_available():
                        groups = []
                        error = "ffmpeg and ffprobe are required to benchmark video similarity"
                    else:
                        groups = find_similar_video_groups(
                            records, threshold=video_threshold, workers=workers
                        )
                    perceptual_secs = time.perf_counter() - pair_started
                    video_seconds_total += perceptual_secs
                    if groups:
                        matched_stage = "video"
                else:
                    groups = find_similar_image_groups(
                        records, threshold=image_threshold, workers=workers
                    )
                    perceptual_secs = time.perf_counter() - pair_started
                    perceptual_seconds_total += perceptual_secs
                    if groups:
                        matched_stage = "perceptual"
                record_error = next((record.error for record in records if record.error), None)
                if error or record_error:
                    error = error or record_error
                    predicted = None
                    matched_stage = "error"
                else:
                    predicted = bool(groups)
                    if semantic and pair.media_type == MediaType.IMAGE and not groups:
                        pending_semantic.append((idx, records))
            except Exception as exc:
                error = str(exc)
                predicted = None
                matched_stage = "error"
            predictions.append(
                {
                    "path_a": str(pair.path_a),
                    "path_b": str(pair.path_b),
                    "media_type": pair.media_type.value,
                    "category": pair.category,
                    "similar": pair.similar,
                    "predicted_similar": predicted,
                    "matched_stage": matched_stage,
                    "perceptual_seconds": perceptual_secs,
                    "semantic_seconds": 0.0,
                    "semantic_score": None,
                    "latency_seconds": time.perf_counter() - pair_started,
                    "error": error,
                }
            )

        unique_semantic_paths = 0
        if semantic and pending_semantic:
            from .semantic import embed_records, group_semantic_vectors

            unique_by_path: dict[str, FileRecord] = {}
            for _idx, pair_records in pending_semantic:
                for rec in pair_records:
                    unique_by_path.setdefault(rec.path, rec)
            unique_records = list(unique_by_path.values())
            unique_semantic_paths = len(unique_records)

            embed_started = time.perf_counter()
            vectors: dict[str, bytes] = {}
            global_error: str | None = None
            per_path_errors: dict[str, str] = {}
            try:
                vectors, embed_errors = embed_records(
                    unique_records,
                    workers=workers or 1,
                    use_cache=False,
                )
                for msg in embed_errors:
                    # Format: "semantic embedding failed: <path>: <reason>"
                    parts = msg.split(": ", 2)
                    if len(parts) >= 2:
                        per_path_errors.setdefault(parts[1], msg)
            except Exception as exc:
                global_error = str(exc)
            semantic_embed_seconds = time.perf_counter() - embed_started

            for idx, pair_records in pending_semantic:
                pred = predictions[idx]
                verify_started = time.perf_counter()
                path_a, path_b = pair_records[0].path, pair_records[1].path
                if global_error is not None:
                    pred["error"] = global_error
                    pred["predicted_similar"] = None
                    pred["matched_stage"] = "error"
                elif path_a not in vectors or path_b not in vectors:
                    err_parts = [
                        per_path_errors[p]
                        for p in (path_a, path_b)
                        if p in per_path_errors
                    ]
                    pred["error"] = "; ".join(err_parts) or "semantic embedding missing"
                    pred["predicted_similar"] = None
                    pred["matched_stage"] = "error"
                else:
                    pair_vectors = {path_a: vectors[path_a], path_b: vectors[path_b]}
                    pred["semantic_score"] = _cosine_similarity(vectors[path_a], vectors[path_b])
                    try:
                        _sets, sem_groups = group_semantic_vectors(
                            pair_records,
                            pair_vectors,
                            duplicate_threshold=semantic_duplicate_threshold,
                            workers=workers or 1,
                            use_cache=False,
                            judge=judge,
                        )
                        if sem_groups:
                            pred["predicted_similar"] = True
                            pred["matched_stage"] = "semantic"
                        else:
                            pred["predicted_similar"] = False
                            pred["matched_stage"] = "none"
                    except Exception as exc:
                        pred["error"] = str(exc)
                        pred["predicted_similar"] = None
                        pred["matched_stage"] = "error"
                sem_secs = time.perf_counter() - verify_started
                semantic_verify_seconds += sem_secs
                pred["semantic_seconds"] = sem_secs
                pred["latency_seconds"] += sem_secs
    finally:
        if judge is not None:
            judge.close()

    elapsed = time.perf_counter() - started
    report = summarize_similarity_predictions(predictions)
    report["categories"] = {
        category: summarize_similarity_predictions(
            [p for p in predictions if p["category"] == category]
        )
        for category in sorted({p["category"] for p in predictions})
    }
    stage_counts: dict[str, int] = {}
    for pred in predictions:
        stage = str(pred["matched_stage"])
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    report["diagnostics"] = {
        "stage_counts": stage_counts,
        "perceptual_seconds": perceptual_seconds_total,
        "video_seconds": video_seconds_total,
        "semantic_embed_seconds": semantic_embed_seconds,
        "semantic_verify_seconds": semantic_verify_seconds,
        "semantic_unique_paths": unique_semantic_paths if semantic else 0,
    }
    report["semantic"] = semantic
    report["decider"] = decider
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
