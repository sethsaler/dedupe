"""Focused tests for command-line diagnostics and benchmark handlers."""

from __future__ import annotations

import json
from pathlib import Path

from dedupe import cli


def test_parser_exposes_doctor_and_similarity_thresholds() -> None:
    doctor = cli.build_parser().parse_args(["doctor", "--json"])
    benchmark = cli.build_parser().parse_args(
        [
            "benchmark-similarity",
            "pairs.json",
            "--json",
            "report.json",
            "--threshold",
            "9",
            "--video-threshold",
            "11",
            "--workers",
            "2",
        ]
    )

    assert doctor.command == "doctor" and doctor.json is True
    assert (benchmark.threshold, benchmark.video_threshold, benchmark.workers) == (9, 11, 2)


def test_doctor_json_exit_status_only_tracks_core_readiness(monkeypatch, capsys) -> None:
    report = {
        "application": {"name": "dedupe", "version": "1.2.3"},
        "core_ready": True,
        "ffmpeg": {"available": False},
        "opencv": {"available": False},
    }
    monkeypatch.setattr(cli, "collect_doctor_report", lambda: report)

    assert cli.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report


def test_similarity_handler_writes_report_and_prioritizes_false_positives(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    report = {
        "total": 2,
        "evaluated": 2,
        "errors": 0,
        "false_positives": 1,
        "false_negatives": 1,
        "false_positive_pairs": [["fp-a", "fp-b"]],
        "false_negative_pairs": [["fn-a", "fn-b"]],
        "precision": 0.5,
        "recall": 0.5,
        "elapsed_seconds": 1.25,
    }
    calls = []

    def fake_run(manifest, **kwargs):
        calls.append((manifest, kwargs))
        return report

    monkeypatch.setattr("dedupe.similarity_benchmark.run_similarity_benchmark", fake_run)
    output = tmp_path / "report.json"

    code = cli.main(
        ["benchmark-similarity", "pairs.json", "--json", str(output), "--threshold", "7"]
    )

    text = capsys.readouterr().out
    assert code == 0
    assert text.index("False positives") < text.index("False negatives") < text.index("Precision")
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert calls == [("pairs.json", {"image_threshold": 7, "video_threshold": 8, "workers": None})]
