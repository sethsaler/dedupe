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


def _quarantine_receipts(tmp_path: Path):
    from dedupe.actions import apply_actions
    from dedupe.grouping import build_groups
    from dedupe.models import FileRecord, MediaType

    def _rec(path: Path, data: bytes) -> FileRecord:
        path.write_bytes(data)
        st = path.stat()
        return FileRecord(
            path=str(path.resolve()),
            size=st.st_size,
            mtime=st.st_mtime,
            media_type=MediaType.IMAGE,
            extension=path.suffix.lower(),
        )

    logs = tmp_path / "logs"
    groups = build_groups([[_rec(tmp_path / "a.jpg", b"dup"), _rec(tmp_path / "b.jpg", b"dup")]], [])
    apply_actions(
        groups, action="quarantine", quarantine_dir=tmp_path / "q",
        dry_run=True, log_dir=logs,
    )
    executed = apply_actions(
        groups, action="quarantine", quarantine_dir=tmp_path / "q",
        dry_run=False, log_dir=logs, roots=[str(tmp_path)],
    )
    return logs, executed


def test_receipts_list_json_is_newest_first(tmp_path: Path, capsys) -> None:
    logs, executed = _quarantine_receipts(tmp_path)

    assert cli.main(["receipts", "list", "--log-dir", str(logs), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [entry["log_path"] for entry in payload][0] == executed.log_path
    assert payload[0]["undoable"] is True
    assert any(entry["dry_run"] for entry in payload)

    assert cli.main(["receipts", "list", "--log-dir", str(logs), "--no-previews", "--json"]) == 0
    executed_only = json.loads(capsys.readouterr().out)
    assert len(executed_only) == 1


def test_receipts_show_accepts_an_id_and_reports_unknown_ids(tmp_path: Path, capsys) -> None:
    logs, executed = _quarantine_receipts(tmp_path)
    receipt_id = Path(executed.log_path).stem

    assert cli.main(["receipts", "show", receipt_id, "--log-dir", str(logs)]) == 0
    text = capsys.readouterr().out
    assert receipt_id in text
    assert "quarantine (executed)" in text

    assert cli.main(["receipts", "show", "does-not-exist", "--log-dir", str(logs)]) == 2
    assert "no receipt matching" in capsys.readouterr().err


def test_receipts_prune_requires_a_criterion_and_previews_by_default(
    tmp_path: Path, capsys
) -> None:
    logs, _ = _quarantine_receipts(tmp_path)

    assert cli.main(["receipts", "prune", "--log-dir", str(logs)]) == 2
    assert "at least one of" in capsys.readouterr().err

    assert cli.main(["receipts", "prune", "--log-dir", str(logs), "--drop-previews"]) == 0
    assert "DRY-RUN prune: 1 receipts" in capsys.readouterr().out
    assert len(list(logs.iterdir())) == 2

    assert cli.main(
        ["receipts", "prune", "--log-dir", str(logs), "--drop-previews", "--execute", "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed_count"] == 1 and payload["kept_count"] == 1
    assert len(list(logs.iterdir())) == 1


def test_undo_resolves_a_receipt_id(tmp_path: Path, capsys) -> None:
    logs, executed = _quarantine_receipts(tmp_path)
    quarantined = Path(executed.items[0].destination)
    original = Path(executed.items[0].path)
    assert quarantined.exists() and not original.exists()

    assert cli.main(["undo", Path(executed.log_path).stem, "--log-dir", str(logs)]) == 0
    assert "DRY-RUN undo: 1 ok" in capsys.readouterr().out
    assert not original.exists()

    assert cli.main(
        ["undo", Path(executed.log_path).stem, "--log-dir", str(logs), "--execute"]
    ) == 0
    assert "EXECUTED undo: 1 ok" in capsys.readouterr().out
    assert original.exists() and not quarantined.exists()


def test_undo_reports_unknown_receipt(tmp_path: Path, capsys) -> None:
    assert cli.main(["undo", "missing-receipt", "--log-dir", str(tmp_path / "logs")]) == 2
    assert "no receipt matching" in capsys.readouterr().err
