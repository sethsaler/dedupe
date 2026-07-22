"""Local web API security and state-isolation tests."""

import json
import platform
import subprocess
import time
from pathlib import Path

from dedupe.cache import HashCache
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


def test_parallel_scan_streams_report_per_folder_and_tag_groups(tmp_path: Path) -> None:
    data = b"identical-binary-payload-for-exact-match!!!"
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    (folder_a / "a1.jpg").write_bytes(data)
    (folder_a / "a2.jpg").write_bytes(data)
    (folder_b / "b1.jpg").write_bytes(data)
    (folder_b / "b2.jpg").write_bytes(data)

    app = create_app()
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "cache.sqlite3")
    client = app.test_client()
    token = app.config["DEDUPE_CSRF_TOKEN"]

    started = client.post(
        "/api/scan",
        json={
            "paths": [str(folder_a), str(folder_b)],
            "parallel_streams": True,
            "similar": False,
            "include_videos": False,
            "use_cache": False,
        },
        headers={"X-Dedupe-Token": token},
    )
    assert started.status_code == 200

    deadline = time.monotonic() + 15
    status = client.get("/api/status").get_json()
    while status["scanning"] and time.monotonic() < deadline:
        time.sleep(0.05)
        status = client.get("/api/status").get_json()

    assert not status["scanning"]
    # Two independent streams, each reporting its own folder.
    assert len(status["streams"]) == 2
    assert all(stream["done"] for stream in status["streams"])
    assert {Path(stream["root"]).name for stream in status["streams"]} == {"a", "b"}

    groups = client.get("/api/groups?kind=exact").get_json()["groups"]
    # No cross-folder dedup: one exact group per folder, each tagged with its root.
    assert len(groups) == 2
    assert {Path(group["root"]).name for group in groups} == {"a", "b"}


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


