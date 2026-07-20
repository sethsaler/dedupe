"""Local web API security and state-isolation tests."""

import json
import platform
import subprocess
from pathlib import Path

from dedupe.grouping import build_groups, build_no_human_groups
from dedupe.human_detection import human_detection_signature
from dedupe.models import FileRecord, MediaType, ScanResult
from dedupe.web.app import WEB_API_VERSION, create_app


def _result(tmp_path: Path) -> ScanResult:
    records = []
    for name in ("a.jpg", "b.jpg"):
        path = tmp_path / name
        path.write_bytes(b"same duplicate")
        stat = path.stat()
        records.append(
            FileRecord(
                path=str(path),
                size=stat.st_size,
                mtime=stat.st_mtime,
                media_type=MediaType.IMAGE,
                extension=".jpg",
                device=stat.st_dev,
                inode=stat.st_ino,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    return ScanResult(
        roots=[str(tmp_path)],
        files=records,
        groups=build_groups([records], []),
    )


def _non_human_result(tmp_path: Path) -> ScanResult:
    path = tmp_path / "landscape.jpg"
    path.write_bytes(b"landscape")
    stat = path.stat()
    record = FileRecord(
        path=str(path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        media_type=MediaType.IMAGE,
        extension=".jpg",
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
        human_detection_status="no_person_detected",
        human_detection_signature=human_detection_signature(),
    )
    return ScanResult(
        roots=[str(tmp_path)],
        files=[record],
        groups=build_no_human_groups([record]),
    )


def test_mutating_api_rejects_cross_origin_and_plain_text(tmp_path: Path) -> None:
    app = create_app(_result(tmp_path))
    client = app.test_client()
    token = app.config["DEDUPE_CSRF_TOKEN"]
    scan_id = client.get("/api/status").get_json()["scan_id"]
    payload = {"action": "trash", "dry_run": True, "scan_id": scan_id}

    plain = client.post(
        "/api/action",
        data=json.dumps(payload),
        content_type="text/plain",
        headers={"Origin": "https://attacker.example"},
    )
    assert plain.status_code == 415

    cross_origin = client.post(
        "/api/action",
        json=payload,
        headers={
            "Origin": "https://attacker.example",
            "X-Dedupe-Token": token,
        },
    )
    assert cross_origin.status_code == 403

    valid = client.post(
        "/api/action",
        json=payload,
        headers={"X-Dedupe-Token": token},
    )
    assert valid.status_code == 200
    assert valid.get_json()["success_count"] == 1


def test_action_endpoint_scopes_by_kinds(tmp_path: Path) -> None:
    app = create_app(_result(tmp_path))
    client = app.test_client()
    token = app.config["DEDUPE_CSRF_TOKEN"]
    scan_id = client.get("/api/status").get_json()["scan_id"]

    # Scoped away from the only (exact) group → nothing to act on.
    scoped_away = client.post(
        "/api/action",
        json={"action": "trash", "dry_run": True, "scan_id": scan_id, "kinds": "similar"},
        headers={"X-Dedupe-Token": token},
    )
    assert scoped_away.status_code == 200
    assert scoped_away.get_json()["success_count"] == 0

    # Scoped to exact → the one selected duplicate is reported.
    scoped_exact = client.post(
        "/api/action",
        json={"action": "trash", "dry_run": True, "scan_id": scan_id, "kinds": "exact"},
        headers={"X-Dedupe-Token": token},
    )
    assert scoped_exact.status_code == 200
    assert scoped_exact.get_json()["success_count"] == 1


def test_mutations_reject_stale_scan_generation(tmp_path: Path) -> None:
    app = create_app(_result(tmp_path))
    client = app.test_client()
    response = client.post(
        "/api/smart-select",
        json={"rule": "automatic", "scan_id": "old-scan"},
        headers={"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]},
    )
    assert response.status_code == 409


def test_app_instances_do_not_share_results(tmp_path: Path) -> None:
    first = create_app(_result(tmp_path))
    second = create_app()

    assert first.test_client().get("/api/status").get_json()["has_result"] is True
    assert second.test_client().get("/api/status").get_json()["has_result"] is False


def test_status_exposes_web_api_version() -> None:
    status = create_app().test_client().get("/api/status").get_json()

    assert status["web_api_version"] == WEB_API_VERSION


def test_review_ui_exposes_clear_selection_controls(tmp_path: Path) -> None:
    app = create_app(_result(tmp_path))
    html = app.test_client().get("/").get_data(as_text=True)

    assert 'id="btnSelectSuggested"' in html
    assert 'id="btnClearGroup"' in html
    assert "Apply to this group" in html
    assert "Preview trash" in html
    assert "Preview quarantine" in html
    assert "Preview isolate" in html


def test_non_human_image_can_be_deleted_and_undone(tmp_path: Path) -> None:
    result = _non_human_result(tmp_path)
    group = result.groups[0]
    original = Path(group.members[0].path)
    app = create_app(result)
    app.config["DEDUPE_RECOVERY_DIR"] = str(tmp_path / "recovery")
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    payload = {"group_id": group.id, "path": str(original), "scan_id": scan_id}

    deleted = client.post("/api/non-human/delete", json=payload, headers=headers)
    assert deleted.status_code == 200
    assert deleted.get_json()["deleted_paths"] == [str(original)]
    assert not original.exists()

    fetched = client.get(f"/api/groups/{group.id}").get_json()
    assert fetched["deleted_paths"] == [str(original)]

    undone = client.post("/api/non-human/undo", json=payload, headers=headers)
    assert undone.status_code == 200
    assert undone.get_json()["deleted_paths"] == []
    assert original.read_bytes() == b"landscape"


def test_scan_rejects_unknown_human_backend(tmp_path: Path) -> None:
    app = create_app()
    response = app.test_client().post(
        "/api/scan",
        json={"paths": [str(tmp_path)], "human_backend": "cloud-magic"},
        headers={"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]},
    )

    assert response.status_code == 400
    assert "unknown human detector" in response.get_json()["error"]


def test_macos_picker_returns_multiple_files(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="/tmp/first image.jpg\n/tmp/second.jpg\n",
            stderr="",
        )

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(subprocess, "run", fake_run)
    app = create_app()
    response = app.test_client().post(
        "/api/pick-folder",
        json={"kind": "files"},
        headers={"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]},
    )

    assert response.status_code == 200
    assert response.get_json()["paths"] == [
        str(Path("/tmp/first image.jpg").resolve()),
        str(Path("/tmp/second.jpg").resolve()),
    ]
    assert captured["command"][0] == "/usr/bin/osascript"
    assert "choose file" in captured["command"][2]
    assert "activateIgnoringOtherApps" in captured["command"][2]
    assert captured["kwargs"]["timeout"] == 300


def test_macos_picker_surfaces_native_error(monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Not authorized to display a dialog",
        ),
    )
    app = create_app()
    response = app.test_client().post(
        "/api/pick-folder",
        json={"kind": "folder"},
        headers={"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]},
    )

    assert response.status_code == 500
    assert "Not authorized" in response.get_json()["error"]
