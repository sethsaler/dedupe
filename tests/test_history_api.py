"""History remains usable before loading any scan after an app restart."""

import json
import os
import threading
import time
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageCms

from dedupe import receipts
from dedupe.actions import ActionItem, ActionResult, _write_action_log, apply_actions
from dedupe.cache import HashCache
from dedupe.grouping import build_groups
from dedupe.history import action_label, list_history
from dedupe.models import FileRecord, MediaType
from dedupe.web import media as web_media
from dedupe.web.app import create_app


def records(tmp_path, count=2):
    result = []
    for index in range(count):
        path = tmp_path / f"photo-{index}.jpg"
        path.write_bytes(b"same image")
        stat = path.stat()
        result.append(FileRecord(path=str(path), size=stat.st_size, mtime=stat.st_mtime,
                                 media_type=MediaType.IMAGE, extension=".jpg"))
    return result


def client_for(tmp_path):
    app = create_app(review_session_path=tmp_path / "session.json",
                     cache_path=tmp_path / "cache.sqlite3")
    app.config["TESTING"] = True
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    return client, headers, client.get("/api/status").get_json()["scan_id"]


def test_history_restores_receipt_without_loading_a_scan_after_restart(tmp_path):
    groups = build_groups([records(tmp_path)], [])
    receipt = apply_actions(groups, action="quarantine", quarantine_dir=tmp_path / "q",
                            log_dir=tmp_path / "logs", dry_run=False)
    source = Path(receipt.items[0].path)
    client, headers, scan_id = client_for(tmp_path)
    assert not client.get("/api/status").get_json()["has_result"]
    history = client.get("/api/history").get_json()
    assert len(history["actions"]) == 1
    assert history["actions"][0]["undoable"]
    reference = history["actions"][0]["id"]
    body = {"receipts": [reference], "scan_id": scan_id, "dry_run": True}
    preview = client.post("/api/action/undo", json=body, headers=headers)
    assert preview.status_code == 200
    assert preview.get_json()["success_count"] == 1
    assert not source.exists()
    body.update(dry_run=False, preview_token=preview.get_json()["preview_token"])
    restored = client.post("/api/action/undo", json=body, headers=headers)
    assert restored.status_code == 200
    assert restored.get_json()["success_count"] == 1
    assert source.read_bytes() == b"same image"
    fresh_client, _headers, _scan_id = client_for(tmp_path)
    row = next(row for row in fresh_client.get("/api/history").get_json()["actions"]
               if row["id"] == reference)
    assert not row["undoable"]


def test_history_pages_actions_and_persisted_decisions(tmp_path):
    for index in range(3):
        receipt = ActionResult(dry_run=False, action="trash",
                               started_at=f"2026-11-0{index + 1}T12:00:00+00:00")
        receipt.items = [ActionItem(path=f"/example/{index}", ok=False, action="trash")]
        _write_action_log(receipt, tmp_path / "logs")
    cache = HashCache(tmp_path / "cache.sqlite3")
    cache.mark_distinct(records(tmp_path, 3))
    cache.close()
    client, _headers, _scan_id = client_for(tmp_path)
    first = client.get("/api/history?limit=2&offset=0").get_json()
    second = client.get("/api/history?limit=2&offset=2").get_json()
    assert first["action_count"] == first["decision_count"] == 3
    assert len(first["actions"]) == len(first["decisions"]) == 2
    assert len(second["actions"]) == len(second["decisions"]) == 1
    assert {row["id"] for row in first["actions"]}.isdisjoint(
        row["id"] for row in second["actions"]
    )
    assert first["actions"][0]["started_at"] > second["actions"][0]["started_at"]


