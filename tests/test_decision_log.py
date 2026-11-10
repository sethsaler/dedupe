"""Decision log: crop and distinct review decisions kept for tuning."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest
from PIL import Image

from dedupe.borders import BORDER_SIGNATURE, CropBox
from dedupe.cli import main as cli_main
from dedupe.decision_log import (
    append_decisions,
    benchmark_manifest,
    crop_manifest,
    current_crop_labels,
    current_distinct_labels,
    make_crop_event,
    make_distinct_event,
    make_distinct_path_event,
    pair_features,
    read_decisions,
    summarize_decisions,
    validate_skip_annotation,
)
from dedupe.grouping import build_crop_groups, build_groups
from dedupe.models import FileRecord, MediaType, ScanResult
from dedupe.similarity_benchmark import load_similarity_manifest
from dedupe.web import app as web_app
from dedupe.web.app import create_app

BOX = CropBox(left=0, top=30, right=320, bottom=210, width=320, height=240)


def _record(path: Path, **overrides) -> FileRecord:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"media")
    info = path.stat()
    fields = {
        "path": str(path),
        "size": info.st_size,
        "mtime": info.st_mtime,
        "media_type": MediaType.IMAGE,
        "extension": path.suffix.lower(),
        "device": info.st_dev,
        "inode": info.st_ino,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "width": 320,
        "height": 240,
        "phash": "0000000000000000",
        "dhash": "00000000000000ff",
        "border_crop": BOX.encode(),
        "border_signature": BORDER_SIGNATURE,
    }
    fields.update(overrides)
    return FileRecord(**fields)


def test_append_read_roundtrip_and_malformed_lines(tmp_path: Path) -> None:
    log = tmp_path / "state" / "decision-log.jsonl"
    record = _record(tmp_path / "a.png")
    written = append_decisions(
        [make_crop_event("skipped", record, BOX, reason="cuts_person")], log
    )
    assert written == 1
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    with log.open("a", encoding="utf-8") as stream:
        stream.write("not json\n{\"kind\": \"other\"}\n")

    events, skipped = read_decisions(log)
    assert skipped == 2
    assert len(events) == 1
    event = events[0]
    assert event["version"] == 1 and event["event_id"] and event["recorded_at"]
    # The rejected suggestion survives in the log even though the cache
    # verdict for a skip overwrites it with "dismissed".
    assert event["suggestion"]["top"] == 30
    assert event["suggestion_encoded"] == BOX.encode()
    assert event["reason"] == "cuts_person"
    assert read_decisions(tmp_path / "missing.jsonl") == ([], 0)


def test_pair_features_from_stored_hashes(tmp_path: Path) -> None:
    left = _record(tmp_path / "a.png", video_fingerprint="0000000000000000,ffffffffffffffff")
    right = _record(
        tmp_path / "b.png",
        phash="000000000000000f",
        dhash="00000000000000ff",
        video_fingerprint="000000000000000f,ffffffffffffffff",
    )
    features = pair_features(left, right)
    assert features["phash_distance"] == 4
    assert features["dhash_distance"] == 0
    assert features["video_mean_distance"] == 2.0
    assert features["video_max_distance"] == 4
    assert features["same_dimensions"] is True


def test_validate_skip_annotation() -> None:
    assert validate_skip_annotation("other", "  a note  ") == ("other", "a note")
    assert validate_skip_annotation(None, "") == (None, None)
    with pytest.raises(ValueError):
        validate_skip_annotation("bogus", None)
    with pytest.raises(ValueError):
        validate_skip_annotation(None, 3)


def _events(tmp_path: Path) -> list[dict]:
    a = _record(tmp_path / "a.png")
    b = _record(tmp_path / "b.png", phash="000000000000003f")
    c = _record(tmp_path / "c.png", phash="0000000000000003")
    return [
        make_crop_event("applied", a, BOX, source="single"),
        make_crop_event("skipped", b, BOX),
        make_crop_event("annotated", b, BOX, reason="bars_intentional", note="letterbox"),
        make_crop_event("applied", c, BOX, source="bulk"),
        make_crop_event("undone", c, BOX),
        make_distinct_event("marked", a, b, source="swipe", label_scope="pair"),
        make_distinct_event("marked", a, c, source="board", label_scope="group"),
        make_distinct_event("marked", b, c, source="swipe", label_scope="backfill"),
    ]


def test_current_labels_fold_reversals_and_annotations(tmp_path: Path) -> None:
    events = _events(tmp_path)
    crops = {e["file"]["path"]: e for e in current_crop_labels(events)}
    assert crops[str(tmp_path / "a.png")]["decision"] == "applied"
    skipped = crops[str(tmp_path / "b.png")]
    assert skipped["decision"] == "skipped"
    assert skipped["reason"] == "bars_intentional" and skipped["note"] == "letterbox"
    assert crops[str(tmp_path / "c.png")]["decision"] == "undone"

    events.append(make_crop_event("reconsidered", _record(tmp_path / "b.png"), BOX))
    assert str(tmp_path / "b.png") not in {
        e["file"]["path"] for e in current_crop_labels(events)
    }

    pairs = {(e["path_a"], e["path_b"]) for e in current_distinct_labels(events)}
    assert len(pairs) == 3
    events.append(make_distinct_path_event(
        "unmarked", str(tmp_path / "b.png"), str(tmp_path / "a.png"), source="undo"
    ))
    pairs = {(e["path_a"], e["path_b"]) for e in current_distinct_labels(events)}
    assert tuple(sorted((str(tmp_path / "a.png"), str(tmp_path / "b.png")))) not in pairs


def test_summary_rates_reasons_and_distances(tmp_path: Path) -> None:
    summary = summarize_decisions(_events(tmp_path), skipped_lines=1)
    assert summary["skipped_lines"] == 1
    crop = summary["crop"]
    assert crop["labels"] == {"applied": 1, "skipped": 1, "undone": 1}
    assert crop["acceptance_rate"] == round(1 / 3, 4)
    assert crop["applied_by_source"] == {"single": 1}
    assert crop["skip_reasons"] == {"bars_intentional": 1}
    assert crop["annotated_skips"] == 1
    assert crop["by_kind"]["black_bars"]["decided"] == 3
    distinct = summary["distinct"]
    assert distinct["current_pairs"] == 3
    assert distinct["by_label_scope"] == {"backfill": 1, "group": 1, "pair": 1}
    assert distinct["phash_distance"]["count"] == 3


def test_benchmark_manifest_defaults_to_inspected_pairs(tmp_path: Path) -> None:
    events = _events(tmp_path)
    manifest = benchmark_manifest(events)
    assert [(p["path_a"], p["path_b"]) for p in manifest["pairs"]] == [
        tuple(sorted((str(tmp_path / "a.png"), str(tmp_path / "b.png"))))
    ]
    assert manifest["pairs"][0]["similar"] is False
    wider = benchmark_manifest(events, scopes=("pair", "group", "backfill"))
    assert len(wider["pairs"]) == 3

    crops = crop_manifest(events)
    assert {item["decision"] for item in crops["items"]} == {"applied", "skipped", "undone"}


def test_exported_manifest_loads_in_similarity_benchmark(tmp_path: Path) -> None:
    for name, color in (("a.png", (200, 30, 30)), ("b.png", (30, 30, 200))):
        Image.new("RGB", (64, 48), color).save(tmp_path / name)
    a = _record(tmp_path / "a.png")
    b = _record(tmp_path / "b.png")
    log = tmp_path / "decisions.jsonl"
    append_decisions(
        [make_distinct_event("marked", a, b, source="swipe", label_scope="pair")], log
    )
    out = tmp_path / "manifest.json"
    assert cli_main(["decisions", "export-benchmark", str(out), "--log", str(log)]) == 0
    pairs = load_similarity_manifest(out)
    assert len(pairs) == 1 and pairs[0].similar is False


def test_cli_summary_and_crop_export(tmp_path: Path, capsys) -> None:
    log = tmp_path / "decisions.jsonl"
    append_decisions(_events(tmp_path), log)
    assert cli_main(["decisions", "summary", "--log", str(log)]) == 0
    text = capsys.readouterr().out
    assert "Crops: 3 decided" in text and "bars_intentional" in text
    assert cli_main(["decisions", "summary", "--log", str(log), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["distinct"]["current_pairs"] == 3

    out = tmp_path / "crops.json"
    assert cli_main(["decisions", "export-crops", str(out), "--log", str(log)]) == 0
    assert len(json.loads(out.read_text())["items"]) == 3


class _FakeBatch:
    trash_dir: Path

    def send(self, src: Path) -> Path:
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        target = self.trash_dir / Path(src).name
        shutil.move(str(src), str(target))
        return target


def _framed_image(path: Path) -> Path:
    image = Image.new("RGB", (320, 240), (3, 3, 3))
    image.paste(Image.new("RGB", (320, 180), (180, 120, 60)), (0, 30))
    image.save(path)
    return path


def test_web_crop_decisions_are_logged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _FakeBatch.trash_dir = tmp_path / "Trash"
    monkeypatch.setattr(web_app, "TrashBatch", _FakeBatch)
    one = _record(_framed_image(tmp_path / "one.png"))
    two = _record(_framed_image(tmp_path / "two.png"))
    group = build_crop_groups([one, two])[0]
    result = ScanResult(roots=[str(tmp_path)], files=[one, two], groups=[group])
    log = tmp_path / "decisions.jsonl"
    app = create_app(
        result,
        review_session_path=tmp_path / "session.json",
        cache_path=tmp_path / "cache.sqlite3",
        decision_log_path=log,
    )
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    base = {"group_id": group.id, "scan_id": scan_id}

    skipped = client.post("/api/crop/skip", json={**base, "path": one.path}, headers=headers)
    assert skipped.status_code == 200, skipped.get_json()
    member = next(m for m in skipped.get_json()["members"] if m["path"] == one.path)
    assert member["crop_skip_reason"] is None
    assert skipped.get_json()["crop_skip_reasons"]

    annotated = client.post(
        "/api/crop/annotate-skip",
        json={**base, "path": one.path, "reason": "cuts_person", "note": "face at top"},
        headers=headers,
    )
    assert annotated.status_code == 200, annotated.get_json()
    member = next(m for m in annotated.get_json()["members"] if m["path"] == one.path)
    assert member["crop_skip_reason"] == "cuts_person"

    bad = client.post(
        "/api/crop/annotate-skip",
        json={**base, "path": one.path, "reason": "nope"},
        headers=headers,
    )
    assert bad.status_code == 400

    applied = client.post("/api/crop/apply", json={**base, "path": two.path}, headers=headers)
    assert applied.status_code == 200, applied.get_json()
    undone = client.post("/api/crop/undo", json={**base, "path": two.path}, headers=headers)
    assert undone.status_code == 200, undone.get_json()

    reconsidered = client.post(
        "/api/crop/skip", json={**base, "path": one.path, "skip": False}, headers=headers
    )
    assert reconsidered.status_code == 200

    events, skipped_lines = read_decisions(log)
    assert skipped_lines == 0
    assert [e["decision"] for e in events] == [
        "skipped", "annotated", "applied", "undone", "reconsidered",
    ]
    assert events[0]["suggestion"]["top"] == 30
    assert events[2]["source"] == "single" and events[2]["after"]["height"] == 180
    labels = current_crop_labels(events)
    assert {e["file"]["path"]: e["decision"] for e in labels} == {two.path: "undone"}


def test_web_distinct_decisions_are_logged(tmp_path: Path) -> None:
    records = [
        _record(tmp_path / name, border_crop=None, border_signature=None)
        for name in ("a.jpg", "b.jpg", "c.jpg")
    ]
    result = ScanResult(
        roots=[str(tmp_path)], files=records, groups=build_groups([], [records])
    )
    group = result.groups[0]
    keeper = group.suggested_keep
    other = next(m.path for m in group.members if m.path != keeper)
    log = tmp_path / "decisions.jsonl"
    app = create_app(
        result,
        review_session_path=tmp_path / "session.json",
        cache_path=tmp_path / "cache.sqlite3",
        decision_log_path=log,
    )
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    marked = client.post(
        "/api/similar/mark-distinct",
        json={"group_id": group.id, "path": other, "scan_id": scan_id, "source": "swipe"},
        headers=headers,
    )
    assert marked.status_code == 200, marked.get_json()
    unmarked = client.post(
        "/api/similar/unmark-distinct",
        json={"group_id": group.id, "path": other, "scan_id": scan_id},
        headers=headers,
    )
    assert unmarked.status_code == 200, unmarked.get_json()

    events, _ = read_decisions(log)
    assert [(e["decision"], e["label_scope"], e["source"]) for e in events] == [
        ("marked", "pair", "swipe"),
        ("unmarked", "pair", "undo"),
    ]
    assert events[0]["pair"]["phash_distance"] == 0
    assert current_distinct_labels(events) == []