def test_action_endpoint_combines_exact_and_similar_with_tabulated_counts(
    tmp_path: Path,
) -> None:
    records = []
    for name, contents in (
        ("exact-a.jpg", b"same"),
        ("exact-b.jpg", b"same"),
        ("similar-a.jpg", b"first"),
        ("similar-b.jpg", b"second"),
    ):
        path = tmp_path / name
        path.write_bytes(contents)
        stat = path.stat()
        records.append(FileRecord(
            path=str(path),
            size=stat.st_size,
            mtime=stat.st_mtime,
            media_type=MediaType.IMAGE,
            extension=".jpg",
        ))
    result = ScanResult(
        roots=[str(tmp_path)],
        files=records,
        groups=build_groups([[records[0], records[1]]], [[records[2], records[3]]]),
    )
    app = create_app(result)
    client = app.test_client()
    scan_id = client.get("/api/status").get_json()["scan_id"]

    response = client.post(
        "/api/action",
        json={
            "action": "trash",
            "dry_run": True,
            "scan_id": scan_id,
            "kinds": "duplicates",
        },
        headers={"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success_count"] == 2
    assert payload["selection_counts"] == {
        "exact": 1,
        "similar": 1,
        "no_humans": 0,
        "unique_total": 2,
    }


def test_action_count_assigns_exact_similar_overlap_only_once(tmp_path: Path) -> None:
    records = []
    for name, contents in (("a.jpg", b"same"), ("b.jpg", b"same"), ("c.jpg", b"other")):
        path = tmp_path / name
        path.write_bytes(contents)
        stat = path.stat()
        records.append(FileRecord(
            path=str(path),
            size=stat.st_size,
            mtime=stat.st_mtime,
            media_type=MediaType.IMAGE,
            extension=".jpg",
        ))
    groups = build_groups([[records[0], records[1]]], [[records[1], records[2]]])
    exact = next(group for group in groups if group.kind.value == "exact")
    similar = next(group for group in groups if group.kind.value == "similar")
    exact.selected_for_removal = [records[1].path]
    similar.selected_for_removal = [records[1].path]
    app = create_app(ScanResult(roots=[str(tmp_path)], files=records, groups=groups))
    client = app.test_client()
    scan_id = client.get("/api/status").get_json()["scan_id"]

    response = client.post(
        "/api/action",
        json={"action": "trash", "dry_run": True, "scan_id": scan_id, "kinds": "duplicates"},
        headers={"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]},
    )

    payload = response.get_json()
    assert payload["success_count"] == 1
    assert payload["selection_counts"] == {
        "exact": 1,
        "similar": 0,
        "no_humans": 0,
        "unique_total": 1,
    }


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
    assert 'id="memberPagination"' in html
    assert 'id="memberPaginationBottom"' in html
    assert 'class="btn ghost member-prev"' in html
    assert 'class="btn ghost member-next"' in html
    assert 'id="lbVideo"' in html
    assert 'id="lbSpeed"' in html

    script = app.test_client().get("/static/app.js").get_data(as_text=True)
    assert 'class="hover-video"' in script
    assert 'class="thumb-image ${m.media_type === "gif" ? "hover-gif"' in script
    assert 'video.muted = true' in script


def test_media_endpoint_streams_only_scanned_files_with_range_support(tmp_path: Path) -> None:
    result = _result(tmp_path)
    client = create_app(result).test_client()
    scanned = Path(result.files[0].path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not part of scan")

    response = client.get(
        "/api/media",
        query_string={"path": str(scanned)},
        headers={"Range": "bytes=0-3"},
    )
    assert response.status_code == 206
    assert response.data == b"same"
    assert response.headers["Accept-Ranges"] == "bytes"

    forbidden = client.get("/api/media", query_string={"path": str(outside)})
    assert forbidden.status_code == 403


def test_non_human_image_can_be_deleted_and_undone(tmp_path: Path) -> None:
    result = _non_human_result(tmp_path)
    group = result.groups[0]
    original = Path(group.members[0].path)
    app = create_app(result)
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


def test_remaining_non_human_images_can_be_batch_marked_as_human(tmp_path: Path) -> None:
    result = _non_human_result(tmp_path)
    deleted_record = result.files[0]
    remaining_path = tmp_path / "portrait.jpg"
    remaining_path.write_bytes(b"portrait")
    stat = remaining_path.stat()
    remaining_record = FileRecord(
        path=str(remaining_path),
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
    result.files.append(remaining_record)
    result.groups = build_no_human_groups(result.files)
    app = create_app(result)
    cache_path = tmp_path / "hashes.sqlite3"
    app.config["DEDUPE_CACHE_PATH"] = str(cache_path)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    group_id = result.groups[0].id

    deleted = client.post(
        "/api/non-human/delete",
        json={"group_id": group_id, "path": deleted_record.path, "scan_id": scan_id},
        headers=headers,
    )
    assert deleted.status_code == 200

    response = client.post(
        "/api/non-human/mark-remaining-human",
        json={"scan_id": scan_id},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json()["marked_count"] == 1
    groups = client.get("/api/groups?kind=no_humans").get_json()["groups"]
    assert len(groups) == 1
    assert groups[0]["deleted_paths"] == [deleted_record.path]
    assert [member["path"] for member in groups[0]["members"]] == [deleted_record.path]

    fresh = FileRecord(
        path=remaining_record.path,
        size=remaining_record.size,
        mtime=remaining_record.mtime,
        media_type=remaining_record.media_type,
        extension=remaining_record.extension,
        device=remaining_record.device,
        inode=remaining_record.inode,
        mtime_ns=remaining_record.mtime_ns,
    )
    cache = HashCache(cache_path)
    assert cache.hydrate([fresh]) == 1
    assert fresh.human_detection_status == "person_confirmed"
    assert fresh.human_detector == "manual_review"
    assert fresh.human_detection_signature is None
    assert cache.hydrate([deleted_record]) == 0
    cache.close()

    # Restore the trashed file so the test does not leave junk in the real Trash.
    client.post(
        "/api/non-human/undo",
        json={"group_id": group_id, "path": deleted_record.path, "scan_id": scan_id},
        headers=headers,
    )


def test_similar_group_can_be_marked_distinct(tmp_path: Path) -> None:
    result = _result(tmp_path)
    result.groups = build_groups([], [result.files])
    group = result.groups[0]
    app = create_app(result)
    cache_path = tmp_path / "hashes.sqlite3"
    app.config["DEDUPE_CACHE_PATH"] = str(cache_path)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    response = client.post(
        "/api/similar/mark-distinct",
        json={"group_id": group.id, "scan_id": scan_id},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json()["pair_count"] == 1
    assert client.get("/api/groups?kind=similar").get_json()["groups"] == []
    cache = HashCache(cache_path)
    expected_pair = tuple(sorted(record.path for record in result.files))
    assert cache.distinct_pairs(result.files) == {expected_pair}
    cache.close()


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