def test_history_reconsider_requires_token_and_current_session(tmp_path):
    cache = HashCache(tmp_path / "cache.sqlite3")
    members = records(tmp_path)
    cache.mark_distinct(members)
    cache.close()
    client, headers, scan_id = client_for(tmp_path)
    row = client.get("/api/history").get_json()["decisions"][0]
    body = {"path_a": row["path_a"], "path_b": row["path_b"], "scan_id": scan_id}
    assert client.post("/api/history/reconsider", json=body).status_code == 403
    stale = client.post("/api/history/reconsider", json={**body, "scan_id": "stale"},
                        headers=headers)
    assert stale.status_code == 409
    assert client.get("/api/history").get_json()["decision_count"] == 1
    response = client.post("/api/history/reconsider", json=body, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["removed_pairs"] == 1
    fresh_client, _headers, _scan_id = client_for(tmp_path)
    assert fresh_client.get("/api/history").get_json()["decision_count"] == 0
    assert all(Path(record.path).exists() for record in members)


def test_history_exposes_action_labels_and_reads_receipts_once(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    for index, action in enumerate(("trash", "quarantine", "isolate:copy", "undo:quarantine")):
        receipt = ActionResult(
            dry_run=False,
            action=action,
            started_at=f"2026-11-0{index + 1}T12:00:00+00:00",
        )
        receipt.items = [ActionItem(path=f"/example/{index}", ok=False, action=action)]
        _write_action_log(receipt, logs)
    # Preview receipts must be skipped before opening/parsing.
    preview_path = logs / "preview-20261105T120000Z-deadbeef.json"
    preview_path.write_text("{corrupt preview that must not be read", encoding="utf-8")

    read_counts: dict[str, int] = {}
    orig_read_bytes = Path.read_bytes

    def tracking_read_bytes(self: Path) -> bytes:
        if self.parent == logs and self.suffix == ".json":
            read_counts[self.name] = read_counts.get(self.name, 0) + 1
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", tracking_read_bytes)
    history = list_history(logs, cache_path=tmp_path / "cache.sqlite3")

    assert history["action_count"] == 4
    assert preview_path.name not in read_counts
    assert all(count == 1 for count in read_counts.values())
    labels = {row["action"]: row["action_label"] for row in history["actions"]}
    assert labels == {
        "trash": "Moved to Trash",
        "quarantine": "Moved to Quarantine",
        "isolate:copy": "Isolated (Copy)",
        "undo:quarantine": "Restored from Quarantine",
    }
    assert action_label("crop") == "Cropped Borders"
    assert action_label("isolate:hardlink") == "Isolated (Hardlink)"


def test_history_supports_crop_receipts_and_missing_backup_reason(tmp_path):
    logs = tmp_path / "logs"
    cropped = tmp_path / "cropped.jpg"
    cropped.write_bytes(b"cropped-bytes")
    trashed_orig = tmp_path / "trash" / "cropped.jpg"
    trashed_orig.parent.mkdir()
    trashed_orig.write_bytes(b"original-bytes")

    receipt = ActionResult(dry_run=False, action="crop", started_at="2026-11-10T12:00:00+00:00")
    receipt.items = [
        ActionItem(
            path=str(cropped),
            ok=True,
            action="crop",
            destination=str(trashed_orig),
            size=len(b"original-bytes"),
        )
    ]
    _write_action_log(receipt, logs)

    # Also write a receipt using explicit original_in_trash with no destination field.
    explicit_path = logs / "action-20261111T120000Z-11112222.json"
    explicit_path.write_text(
        json.dumps(
            {
                "action": "crop",
                "dry_run": False,
                "started_at": "2026-11-11T12:00:00+00:00",
                "success_count": 1,
                "fail_count": 0,
                "items": [
                    {
                        "path": str(cropped),
                        "ok": True,
                        "action": "crop",
                        "original_in_trash": str(trashed_orig),
                        "size": 14,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    history = list_history(logs, cache_path=tmp_path / "cache.sqlite3")
    assert history["action_count"] == 2
    for row in history["actions"]:
        assert row["action"] == "crop"
        assert row["action_label"] == "Cropped Borders"
        assert row["undoable"] is True
        assert row["restorable_count"] == 1

    trashed_orig.unlink()
    after_empty_trash = list_history(logs, cache_path=tmp_path / "cache.sqlite3")
    for row in after_empty_trash["actions"]:
        assert row["undoable"] is False
        assert row["restorable_count"] == 0
        assert "Trash" in (row["undo_blocked_reason"] or "")


def test_receipts_skip_previews_before_json_loads_and_auto_prune_previews(
    tmp_path, monkeypatch
):
    logs = tmp_path / "logs"
    logs.mkdir()
    action_file = logs / "action-20261101T000000Z-aaaaaaaa.json"
    action_file.write_text(
        json.dumps({"action": "trash", "dry_run": False, "started_at": "2026-11-01T00:00:00Z"}),
        encoding="utf-8",
    )
    for i in range(6):
        preview = logs / f"preview-2026110{i + 1}T000000Z-bbbbbbbb.json"
        preview.write_text(
            json.dumps({"action": "trash", "dry_run": True, "started_at": f"2026-11-0{i + 1}T00:00:00Z"}),
            encoding="utf-8",
        )
        os.utime(preview, (1_700_000_000 + i * 10, 1_700_000_000 + i * 10))

    parsed_files: list[str] = []
    orig_loads = json.loads

    def tracking_loads(raw, *args, **kwargs):
        parsed_files.append(raw)
        return orig_loads(raw, *args, **kwargs)

    monkeypatch.setattr(receipts.json, "loads", tracking_loads)
    assert "crop" in receipts.UNDOABLE_ACTIONS
    executed = receipts.list_receipts(logs, include_previews=False)
    assert len(executed) == 1
    assert len(parsed_files) == 1  # Only action-*.json was parsed, zero previews opened.

    removed = receipts.auto_prune_preview_receipts(logs, max_keep=2)
    assert removed == 4
    remaining_previews = sorted(p.name for p in logs.glob("preview-*.json"))
    assert remaining_previews == [
        "preview-20261105T000000Z-bbbbbbbb.json",
        "preview-20261106T000000Z-bbbbbbbb.json",
    ]
    assert action_file.exists()


def test_web_media_grid_variant_icc_profile_and_no_sync_utime(tmp_path, monkeypatch):
    assert web_media.VARIANT_MAX_SIDE == {
        "grid": 512,
        "thumb": 1600,
        "preview": 2560,
        "full": None,
    }
    assert web_media.VARIANT_QUALITY == {
        "grid": 82,
        "thumb": 85,
        "preview": 88,
        "full": 95,
    }

    icc_bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source = tmp_path / "p3-sample.jpg"
    Image.new("RGB", (1200, 800), (40, 120, 200)).save(
        source, format="JPEG", icc_profile=icc_bytes
    )

    grid_bytes = web_media.image_thumbnail_bytes(source, variant="grid")
    with Image.open(BytesIO(grid_bytes)) as thumb:
        assert max(thumb.size) <= 512
        assert thumb.info.get("icc_profile") == icc_bytes

    def fail_fsync(_fd):
        raise AssertionError("store_thumbnail must not call os.fsync")

    def fail_utime(*_args, **_kwargs):
        raise AssertionError("_stored_thumbnail must not call os.utime")

    monkeypatch.setattr(web_media.os, "fsync", fail_fsync)
    monkeypatch.setattr(web_media.os, "utime", fail_utime)
    dest = tmp_path / "thumbs" / "ab" / "cdef.jpg"
    web_media.store_thumbnail(dest, grid_bytes)
    assert web_media._stored_thumbnail(dest, "abcdef") == (dest, "abcdef")


def test_web_media_async_prune_runs_after_inflight_signal(tmp_path, monkeypatch):
    cache_dir = tmp_path / "thumbs"
    prune_started = threading.Event()
    prune_release = threading.Event()
    threads_seen: list[str] = []

    def fake_prune(*, cache_dir=None, budget=None):
        threads_seen.append(threading.current_thread().name)
        prune_started.set()
        prune_release.wait(timeout=5)
        return 0

    monkeypatch.setattr(web_media, "prune_thumbnail_cache", fake_prune)
    monkeypatch.setattr(web_media, "generate_thumbnail_bytes", lambda path, *, variant: b"jpg")
    with web_media._prune_lock:
        web_media._prune_state["writes"] = web_media.PRUNE_EVERY_N_WRITES - 1
        web_media._prune_state["last_run"] = (
            time.monotonic() - web_media.PRUNE_MIN_INTERVAL_SECONDS - 5
        )
        web_media._prune_state["running"] = False

    source = tmp_path / "photo.jpg"
    source.write_bytes(b"img")
    # cached_thumbnail must return immediately even while background prune waits on prune_release.
    result = web_media.cached_thumbnail(source, cache_dir=cache_dir)
    assert result is not None
    assert prune_started.wait(timeout=2)
    assert threads_seen == ["dedupe-thumb-prune"]
    prune_release.set()


def test_video_thumbnail_bytes_fallback_clamp_flags_and_negative_cache(tmp_path, monkeypatch):
    video = tmp_path / "live.mov"
    video.write_bytes(b"not-a-real-video")
    web_media._video_fail_cache.clear()
    monkeypatch.setattr(web_media.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    commands: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        out_path = Path(cmd[-1])
        if "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "0":
            out_path.write_bytes(b"jpeg-frame")

    monkeypatch.setattr(web_media.subprocess, "run", fake_run)
    data = web_media.video_thumbnail_bytes(video, variant="grid")
    assert data == b"jpeg-frame"
    assert len(commands) == 2
    assert commands[0][commands[0].index("-ss") + 1] == "1"
    assert commands[1][commands[1].index("-ss") + 1] == "0"
    for cmd in commands:
        assert "-threads" in cmd and cmd[cmd.index("-threads") + 1] == "1"
        assert "-an" in cmd and "-sn" in cmd and "-dn" in cmd
        vf = cmd[cmd.index("-vf") + 1]
        assert "w='min(512,iw)':h='min(512,ih)'" in vf
        assert "force_original_aspect_ratio=decrease" in vf

    # Failing video is negatively cached for TTL so subsequent calls don't respawn ffmpeg.
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"corrupt")
    fail_commands: list[list[str]] = []

    def always_empty(cmd, **_kwargs):
        fail_commands.append(cmd)

    monkeypatch.setattr(web_media.subprocess, "run", always_empty)
    assert web_media.video_thumbnail_bytes(broken) is None
    assert len(fail_commands) == 2  # -ss 1 then -ss 0
    assert web_media.video_thumbnail_bytes(broken) is None
    assert len(fail_commands) == 2  # served from negative TTL cache without spawning ffmpeg

    # When the source file changes, negative cache misses and retries.
    broken.write_bytes(b"replaced-longer-corrupt-payload")
    assert web_media.video_thumbnail_bytes(broken) is None
    assert len(fail_commands) == 4


def test_ensure_heif_registered_runs_once_per_process(monkeypatch):
    from dedupe import similar_image

    calls = 0

    def fake_register():
        nonlocal calls
        calls += 1

    monkeypatch.setattr(web_media, "_HEIF_REGISTERED", False)
    monkeypatch.setattr(similar_image, "ensure_heif_registered", fake_register)
    web_media._ensure_heif_registered()
    web_media._ensure_heif_registered()
    assert calls == 1
