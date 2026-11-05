"""History remains usable before loading any scan after an app restart."""

from pathlib import Path

from dedupe.actions import ActionItem, ActionResult, _write_action_log, apply_actions
from dedupe.cache import HashCache
from dedupe.grouping import build_groups
from dedupe.models import FileRecord, MediaType
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
