"""Labeled benchmark harness for local person-detection backends."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .human_detection import (
    DEFAULT_CONFIDENCE,
    DEFAULT_PHOTON_MODEL,
    HUMAN_BACKENDS,
    analyze_person_presence,
    create_person_detector,
)
from .models import FileRecord
from .scanner import inventory


@dataclass(frozen=True)
class BenchmarkItem:
    path: Path
    has_person: bool


def load_benchmark_manifest(path: str | Path) -> list[BenchmarkItem]:
    """Load a JSON list (or ``{"items": [...]}``) with labeled media paths."""
    manifest_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read benchmark manifest {manifest_path}: {exc}") from exc

    raw_items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("benchmark manifest must contain a non-empty JSON items list")

    items: list[BenchmarkItem] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise TypeError(f"benchmark item {index} must be an object")
        raw_path = raw.get("path")
        has_person = raw.get("has_person")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"benchmark item {index} needs a non-empty path")
        if not isinstance(has_person, bool):
            raise TypeError(f"benchmark item {index} has_person must be true or false")
        media_path = Path(raw_path).expanduser()
        if not media_path.is_absolute():
            media_path = manifest_path.parent / media_path
        media_path = media_path.resolve()
        if not media_path.is_file():
            raise ValueError(f"benchmark item {index} does not exist: {media_path}")
        records = inventory([media_path])
        if len(records) != 1:
            raise ValueError(f"benchmark item {index} is not supported media: {media_path}")
        items.append(BenchmarkItem(media_path, has_person))
    return items


def summarize_predictions(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate confusion-matrix metrics, emphasizing missed people."""
    evaluated = [p for p in predictions if p.get("predicted_has_person") is not None]
    tp = sum(p["has_person"] and p["predicted_has_person"] for p in evaluated)
    tn = sum(not p["has_person"] and not p["predicted_has_person"] for p in evaluated)
    fp = sum(not p["has_person"] and p["predicted_has_person"] for p in evaluated)
    fn = sum(p["has_person"] and not p["predicted_has_person"] for p in evaluated)

    def ratio(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    return {
        "total": len(predictions),
        "evaluated": len(evaluated),
        "errors": len(predictions) - len(evaluated),
        "true_positives": tp,
        "true_negatives": tn,
        "false_positives": fp,
        "false_negatives": fn,
        "person_recall": ratio(tp, tp + fn),
        "person_precision": ratio(tp, tp + fp),
        # Of everything declared safe to surface as "no person," how often was
        # that correct? This is the benchmark's primary safety measure.
        "no_person_precision": ratio(tn, tn + fn),
        "accuracy": ratio(tp + tn, len(evaluated)),
        "false_negative_paths": [
            p["path"]
            for p in evaluated
            if p["has_person"] and not p["predicted_has_person"]
        ],
        "false_positive_paths": [
            p["path"]
            for p in evaluated
            if not p["has_person"] and p["predicted_has_person"]
        ],
    }


def run_human_benchmark(
    manifest: str | Path,
    *,
    backends: list[str] | tuple[str, ...] = ("opencv",),
    confidence: float = DEFAULT_CONFIDENCE,
    photon_model: str = DEFAULT_PHOTON_MODEL,
) -> dict[str, Any]:
    """Run selected detectors on the same labeled media and return JSON-safe results."""
    manifest_path = Path(manifest).expanduser().resolve()
    items = load_benchmark_manifest(manifest_path)
    invalid = [backend for backend in backends if backend not in HUMAN_BACKENDS]
    if invalid:
        raise ValueError(f"unknown benchmark backend(s): {', '.join(invalid)}")

    results: dict[str, dict[str, Any]] = {}
    for backend in dict.fromkeys(backends):
        started = time.perf_counter()
        try:
            detector = create_person_detector(
                backend,
                confidence=confidence,
                photon_model=photon_model,
            )
        except Exception as exc:
            results[backend] = {
                "backend": backend,
                "error": str(exc),
                "predictions": [],
            }
            continue

        predictions: list[dict[str, Any]] = []
        try:
            for item in items:
                record: FileRecord = inventory([item.path])[0]
                item_started = time.perf_counter()
                prediction = analyze_person_presence(record, detector)
                predictions.append(
                    {
                        "path": str(item.path),
                        "has_person": item.has_person,
                        "predicted_has_person": prediction,
                        "status": record.human_detection_status,
                        "detector": record.human_detector,
                        "frames_analyzed": record.human_frames_analyzed,
                        "evidence_score": record.human_max_confidence,
                        "latency_seconds": time.perf_counter() - item_started,
                        "error": record.error,
                    }
                )
        finally:
            detector.close()

        elapsed = time.perf_counter() - started
        metrics = summarize_predictions(predictions)
        metrics.update(
            {
                "backend": backend,
                "detector": predictions[0]["detector"] if predictions else backend,
                "elapsed_seconds": elapsed,
                "media_per_second": len(predictions) / elapsed if elapsed else None,
                "predictions": predictions,
            }
        )
        results[backend] = metrics

    return {
        "manifest": str(manifest_path),
        "photon_model": photon_model,
        "item_count": len(items),
        "backends": list(dict.fromkeys(backends)),
        "results": results,
    }


def format_benchmark_report(report: dict[str, Any]) -> str:
    """Format a compact terminal comparison without extra dependencies."""
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    lines = [
        f"Person detection benchmark: {report['item_count']} labeled media files",
        "backend       evaluated  recall   no-person precision  accuracy  seconds",
    ]
    for backend in report["backends"]:
        result = report["results"][backend]
        if result.get("error"):
            lines.append(f"{backend:<13} ERROR: {result['error']}")
            continue
        lines.append(
            f"{backend:<13} {result['evaluated']:>9}/{result['total']:<3}  "
            f"{pct(result['person_recall']):>7}  "
            f"{pct(result['no_person_precision']):>19}  "
            f"{pct(result['accuracy']):>8}  "
            f"{result['elapsed_seconds']:>7.2f}"
        )
        if result["false_negative_paths"]:
            lines.append("  missed people: " + ", ".join(result["false_negative_paths"]))
    return "\n".join(lines)


def summarize_ranking(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """Metrics for "least likely to show people first" orderings.

    Ties break pessimistically (people first), unscored files rank last, and
    every metric treats people-free files as the items being retrieved:

    - ``nonhuman_average_precision``: average precision of the ascending order.
    - ``auc``: probability a people-free file scores below a file with people.
    - ``clean_run``: people-free files ranked before the first file with people.
    - ``yield_at_1pct`` / ``yield_at_5pct``: the share of people-free files
      reachable while files with people make up at most 1% / 5% of the prefix.
    """
    items = [item for item in scored if item.get("score") is not None]
    order = sorted(items, key=lambda item: (item["score"], not item["has_person"]))
    negatives = sum(not item["has_person"] for item in order)
    positives = len(order) - negatives
    hits = humans = 0
    precision_sum = 0.0
    clean_run = None
    yields = {0.01: 0, 0.05: 0}
    for rank, item in enumerate(order, start=1):
        if item["has_person"]:
            humans += 1
            if clean_run is None:
                clean_run = rank - 1
        else:
            hits += 1
            precision_sum += hits / rank
        for ceiling, best in yields.items():
            if humans <= ceiling * rank:
                yields[ceiling] = max(best, hits)
    if clean_run is None:
        clean_run = negatives
    wins = 0.0
    for person in (item for item in order if item["has_person"]):
        for other in (item for item in order if not item["has_person"]):
            if other["score"] < person["score"]:
                wins += 1
            elif other["score"] == person["score"]:
                wins += 0.5

    def ratio(numerator: float, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    return {
        "total": len(scored),
        "evaluated": len(order),
        "errors": len(scored) - len(order),
        "with_people": positives,
        "without_people": negatives,
        "nonhuman_average_precision": ratio(precision_sum, negatives),
        "auc": ratio(wins, positives * negatives),
        "clean_run": clean_run,
        "yield_at_1pct": ratio(yields[0.01], negatives),
        "yield_at_5pct": ratio(yields[0.05], negatives),
        "lowest_scored_people": [
            {"path": item["path"], "score": item["score"]}
            for item in order
            if item["has_person"]
        ][:10],
        "highest_scored_without_people": [
            {"path": item["path"], "score": item["score"]}
            for item in reversed(order)
            if not item["has_person"]
        ][:10],
    }


def run_people_rank_benchmark(
    manifest: str | Path, *, workers: int | None = None
) -> dict[str, Any]:
    """Score a labeled manifest with the people-likelihood model and rank it."""
    from .people_score import people_score_signature, score_people

    manifest_path = Path(manifest).expanduser().resolve()
    items = load_benchmark_manifest(manifest_path)
    records = [inventory([item.path])[0] for item in items]
    started = time.perf_counter()
    score_people(records, workers=workers)
    elapsed = time.perf_counter() - started
    scored = [
        {"path": str(item.path), "has_person": item.has_person, "score": record.people_score}
        for item, record in zip(items, records, strict=True)
    ]
    metrics = summarize_ranking(scored)
    metrics.update(
        {
            "manifest": str(manifest_path),
            "scorer": people_score_signature(),
            "elapsed_seconds": elapsed,
            "media_per_second": len(records) / elapsed if elapsed else None,
            "predictions": scored,
        }
    )
    return metrics


def format_ranking_report(report: dict[str, Any]) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    lines = [
        (
            f"People-likelihood ranking: {report['evaluated']}/{report['total']} labeled files "
            f"({report['with_people']} with people, {report['without_people']} without)"
        ),
        f"  scorer:                {report['scorer']}",
        f"  non-human AP:          {pct(report['nonhuman_average_precision'])}",
        f"  AUC:                   {pct(report['auc'])}",
        (
            f"  clean run:             {report['clean_run']} people-free files before the "
            "first file with people"
        ),
        f"  yield at 1% / 5%:      {pct(report['yield_at_1pct'])} / {pct(report['yield_at_5pct'])}",
        f"  speed:                 {report['media_per_second'] or 0:.1f} files/s",
    ]
    if report["lowest_scored_people"]:
        lines.append("  people ranked least likely:")
        lines.extend(
            f"    {item['score']:.4f}  {item['path']}" for item in report["lowest_scored_people"][:5]
        )
    return "\n".join(lines)
