"""Local web API security and state-isolation tests."""

import json
import os
import platform
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

import dedupe.actions as actions_module
from dedupe.cache import HashCache
from dedupe.grouping import (
    build_faces_groups,
    build_groups,
    build_low_resolution_groups,
    build_no_human_groups,
    build_random_review_groups,
)
from dedupe.human_detection import human_detection_signature
from dedupe.keep_decisions import load_keep_decisions
from dedupe.models import (
    FileRecord,
    GroupKind,
    MediaType,
    ScanDiagnostics,
    ScanProgress,
    ScanResult,
    StageDiagnostics,
)
from dedupe.web import app as web_app
from dedupe.web import media as web_media
from dedupe.web.app import WEB_API_VERSION, create_app


@pytest.fixture(autouse=True)
def isolate_review_session(tmp_path: Path, monkeypatch) -> None:
    """Never let a web test read or overwrite the user's saved review."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("DEDUPE_THUMBNAIL_CACHE_DIR", str(tmp_path / "thumbs"))


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


def _faces_result(tmp_path: Path) -> ScanResult:
    path = tmp_path / "group_photo.jpg"
    path.write_bytes(b"group photo")
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
        face_count=3,
        face_detection_signature="face-count-v1|test",
    )
    return ScanResult(
        roots=[str(tmp_path)],
        files=[record],
        groups=build_faces_groups([record]),
    )


def test_status_transports_diagnostics_and_completed_elapsed(tmp_path: Path) -> None:
    result = _result(tmp_path)
    result.diagnostics = ScanDiagnostics(
        total_duration_seconds=2.5,
        cache_hits=3,
        stages={"inventory": StageDiagnostics(attempted=1, succeeded=1)},
    )

    status = create_app(result).test_client().get("/api/status").get_json()

    assert status["progress"]["elapsed_seconds"] == 2.5
    assert status["summary"]["diagnostics"] == result.diagnostics.to_dict()


def test_bulk_action_ui_separates_exact_matches_from_similars(tmp_path: Path) -> None:
    page = create_app(_result(tmp_path)).test_client().get("/").get_data(as_text=True)

    assert 'id="btnTrashExact"' in page
    assert "Delete All Selected Exact Matches" in page
    assert 'id="btnTrashSimilar"' in page
    assert "Delete All Selected Similar Matches" in page
    assert 'id="btnTrashReview"' in page
    assert "Delete All Selected Low-res + Random" in page
    assert 'id="actionScope"' not in page


def test_similar_group_payload_reports_image_and_video_fingerprint_agreement(
    tmp_path: Path,
) -> None:
    records = []
    specifications = (
        ("image-keeper.jpg", MediaType.IMAGE, {
            "phash": "0000000000000000",
            "dhash": "0000000000000000",
            "tile_phashes": "t2:" + ",".join(["0000000000000000"] * 5),
        }),
        ("image-copy.jpg", MediaType.IMAGE, {
            "phash": "0000000000000001",
            "dhash": "0000000000000000",
            "tile_phashes": "t2:" + ",".join(["0000000000000000"] * 5),
        }),
        ("video-keeper.mp4", MediaType.VIDEO, {
            "video_fingerprint": "v3:0000000000000000,0000000000000000",
        }),
        ("video-copy.mp4", MediaType.VIDEO, {
            "video_fingerprint": "v3:0000000000000001,0000000000000000",
        }),
    )
    for name, media_type, fingerprints in specifications:
        path = tmp_path / name
        path.write_bytes(b"media")
        records.append(FileRecord(
            path=str(path),
            size=5,
            mtime=1,
            media_type=media_type,
            extension=path.suffix,
            **fingerprints,
        ))
    groups = build_groups([], [[records[0], records[1]], [records[2], records[3]]])
    for group, keeper in zip(groups, (records[0], records[2]), strict=True):
        group.suggested_keep = keeper.path
    result = ScanResult(roots=[str(tmp_path)], files=records, groups=groups)

    payloads = create_app(result).test_client().get("/api/groups?kind=similar").get_json()[
        "groups"
    ]
    image_members = {member["path"]: member for member in payloads[0]["members"]}
    video_members = {member["path"]: member for member in payloads[1]["members"]}

    assert image_members[records[0].path]["similarity_percent"] == 100.0
    assert image_members[records[1].path]["similarity_percent"] == 99.8
    assert video_members[records[2].path]["similarity_percent"] == 100.0
    assert video_members[records[3].path]["similarity_percent"] == 99.2


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


def _fake_web_trash(dest_dir: Path):
    """Redirect trash into a temp directory so tests don't pollute the real Trash."""

    def _send_to_trash(src: Path, batch=None) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / src.name
        n = 1
        while target.exists():
            target = dest_dir / f"{src.stem}_{n}{src.suffix}"
            n += 1
        import shutil

        shutil.move(str(src), str(target))
        return target

    return _send_to_trash


def _trash_exact_pair(client, headers: dict, scan_id: str) -> dict:
    preview = client.post(
        "/api/action",
        json={"action": "trash", "dry_run": True, "kinds": "exact", "scan_id": scan_id},
        headers=headers,
    )
    assert preview.status_code == 200
    executed = client.post(
        "/api/action",
        json={
            "action": "trash",
            "dry_run": False,
            "kinds": "exact",
            "scan_id": scan_id,
            "preview_token": preview.get_json()["preview_token"],
        },
        headers=headers,
    )
    assert executed.status_code == 200
    return executed.get_json()


def test_action_undo_restores_an_executed_trash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        actions_module, "_send_to_trash", _fake_web_trash(tmp_path / "trash")
    )
    app = create_app(_result(tmp_path))
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    executed = _trash_exact_pair(client, headers, scan_id)
    assert executed["success_count"] == 1
    trashed_path = next(item["path"] for item in executed["items"] if item["ok"])
    assert not Path(trashed_path).exists()

    preview = client.post(
        "/api/action/undo",
        json={"receipts": [executed["log_path"]], "dry_run": True, "scan_id": scan_id},
        headers=headers,
    )
    assert preview.status_code == 200
    assert preview.get_json()["success_count"] == 1

    restored = client.post(
        "/api/action/undo",
        json={
            "receipts": [executed["log_path"]],
            "dry_run": False,
            "scan_id": scan_id,
            "preview_token": preview.get_json()["preview_token"],
        },
        headers=headers,
    )
    assert restored.status_code == 200
    assert restored.get_json()["success_count"] == 1
    assert Path(trashed_path).exists()
    # The review is not re-populated: the moved member stays out of the group.
    groups = client.get("/api/groups?kind=exact").get_json()["groups"]
    assert all(
        trashed_path not in {member["path"] for member in group["members"]}
        for group in groups
    )


def test_action_undo_refuses_a_blocked_restore(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        actions_module, "_send_to_trash", _fake_web_trash(tmp_path / "trash")
    )
    app = create_app(_result(tmp_path))
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    executed = _trash_exact_pair(client, headers, scan_id)
    item = next(item for item in executed["items"] if item["ok"])
    # Something new now occupies the original path.
    Path(item["path"]).write_bytes(b"blocking")

    preview = client.post(
        "/api/action/undo",
        json={"receipts": [executed["log_path"]], "dry_run": True, "scan_id": scan_id},
        headers=headers,
    )
    assert preview.get_json()["fail_count"] == 1

    refused = client.post(
        "/api/action/undo",
        json={
            "receipts": [executed["log_path"]],
            "dry_run": False,
            "scan_id": scan_id,
            "preview_token": preview.get_json()["preview_token"],
        },
        headers=headers,
    )
    assert refused.status_code == 400
    assert "nothing was restored" in refused.get_json()["error"]
    assert Path(item["destination"]).exists()


def test_action_undo_validates_the_request(tmp_path: Path) -> None:
    app = create_app(_result(tmp_path))
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    missing = client.post(
        "/api/action/undo", json={"scan_id": scan_id}, headers=headers
    )
    assert missing.status_code == 400

    unknown = client.post(
        "/api/action/undo",
        json={"receipts": ["no-such-receipt"], "scan_id": scan_id},
        headers=headers,
    )
    assert unknown.status_code == 404

    stale = client.post(
        "/api/action/undo",
        json={"receipts": ["x"], "scan_id": "stale"},
        headers=headers,
    )
    assert stale.status_code == 409


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


def test_scan_endpoint_forwards_low_resolution_media_types(
    tmp_path: Path, monkeypatch
) -> None:
    captured = {}

    def fake_run_scan(paths, **kwargs):
        captured.update(kwargs)
        return ScanResult(roots=[str(path) for path in paths], files=[], groups=[])

    monkeypatch.setattr(web_app, "run_scan", fake_run_scan)
    app = create_app()
    client = app.test_client()
    response = client.post(
        "/api/scan",
        json={
            "paths": [str(tmp_path)],
            "parallel_streams": False,
            "low_resolution_images": False,
            "low_resolution_gifs": True,
            "low_resolution_videos": False,
            "low_resolution_image_max_pixels": 500_000,
            "low_resolution_gif_max_pixels": 1_500_000,
            "low_resolution_video_max_pixels": 3_000_000,
        },
        headers={"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]},
    )

    assert response.status_code == 200
    _wait_idle(client)
    assert captured["low_resolution_images"] is False
    assert captured["low_resolution_gifs"] is True
    assert captured["low_resolution_videos"] is False
    assert captured["low_resolution_image_max_pixels"] == 500_000
    assert captured["low_resolution_gif_max_pixels"] == 1_500_000
    assert captured["low_resolution_video_max_pixels"] == 3_000_000


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


def test_destructive_action_requires_matching_one_use_preview_token(tmp_path: Path) -> None:
    result = _result(tmp_path)
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    quarantine = tmp_path / "quarantine"
    base = {
        "action": "quarantine",
        "dry_run": False,
        "scan_id": scan_id,
        "quarantine_dir": str(quarantine),
        "kinds": "exact",
    }

    assert client.post("/api/action", json=base, headers=headers).status_code == 409

    preview = client.post(
        "/api/action", json={**base, "dry_run": True}, headers=headers
    ).get_json()
    mismatch = client.post(
        "/api/action",
        json={**base, "kinds": "all", "preview_token": preview["preview_token"]},
        headers=headers,
    )
    assert mismatch.status_code == 409

    preview = client.post(
        "/api/action", json={**base, "dry_run": True}, headers=headers
    ).get_json()
    token = preview["preview_token"]
    executed = client.post(
        "/api/action", json={**base, "preview_token": token}, headers=headers
    )
    assert executed.status_code == 200
    assert executed.get_json()["success_count"] == 1
    assert len(list(quarantine.iterdir())) == 1
    assert client.post(
        "/api/action", json={**base, "preview_token": token}, headers=headers
    ).status_code == 409


def test_selection_change_after_preview_is_rejected(tmp_path: Path) -> None:
    result = _result(tmp_path)
    group = result.groups[0]
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    preview = client.post(
        "/api/action",
        json={"action": "trash", "dry_run": True, "scan_id": scan_id},
        headers=headers,
    ).get_json()
    changed = client.post(
        "/api/selection",
        json={"group_id": group.id, "selected": [], "scan_id": scan_id},
        headers=headers,
    )
    assert changed.status_code == 200
    execution = client.post(
        "/api/action",
        json={
            "action": "trash",
            "dry_run": False,
            "scan_id": scan_id,
            "preview_token": preview["preview_token"],
        },
        headers=headers,
    )
    assert execution.status_code == 409
    assert all(Path(record.path).exists() for record in result.files)


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


def test_app_instances_resume_saved_review_without_sharing_runtime_state(
    tmp_path: Path,
) -> None:
    first = create_app(_result(tmp_path))
    second = create_app()

    first_status = first.test_client().get("/api/status").get_json()
    second_status = second.test_client().get("/api/status").get_json()

    assert first_status["has_result"] is True
    assert second_status["has_result"] is True
    assert first_status["scan_id"] != second_status["scan_id"]
    assert second_status["progress"]["message"] == "Resumed saved review"


def test_status_exposes_web_api_version() -> None:
    status = create_app().test_client().get("/api/status").get_json()

    assert status["web_api_version"] == WEB_API_VERSION


def test_review_ui_exposes_clear_selection_controls(tmp_path: Path) -> None:
    app = create_app(_result(tmp_path))
    html = app.test_client().get("/").get_data(as_text=True)

    assert 'id="btnSelectSuggested"' in html
    assert 'id="btnClearGroup"' in html
    assert "Apply to this group" in html
    assert "Delete All Selected Exact Matches" in html
    assert "Delete All Selected Similar Matches" in html
    assert 'id="actionScope"' not in html
    assert "Preview quarantine" not in html
    assert "Preview isolate" not in html
    assert 'id="memberPagination"' in html
    assert 'id="memberPaginationBottom"' in html
    assert 'class="btn ghost member-prev"' in html
    assert 'class="btn ghost member-next"' in html
    assert 'id="memberSort"' in html
    assert 'value="has_male"' in html
    assert 'id="lbVideo"' in html
    assert 'id="lbSpeed"' in html
    assert 'id="scanQuality"' in html
    assert 'id="resultSearch"' in html
    assert 'id="similarityPreset"' in html
    assert 'id="btnDiscardSession"' in html
    assert 'id="nonHumanBanner"' in html
    assert 'id="candidateReviewBanner"' in html
    assert 'id="scanCollapse"' in html
    assert 'aria-controls="scanMain"' in html
    assert "<kbd>↑</kbd> Back" in html
    assert 'id="optLowResolution"' in html
    assert 'id="optLowResolutionImages"' in html
    assert 'id="optLowResolutionGifs"' in html
    assert 'id="optLowResolutionVideos"' in html
    assert 'id="lowResolutionImageMaxMp"' in html
    assert 'id="lowResolutionGifMaxMp"' in html
    assert 'id="lowResolutionVideoMaxMp"' in html
    assert 'id="optRandomReview"' in html
    assert 'data-kind="low_resolution"' in html
    assert 'data-kind="random_review"' in html
    assert 'data-kind="faces"' in html
    assert 'id="countFaces"' in html
    assert "←</kbd> Delete" in html
    assert "Keep <kbd>→" in html
    assert 'id="lbOpacity"' in html
    assert 'id="lbFlicker"' in html

    # The frontend is split into ES modules; assert across all of them.
    static_dir = Path(web_app.__file__).parent / "static"
    script = "\n".join(
        path.read_text() for path in sorted(static_dir.glob("*.js"))
    )
    assert 'class="hover-video"' in script
    assert 'class="thumb-image ${m.media_type === "gif" ? "hover-gif"' in script
    assert 'data-preview-width="${mediaWidth}"' in script
    assert 'setPreviewAspectRatio(image.closest(".thumb-wrap")' in script
    assert 'video.muted = true' in script
    assert 'method: "DELETE"' in script
    assert 'dry_run: true' in script
    assert 'low_resolution_images: $("optLowResolutionImages").checked' in script
    assert 'low_resolution_gifs: $("optLowResolutionGifs").checked' in script
    assert 'low_resolution_videos: $("optLowResolutionVideos").checked' in script
    assert "low_resolution_image_max_pixels: lowResolutionBounds.images" in script
    assert "low_resolution_gif_max_pixels: lowResolutionBounds.gifs" in script
    assert "low_resolution_video_max_pixels: lowResolutionBounds.videos" in script
    assert 'await reviewCandidate(current, member.path, e.key === "ArrowLeft")' in script
    assert script.count('scrollIntoView({ block: "start", behavior: "instant" })') == 1
    assert 'scrollIntoView({ block: "start", behavior: "smooth" })' not in script
    # Decision reviews (← Delete / → Keep) re-center the candidate's media on
    # every render so the full image stays on screen while arrowing through.
    assert 'scrollIntoView({ block: "center", behavior: "instant" })' in script
    # In a decision review, ↑ / ↓ step between candidates without deciding.
    assert 'e.key === "ArrowUp" && isDecisionReview(currentGroup())' in script
    assert 'e.key === "ArrowDown" && isDecisionReview(currentGroup())' in script
    # The scan setup form collapses into a slim bar once a scan starts (or
    # when results are already loaded) so results get the screen.
    assert "collapseScanPanel" in script

    stylesheet = app.test_client().get("/static/app.css").get_data(as_text=True)
    assert "aspect-ratio: var(--preview-aspect-ratio);" in stylesheet
    assert "aspect-ratio: 16 / 10;" not in stylesheet
    assert ".triage-card .thumb-wrap" in stylesheet
    assert ".scan-panel.collapsed .scan-main" in stylesheet
    assert "aspect-ratio: 1 / 1;" in stylesheet
    # Triage cards must not inherit content-visibility: auto, or off-screen
    # square previews lose their layout box and the grid reflows on scroll.
    triage_card_rule = stylesheet.split(".triage-card {", 1)[1].split("}", 1)[0]
    assert "content-visibility: visible;" in triage_card_rule


def test_independent_review_decision_is_persisted_and_actionable(tmp_path: Path) -> None:
    result = _result(tmp_path)
    duplicate = result.groups[0]
    for record in result.files:
        record.width = 320
        record.height = 240
    low_resolution = build_low_resolution_groups(result.files)[0]
    random_review = build_random_review_groups(result.files, count=2)[0]
    result.groups = [duplicate, low_resolution, random_review]
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    path = duplicate.selected_for_removal[0]

    decision = client.post(
        "/api/selection",
        json={
            "group_id": low_resolution.id,
            "selected": [path],
            "reviewed": [path],
            "decision_path": path,
            "decision_remove": True,
            "scan_id": scan_id,
        },
        headers=headers,
    )

    assert decision.status_code == 200
    assert decision.get_json()["selected_for_removal"] == [path]
    assert decision.get_json()["reviewed_paths"] == [path]
    assert path in random_review.selected_for_removal
    assert path in random_review.reviewed_paths

    keep = client.post(
        "/api/selection",
        json={
            "group_id": random_review.id,
            "decision_path": path,
            "decision_remove": False,
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert keep.status_code == 200
    assert path not in low_resolution.selected_for_removal
    assert path not in random_review.selected_for_removal
    assert path not in duplicate.selected_for_removal

    delete_again = client.post(
        "/api/selection",
        json={
            "group_id": low_resolution.id,
            "decision_path": path,
            "decision_remove": True,
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert delete_again.status_code == 200
    assert path in low_resolution.selected_for_removal
    assert path in random_review.selected_for_removal

    preview = client.post(
        "/api/action",
        json={
            "action": "trash",
            "dry_run": True,
            "kinds": "review_suggestions",
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert preview.status_code == 200
    assert preview.get_json()["success_count"] == 1
    assert preview.get_json()["selection_counts"]["low_resolution"] == 1


def test_faces_review_group_is_served_and_actionable(tmp_path: Path) -> None:
    result = _result(tmp_path)
    duplicate = result.groups[0]
    for index, record in enumerate(result.files):
        record.face_count = index + 1
        record.face_detection_signature = "face-count-v1|test"
    faces_group = build_faces_groups(result.files)[0]
    result.groups = [*result.groups, faces_group]
    result.recompute_stats()
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    groups = client.get("/api/groups?kind=faces").get_json()["groups"]
    assert len(groups) == 1
    assert groups[0]["kind"] == "faces"
    assert groups[0]["policy"] == "independent_candidates"
    assert groups[0]["member_count"] == 2

    # Never select the duplicate keeper; the server would drop it anyway.
    path = next(
        member.path for member in faces_group.members if member.path != duplicate.suggested_keep
    )
    select = client.post(
        "/api/selection",
        json={
            "group_id": faces_group.id,
            "selected": [path],
            "reviewed": [path],
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert select.status_code == 200
    assert select.get_json()["selected_for_removal"] == [path]

    preview = client.post(
        "/api/action",
        json={"action": "trash", "dry_run": True, "kinds": "faces", "scan_id": scan_id},
        headers=headers,
    )
    assert preview.status_code == 200
    payload = preview.get_json()
    assert payload["success_count"] == 1
    assert payload["selection_counts"]["faces"] == 1


@pytest.mark.parametrize("review_kind", ["low_resolution", "random_review"])
def test_trash_routes_review_decisions_to_dedicated_quarantine(
    tmp_path: Path, monkeypatch, review_kind: str
) -> None:
    result = _result(tmp_path)
    review_path = tmp_path / f"{review_kind}.jpg"
    review_path.write_bytes(b"review candidate")
    stat = review_path.stat()
    candidate = FileRecord(
        path=str(review_path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        media_type=MediaType.IMAGE,
        extension=".jpg",
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
        width=320,
        height=240,
    )
    if review_kind == "low_resolution":
        review_group = build_low_resolution_groups([candidate])[0]
    else:
        review_group = build_random_review_groups([candidate], count=1)[0]
    review_group.selected_for_removal = [candidate.path]
    review_group.reviewed_paths = [candidate.path]
    result.files.append(candidate)
    result.groups.append(review_group)

    fake_trash = tmp_path / "fake-trash"

    def send_to_fake_trash(path: Path, _batch) -> Path:
        fake_trash.mkdir(exist_ok=True)
        destination = fake_trash / path.name
        path.replace(destination)
        return destination

    monkeypatch.setattr(actions_module, "_send_to_trash", send_to_fake_trash)
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    request_payload = {
        "action": "trash",
        "dry_run": True,
        "kinds": "all",
        "scan_id": scan_id,
    }

    preview = client.post("/api/action", json=request_payload, headers=headers)

    assert preview.status_code == 200
    preview_payload = preview.get_json()
    quarantine = tmp_path / "_Dedupe Quarantine"
    assert preview_payload["success_count"] == 2
    assert preview_payload["review_quarantine_count"] == 1
    assert preview_payload["review_quarantine_dir"] == str(quarantine)
    review_item = next(item for item in preview_payload["items"] if item["path"] == candidate.path)
    assert review_item["action"] == "quarantine"
    assert Path(review_item["destination"]).parent == quarantine

    executed = client.post(
        "/api/action",
        json={
            **request_payload,
            "dry_run": False,
            "preview_token": preview_payload["preview_token"],
        },
        headers=headers,
    )

    assert executed.status_code == 200
    executed_payload = executed.get_json()
    assert executed_payload["review_quarantine_count"] == 1
    assert (quarantine / review_path.name).is_file()
    assert len(list(fake_trash.iterdir())) == 1
    assert len(executed_payload["log_paths"]) == 2


def test_low_resolution_keep_decisions_persist_durably(tmp_path: Path) -> None:
    result = _result(tmp_path)
    for record in result.files:
        record.width = 320
        record.height = 240
    low_resolution = build_low_resolution_groups(result.files)[0]
    result.groups = [low_resolution]
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    path = low_resolution.members[0].path

    keep = client.post(
        "/api/selection",
        json={
            "group_id": low_resolution.id,
            "decision_path": path,
            "decision_remove": False,
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert keep.status_code == 200
    assert path in load_keep_decisions()

    stage_removal = client.post(
        "/api/selection",
        json={
            "group_id": low_resolution.id,
            "decision_path": path,
            "decision_remove": True,
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert stage_removal.status_code == 200
    assert path not in load_keep_decisions()

    # The checkbox flow (reviewed + unselected) also records a durable keep.
    checkbox_keep = client.post(
        "/api/selection",
        json={
            "group_id": low_resolution.id,
            "selected": [],
            "reviewed": [path],
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert checkbox_keep.status_code == 200
    assert path in load_keep_decisions()


def test_independent_review_rejects_invalid_explicit_decisions(tmp_path: Path) -> None:
    result = _result(tmp_path)
    random_review = build_random_review_groups(result.files, count=2)[0]
    result.groups = [random_review]
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    unknown_path = client.post(
        "/api/selection",
        json={
            "group_id": random_review.id,
            "decision_path": str(tmp_path / "unknown.jpg"),
            "decision_remove": True,
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert unknown_path.status_code == 400

    invalid_decision = client.post(
        "/api/selection",
        json={
            "group_id": random_review.id,
            "decision_path": random_review.members[0].path,
            "decision_remove": "delete",
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert invalid_decision.status_code == 400


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

    preview = client.post("/api/review-candidate/delete", json=payload, headers=headers)
    assert preview.status_code == 200
    assert preview.get_json()["success_count"] == 1
    assert original.exists()

    deleted = client.post(
        "/api/review-candidate/delete",
        json={**payload, "dry_run": False},
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["deleted_paths"] == [str(original)]
    assert not original.exists()

    fetched = client.get(f"/api/groups/{group.id}").get_json()
    assert fetched["deleted_paths"] == [str(original)]

    undone = client.post("/api/review-candidate/undo", json=payload, headers=headers)
    assert undone.status_code == 200
    assert undone.get_json()["deleted_paths"] == []
    assert original.read_bytes() == b"landscape"

    deleted_again = client.post(
        "/api/review-candidate/delete",
        json={**payload, "dry_run": False},
        headers=headers,
    )
    assert deleted_again.status_code == 200
    assert not original.exists()
    client.post("/api/review-candidate/undo", json=payload, headers=headers)


def test_immediate_review_delete_refuses_a_changed_file(tmp_path: Path) -> None:
    result = _non_human_result(tmp_path)
    group = result.groups[0]
    original = Path(group.members[0].path)
    original.write_bytes(b"changed after the scan recorded this file")
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    deleted = client.post(
        "/api/review-candidate/delete",
        json={
            "group_id": group.id,
            "path": str(original),
            "scan_id": scan_id,
            "dry_run": False,
        },
        headers=headers,
    )
    assert deleted.status_code == 400
    assert original.exists()


def test_faces_candidate_can_be_deleted_and_undone(tmp_path: Path) -> None:
    result = _faces_result(tmp_path)
    group = result.groups[0]
    original = Path(group.members[0].path)
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    payload = {"group_id": group.id, "path": str(original), "scan_id": scan_id}

    preview = client.post("/api/review-candidate/delete", json=payload, headers=headers)
    assert preview.status_code == 200
    assert preview.get_json()["success_count"] == 1
    assert original.exists()

    deleted = client.post(
        "/api/review-candidate/delete",
        json={**payload, "dry_run": False, "preview_token": preview.get_json()["preview_token"]},
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["deleted_paths"] == [str(original)]
    assert not original.exists()

    fetched = client.get(f"/api/groups/{group.id}").get_json()
    assert fetched["deleted_paths"] == [str(original)]

    undone = client.post("/api/review-candidate/undo", json=payload, headers=headers)
    assert undone.status_code == 200
    assert undone.get_json()["deleted_paths"] == []
    assert original.read_bytes() == b"group photo"


def test_all_files_group_is_served_per_scanned_root(tmp_path: Path) -> None:
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root b"
    root_a.mkdir()
    root_b.mkdir()
    records = []
    for root, name in ((root_a, "one.jpg"), (root_a, "two.jpg"), (root_b, "three.jpg")):
        path = root / name
        path.write_bytes(b"plain file, no review category")
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
    # A result with no detector groups at all still gets the browse view.
    result = ScanResult(
        roots=[str(root_a), str(root_b)], files=records, groups=[]
    )
    client = create_app(result).test_client()

    served = client.get("/api/groups", query_string={"kind": "all_files"}).get_json()
    assert served["total"] == 2
    by_root = {group["root"]: group for group in served["groups"]}
    assert set(by_root) == {str(root_a), str(root_b)}
    assert [m["path"] for m in by_root[str(root_a)]["members"]] == [
        str(root_a / "one.jpg"),
        str(root_a / "two.jpg"),
    ]
    assert [m["path"] for m in by_root[str(root_b)]["members"]] == [str(root_b / "three.jpg")]
    assert all(group["policy"] == "independent_candidates" for group in served["groups"])
    # Other kind filters are unaffected by the browse groups.
    assert client.get("/api/groups", query_string={"kind": "exact"}).get_json()["total"] == 0


def test_all_files_candidate_can_be_deleted_and_undone(tmp_path: Path) -> None:
    original = tmp_path / "misc" / "notes-photo.jpg"
    original.parent.mkdir()
    original.write_bytes(b"uncategorized but unwanted")
    stat = original.stat()
    record = FileRecord(
        path=str(original),
        size=stat.st_size,
        mtime=stat.st_mtime,
        media_type=MediaType.IMAGE,
        extension=".jpg",
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
    )
    result = ScanResult(roots=[str(tmp_path)], files=[record], groups=[])
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    group = client.get("/api/groups", query_string={"kind": "all_files"}).get_json()["groups"][0]
    payload = {"group_id": group["id"], "path": str(original), "scan_id": scan_id}

    preview = client.post("/api/review-candidate/delete", json=payload, headers=headers)
    assert preview.status_code == 200
    assert preview.get_json()["success_count"] == 1
    assert original.exists()

    deleted = client.post(
        "/api/review-candidate/delete",
        json={**payload, "dry_run": False},
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["deleted_paths"] == [str(original)]
    assert not original.exists()

    fetched = client.get(f"/api/groups/{group['id']}").get_json()
    assert fetched["deleted_paths"] == [str(original)]

    undone = client.post("/api/review-candidate/undo", json=payload, headers=headers)
    assert undone.status_code == 200
    assert undone.get_json()["deleted_paths"] == []
    assert original.read_bytes() == b"uncategorized but unwanted"


def test_all_files_delete_refuses_a_changed_file(tmp_path: Path) -> None:
    original = tmp_path / "doc-scan.jpg"
    original.write_bytes(b"scanned bytes")
    stat = original.stat()
    record = FileRecord(
        path=str(original),
        size=stat.st_size,
        mtime=stat.st_mtime,
        media_type=MediaType.IMAGE,
        extension=".jpg",
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
    )
    result = ScanResult(roots=[str(tmp_path)], files=[record], groups=[])
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    group = client.get("/api/groups", query_string={"kind": "all_files"}).get_json()["groups"][0]
    original.write_bytes(b"changed after the scan recorded this file")

    deleted = client.post(
        "/api/review-candidate/delete",
        json={
            "group_id": group["id"],
            "path": str(original),
            "scan_id": scan_id,
            "dry_run": False,
        },
        headers=headers,
    )
    assert deleted.status_code == 400
    assert original.exists()


def test_one_click_trash_explains_a_keep_veto_from_another_review(tmp_path: Path) -> None:
    original = tmp_path / "small.jpg"
    original.write_bytes(b"kept in low-res, trash attempted from Files")
    stat = original.stat()
    record = FileRecord(
        path=str(original),
        size=stat.st_size,
        mtime=stat.st_mtime,
        media_type=MediaType.IMAGE,
        extension=".jpg",
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
        width=100,
        height=100,
    )
    result = ScanResult(
        roots=[str(tmp_path)],
        files=[record],
        groups=build_low_resolution_groups([record]),
    )
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    # Keep the file in the Low-res review (reviewed, not selected).
    low_res = client.get(
        "/api/groups", query_string={"kind": "low_resolution"}
    ).get_json()["groups"][0]
    kept = client.post(
        "/api/selection",
        json={
            "group_id": low_res["id"],
            "decision_path": str(original),
            "decision_remove": False,
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert kept.status_code == 200

    browse = client.get("/api/groups", query_string={"kind": "all_files"}).get_json()[
        "groups"
    ][0]
    deleted = client.post(
        "/api/review-candidate/delete",
        json={
            "group_id": browse["id"],
            "path": str(original),
            "scan_id": scan_id,
            "dry_run": False,
        },
        headers=headers,
    )
    assert deleted.status_code == 400
    assert "Kept in the Low-res review" in deleted.get_json()["error"]
    assert original.exists()

    # Revising the Keep to a Delete lifts the veto.
    client.post(
        "/api/selection",
        json={
            "group_id": low_res["id"],
            "decision_path": str(original),
            "decision_remove": True,
            "scan_id": scan_id,
        },
        headers=headers,
    )
    deleted = client.post(
        "/api/review-candidate/delete",
        json={
            "group_id": browse["id"],
            "path": str(original),
            "scan_id": scan_id,
            "dry_run": False,
        },
        headers=headers,
    )
    assert deleted.status_code == 200
    assert not original.exists()


def test_candidate_trash_undo_survives_a_restart(tmp_path: Path) -> None:
    result = _faces_result(tmp_path)
    group = result.groups[0]
    original = Path(group.members[0].path)
    review_path = tmp_path / "review.json"
    app = create_app(result, review_session_path=review_path)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    payload = {"group_id": group.id, "path": str(original), "scan_id": scan_id}

    preview = client.post("/api/review-candidate/delete", json=payload, headers=headers)
    deleted = client.post(
        "/api/review-candidate/delete",
        json={**payload, "dry_run": False, "preview_token": preview.get_json()["preview_token"]},
        headers=headers,
    )
    assert deleted.status_code == 200
    assert not original.exists()

    # "Restart": a fresh app resumes from the saved session with no initial result.
    restarted = create_app(review_session_path=review_path)
    client2 = restarted.test_client()
    headers2 = {"X-Dedupe-Token": restarted.config["DEDUPE_CSRF_TOKEN"]}
    scan_id2 = client2.get("/api/status").get_json()["scan_id"]

    fetched = client2.get(f"/api/groups/{group.id}").get_json()
    assert fetched["deleted_paths"] == [str(original)]

    undone = client2.post(
        "/api/review-candidate/undo",
        json={**payload, "scan_id": scan_id2},
        headers=headers2,
    )
    assert undone.status_code == 200
    assert undone.get_json()["deleted_paths"] == []
    assert original.exists()


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

    preview = client.post(
        "/api/review-candidate/delete",
        json={"group_id": group_id, "path": deleted_record.path, "scan_id": scan_id},
        headers=headers,
    )
    deleted = client.post(
        "/api/review-candidate/delete",
        json={
            "group_id": group_id,
            "path": deleted_record.path,
            "scan_id": scan_id,
            "dry_run": False,
            "preview_token": preview.get_json()["preview_token"],
        },
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
        "/api/review-candidate/undo",
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


@pytest.mark.parametrize("failure", ["exception", "invalid-roots", "cancelled"])
def test_unsuccessful_scan_restores_previous_result_and_session(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    prior = _result(tmp_path)
    review_path = tmp_path / "review.json"
    app = create_app(prior, review_session_path=review_path)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    before = client.get("/api/status").get_json()

    def failed_scan(paths, **kwargs):
        if failure == "cancelled":
            deadline = time.monotonic() + 2
            while not kwargs["cancelled"]() and time.monotonic() < deadline:
                time.sleep(0.005)
            raise InterruptedError("scan cancelled")
        if failure == "exception":
            raise RuntimeError("scanner failed")
        return ScanResult(roots=[], files=[], groups=[], errors=["no valid roots"])

    monkeypatch.setattr("dedupe.web.app.run_scan", failed_scan)
    started = client.post(
        "/api/scan", json={"paths": [str(tmp_path / "new-root")]}, headers=headers
    ).get_json()
    if failure == "cancelled":
        response = client.post(
            "/api/scan/cancel",
            json={"scan_id": started["scan_id"]},
            headers=headers,
        )
        assert response.status_code == 200

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        after = client.get("/api/status").get_json()
        if not after["scanning"]:
            break
        time.sleep(0.01)
    assert after["has_result"] is True
    # The exact group plus the per-root All-Files browse group added at boot.
    assert after["summary"]["group_count"] == 2
    assert after["scan_id"] not in {before["scan_id"], started["scan_id"]}
    assert after["groups_version"] > before["groups_version"]
    assert after["review_session"]["saved_at"] == before["review_session"]["saved_at"]
    assert client.get("/api/groups").get_json()["groups"][0]["id"] == prior.groups[0].id


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


def _two_group_result(tmp_path: Path) -> ScanResult:
    records = []
    for name, contents in (
        ("a.jpg", b"same1"),
        ("b.jpg", b"same1"),
        ("c.jpg", b"same2"),
        ("d.jpg", b"same2"),
    ):
        path = tmp_path / name
        path.write_bytes(contents)
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
    groups = build_groups([records[:2], records[2:]], [])
    groups[0].suggested_keep = records[0].path
    groups[0].selected_for_removal = [records[1].path]
    groups[1].suggested_keep = records[2].path
    groups[1].selected_for_removal = [records[3].path]
    return ScanResult(roots=[str(tmp_path)], files=records, groups=groups)


def test_action_rejects_stale_preview_token_with_auto_retry_message(tmp_path: Path) -> None:
    """A non-dry-run /api/action call with a stale/bogus preview token returns 409."""
    result = _result(tmp_path)
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    response = client.post(
        "/api/action",
        json={
            "action": "trash",
            "dry_run": False,
            "scan_id": scan_id,
            "preview_token": "stale-or-bogus-token",
        },
        headers=headers,
    )

    assert response.status_code == 409
    data = response.get_json()
    assert data["preview_stale"] is True
    assert data["preview_stale_reason"] == "missing"
    assert "preview again" in data["error"]
    assert all(Path(record.path).exists() for record in result.files)


def test_expired_preview_token_reports_expiry_and_refuses_to_execute(
    tmp_path: Path, monkeypatch
) -> None:
    """An expired token never executes; the client is told to confirm a fresh preview."""
    result = _result(tmp_path)
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    quarantine = tmp_path / "quarantine"
    base = {
        "action": "quarantine",
        "dry_run": False,
        "scan_id": scan_id,
        "quarantine_dir": str(quarantine),
        "kinds": "exact",
    }

    fresh = client.post("/api/action", json={**base, "dry_run": True}, headers=headers)
    assert fresh.get_json()["preview_expires_in"] == web_app.PREVIEW_TOKEN_TTL_SECONDS

    # Issue a token that is already past its lifetime instead of waiting ten minutes.
    monkeypatch.setattr(web_app, "PREVIEW_TOKEN_TTL_SECONDS", -1)
    payload = client.post(
        "/api/action", json={**base, "dry_run": True}, headers=headers
    ).get_json()
    expired = client.post(
        "/api/action", json={**base, "preview_token": payload["preview_token"]}, headers=headers
    )

    assert expired.status_code == 409
    body = expired.get_json()
    assert body["preview_stale_reason"] == "expired"
    assert "expired" in body["error"]
    assert not quarantine.exists()

    # Re-previewing issues a usable token, so the user only re-confirms the numbers.
    monkeypatch.undo()
    refreshed = client.post(
        "/api/action", json={**base, "dry_run": True}, headers=headers
    ).get_json()
    assert refreshed["preview_token"] != payload["preview_token"]
    executed = client.post(
        "/api/action", json={**base, "preview_token": refreshed["preview_token"]}, headers=headers
    )
    assert executed.status_code == 200
    assert executed.get_json()["success_count"] == 1
    assert len(list(quarantine.iterdir())) == 1


def test_bulk_selection_keeps_one_member_of_every_duplicate_group(tmp_path: Path) -> None:
    result = _two_group_result(tmp_path)
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    response = client.post(
        "/api/selection/bulk",
        json={"operation": "select_all", "scan_id": scan_id},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.get_json()
    # Scope is every group, including the per-root All-Files browse group.
    assert body["group_count"] == 3
    assert body["selected_count"] == 2
    for group in result.groups:
        if group.kind == GroupKind.ALL_FILES:
            # Browse groups are excluded from bulk selection entirely.
            assert not group.selected_for_removal
            continue
        assert group.suggested_keep not in group.selected_for_removal
        assert len(group.selected_for_removal) == len(group.members) - 1

    cleared = client.post(
        "/api/selection/bulk",
        json={"operation": "select_none", "scan_id": scan_id},
        headers=headers,
    ).get_json()
    assert cleared["selected_count"] == 0
    assert all(not group.selected_for_removal for group in result.groups)

    inverted = client.post(
        "/api/selection/bulk",
        json={"operation": "invert", "scan_id": scan_id},
        headers=headers,
    ).get_json()
    assert inverted["selected_count"] == 2
    assert all(
        group.suggested_keep not in group.selected_for_removal
        for group in result.groups
        if group.kind != GroupKind.ALL_FILES
    )


def test_bulk_selection_scopes_to_requested_groups_and_criteria(tmp_path: Path) -> None:
    result = _two_group_result(tmp_path)
    first, second = result.groups
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    client.post(
        "/api/selection/bulk",
        json={"operation": "select_none", "scan_id": scan_id},
        headers=headers,
    )
    scoped = client.post(
        "/api/selection/bulk",
        json={
            "operation": "select_all",
            "group_ids": [first.id],
            "scan_id": scan_id,
        },
        headers=headers,
    )

    assert scoped.status_code == 200
    assert scoped.get_json()["group_count"] == 1
    assert first.selected_for_removal
    assert second.selected_for_removal == []

    # Every member is the same size here, so "smaller than the keeper" selects nothing.
    criteria = client.post(
        "/api/selection/bulk",
        json={
            "operation": "criteria",
            "scan_id": scan_id,
            "criteria": {"smaller_than_keeper": True},
        },
        headers=headers,
    )
    assert criteria.status_code == 200
    assert criteria.get_json()["selected_count"] == 0

    by_size = client.post(
        "/api/selection/bulk",
        json={"operation": "criteria", "scan_id": scan_id, "criteria": {"min_size": 1}},
        headers=headers,
    ).get_json()
    assert by_size["selected_count"] == 2

    rejected = client.post(
        "/api/selection/bulk",
        json={"operation": "delete_everything", "scan_id": scan_id},
        headers=headers,
    )
    assert rejected.status_code == 400
    bad_criteria = client.post(
        "/api/selection/bulk",
        json={"operation": "criteria", "scan_id": scan_id, "criteria": {"min_size": "huge"}},
        headers=headers,
    )
    assert bad_criteria.status_code == 400


def test_bulk_selection_marks_non_human_picks_reviewed(tmp_path: Path) -> None:
    result = _non_human_result(tmp_path)
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    response = client.post(
        "/api/selection/bulk",
        json={"operation": "select_all", "scan_id": scan_id},
        headers=headers,
    )

    assert response.status_code == 200
    group = result.groups[0]
    # Independent candidates are not duplicates, so all of them may be selected.
    assert group.selected_for_removal == [member.path for member in group.members]
    assert group.reviewed_paths == group.selected_for_removal


def test_smart_select_can_target_a_subset_of_groups(tmp_path: Path) -> None:
    result = _two_group_result(tmp_path)
    first, second = result.groups
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    response = client.post(
        "/api/smart-select",
        json={"rule": "deselect_all", "group_ids": [second.id], "scan_id": scan_id},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json()["group_count"] == 1
    assert first.selected_for_removal
    assert second.selected_for_removal == []


def test_action_partial_success_response_includes_selection_counts_and_items(
    tmp_path: Path,
) -> None:
    """Dry-run and executed /api/action responses include selection_counts and a mixed items array."""
    result = _two_group_result(tmp_path)
    Path(result.groups[0].suggested_keep).unlink()

    app = create_app(result)
    client = app.test_client()
    token = app.config["DEDUPE_CSRF_TOKEN"]
    scan_id = client.get("/api/status").get_json()["scan_id"]
    quarantine = tmp_path / "quarantine"
    base_payload = {
        "action": "quarantine",
        "scan_id": scan_id,
        "quarantine_dir": str(quarantine),
    }

    preview = client.post(
        "/api/action",
        json={**base_payload, "dry_run": True},
        headers={"X-Dedupe-Token": token},
    )
    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert "selection_counts" in preview_payload
    assert preview_payload["selection_counts"]["exact"] == 2
    preview_items = preview_payload["items"]
    assert len(preview_items) == 2
    assert {item["ok"] for item in preview_items} == {True, False}

    executed = client.post(
        "/api/action",
        json={**base_payload, "dry_run": False, "preview_token": preview_payload["preview_token"]},
        headers={"X-Dedupe-Token": token},
    )
    assert executed.status_code == 200
    executed_payload = executed.get_json()
    assert "selection_counts" in executed_payload
    executed_items = executed_payload["items"]
    assert len(executed_items) == 2
    assert {item["ok"] for item in executed_items} == {True, False}
    assert len(list(quarantine.iterdir())) == 1


def test_thumbnail_is_generated_once_and_served_from_disk(tmp_path: Path, monkeypatch) -> None:
    result = _result(tmp_path)
    client = create_app(result).test_client()
    scanned = Path(result.files[0].path)
    calls = []

    def fake_thumbnail(path: Path, *, variant: str = "thumb") -> bytes:
        calls.append((str(path), variant))
        return b"jpeg-bytes"

    monkeypatch.setattr(web_media, "image_thumbnail_bytes", fake_thumbnail)

    first = client.get("/api/thumbnail", query_string={"path": str(scanned)})
    second = client.get("/api/thumbnail", query_string={"path": str(scanned)})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.data == second.data == b"jpeg-bytes"
    assert len(calls) == 1
    assert "immutable" in second.headers["Cache-Control"]
    assert second.headers["ETag"]
    assert second.headers["Last-Modified"]

    cached = list((tmp_path / "thumbs").rglob("*.jpg"))
    assert len(cached) == 1


def test_image_thumbnail_uses_display_orientation(tmp_path: Path) -> None:
    from io import BytesIO

    from PIL import Image

    source = tmp_path / "portrait.jpg"
    exif = Image.Exif()
    exif[274] = 6  # Stored landscape, displayed 90 degrees clockwise.
    Image.new("RGB", (80, 40), "navy").save(source, exif=exif)

    with Image.open(BytesIO(web_media.image_thumbnail_bytes(source))) as preview:
        assert preview.size == (40, 80)


def test_thumbnail_cache_key_changes_when_source_file_changes(
    tmp_path: Path, monkeypatch
) -> None:
    result = _result(tmp_path)
    client = create_app(result).test_client()
    scanned = Path(result.files[0].path)
    calls = []

    def fake_thumbnail(path: Path, *, variant: str = "thumb") -> bytes:
        calls.append(str(path))
        return b"jpeg-%d" % len(calls)

    monkeypatch.setattr(web_media, "image_thumbnail_bytes", fake_thumbnail)

    original = client.get("/api/thumbnail", query_string={"path": str(scanned)})
    scanned.write_bytes(b"same duplicate but edited")
    stat = scanned.stat()
    os.utime(scanned, ns=(stat.st_mtime_ns + 1_000_000_000, stat.st_mtime_ns + 1_000_000_000))
    refreshed = client.get("/api/thumbnail", query_string={"path": str(scanned)})

    assert len(calls) == 2
    assert original.headers["ETag"] != refreshed.headers["ETag"]


def test_full_thumbnail_serves_original_for_browser_safe_images(
    tmp_path: Path, monkeypatch
) -> None:
    result = _result(tmp_path)
    client = create_app(result).test_client()
    scanned = Path(result.files[0].path)
    calls = []

    def fake_thumbnail(path: Path, *, variant: str = "thumb") -> bytes:
        calls.append(variant)
        return b"jpeg-bytes"

    monkeypatch.setattr(web_media, "image_thumbnail_bytes", fake_thumbnail)

    response = client.get("/api/thumbnail", query_string={"path": str(scanned), "full": "1"})

    assert response.status_code == 200
    assert response.data == scanned.read_bytes()
    assert calls == []  # Browsers render JPEG natively; no transcode happens.


def test_full_thumbnail_transcodes_formats_browsers_cannot_render(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "raw.tif"
    path.write_bytes(b"tiff bytes")
    stat = path.stat()
    record = FileRecord(
        path=str(path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        media_type=MediaType.IMAGE,
        extension=".tif",
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
    )
    result = ScanResult(roots=[str(tmp_path)], files=[record], groups=[])
    client = create_app(result).test_client()
    calls = []

    def fake_thumbnail(source: Path, *, variant: str = "thumb") -> bytes:
        calls.append(variant)
        return b"transcoded-jpeg"

    monkeypatch.setattr(web_media, "image_thumbnail_bytes", fake_thumbnail)

    response = client.get("/api/thumbnail", query_string={"path": str(path), "full": "1"})

    assert response.status_code == 200
    assert response.data == b"transcoded-jpeg"
    assert calls == ["full"]


def test_thumbnail_rejects_paths_outside_the_scan(tmp_path: Path) -> None:
    result = _result(tmp_path)
    client = create_app(result).test_client()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not part of scan")

    forbidden = client.get("/api/thumbnail", query_string={"path": str(outside)})
    traversal = client.get(
        "/api/thumbnail",
        query_string={"path": f"{result.files[0].path}/../outside.jpg"},
    )

    assert forbidden.status_code == 403
    assert traversal.status_code in (403, 404)
    assert not list((tmp_path / "thumbs").rglob("*.jpg"))


def test_thumbnail_cache_prunes_least_recently_used_entries(tmp_path: Path) -> None:
    cache_dir = tmp_path / "thumbs"
    for index in range(4):
        target = cache_dir / f"{index:02d}" / "entry.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 100)
        os.utime(target, (1_600_000_000 + index, 1_600_000_000 + index))

    removed = web_media.prune_thumbnail_cache(cache_dir=cache_dir, budget=250)

    remaining = sorted(path.parent.name for path in cache_dir.rglob("*.jpg"))
    assert removed == 2
    assert remaining == ["02", "03"]


def test_groups_endpoint_paginates_without_changing_default_behaviour(tmp_path: Path) -> None:
    result = _two_group_result(tmp_path)
    client = create_app(result).test_client()

    everything = client.get("/api/groups").get_json()
    first = client.get("/api/groups", query_string={"limit": 1}).get_json()
    second = client.get("/api/groups", query_string={"limit": 1, "offset": 1}).get_json()
    past_end = client.get("/api/groups", query_string={"limit": 1, "offset": 99}).get_json()

    # Two duplicate groups plus the per-root All-Files browse group.
    assert len(everything["groups"]) == 3
    assert everything["total"] == 3
    assert [g["id"] for g in first["groups"] + second["groups"]] == [
        g["id"] for g in everything["groups"][:2]
    ]
    assert first["total"] == second["total"] == 3
    assert past_end["groups"] == []
    assert everything["groups_version"] == first["groups_version"]


def _wait_idle(client, timeout: float = 5.0) -> dict:
    """Return the first status snapshot with no scan in flight."""
    deadline = time.monotonic() + timeout
    status = client.get("/api/status").get_json()
    while status["scanning"] and time.monotonic() < deadline:
        time.sleep(0.005)
        status = client.get("/api/status").get_json()
    assert status["scanning"] is False
    return status


def _quarantine_action(tmp_path: Path, scan_id: str) -> dict:
    return {
        "action": "quarantine",
        "dry_run": False,
        "scan_id": scan_id,
        "quarantine_dir": str(tmp_path / "quarantine"),
        "kinds": "exact",
    }


def test_second_execute_is_refused_while_one_is_in_flight(tmp_path: Path, monkeypatch) -> None:
    app = create_app(_result(tmp_path))
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = app.test_client().get("/api/status").get_json()["scan_id"]
    base = _quarantine_action(tmp_path, scan_id)
    real_apply = web_app.apply_actions
    inside = threading.Event()
    release = threading.Event()
    executes = []

    def blocking_apply(groups, **kwargs):
        if kwargs.get("dry_run", True):
            return real_apply(groups, **kwargs)
        executes.append(kwargs)
        inside.set()
        assert release.wait(5)
        return real_apply(groups, **kwargs)

    monkeypatch.setattr(web_app, "apply_actions", blocking_apply)
    token = app.test_client().post(
        "/api/action", json={**base, "dry_run": True}, headers=headers
    ).get_json()["preview_token"]
    first: dict = {}

    def execute() -> None:
        response = app.test_client().post(
            "/api/action", json={**base, "preview_token": token}, headers=headers
        )
        first["status"] = response.status_code
        first["payload"] = response.get_json()

    worker = threading.Thread(target=execute)
    worker.start()
    assert inside.wait(5)
    refused = app.test_client().post(
        "/api/action", json={**base, "preview_token": token}, headers=headers
    )
    release.set()
    worker.join(5)

    assert refused.status_code == 409
    assert "already running" in refused.get_json()["error"]
    assert first["status"] == 200
    assert first["payload"]["success_count"] == 1
    assert len(executes) == 1
    assert len(list((tmp_path / "quarantine").iterdir())) == 1
    assert app.test_client().get("/api/status").get_json()["acting"] is False


def test_action_is_refused_while_a_scan_is_running(tmp_path: Path, monkeypatch) -> None:
    scanning = threading.Event()
    release = threading.Event()

    def blocking_scan(paths, **kwargs):
        scanning.set()
        assert release.wait(5)
        return ScanResult(roots=[str(tmp_path)], files=[], groups=[])

    monkeypatch.setattr(web_app, "run_scan", blocking_scan)
    app = create_app(_result(tmp_path), review_session_path=tmp_path / "review.json")
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}

    started = client.post(
        "/api/scan", json={"paths": [str(tmp_path)]}, headers=headers
    ).get_json()
    assert scanning.wait(5)
    refused = client.post(
        "/api/action",
        json={"action": "trash", "dry_run": True, "scan_id": started["scan_id"]},
        headers=headers,
    )
    release.set()
    status = _wait_idle(client)

    assert refused.status_code == 409
    assert "scan" in refused.get_json()["error"]
    assert status["acting"] is False


def test_scan_is_refused_while_an_action_is_running(tmp_path: Path, monkeypatch) -> None:
    app = create_app(_result(tmp_path), review_session_path=tmp_path / "review.json")
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = app.test_client().get("/api/status").get_json()["scan_id"]
    real_apply = web_app.apply_actions
    inside = threading.Event()
    release = threading.Event()
    scans = []

    def blocking_apply(groups, **kwargs):
        inside.set()
        assert release.wait(5)
        return real_apply(groups, **kwargs)

    monkeypatch.setattr(web_app, "apply_actions", blocking_apply)
    monkeypatch.setattr(web_app, "run_scan", lambda paths, **kwargs: scans.append(paths))
    preview: dict = {}

    def act() -> None:
        response = app.test_client().post(
            "/api/action",
            json={"action": "trash", "dry_run": True, "scan_id": scan_id},
            headers=headers,
        )
        preview["status"] = response.status_code

    worker = threading.Thread(target=act)
    worker.start()
    assert inside.wait(5)
    refused = app.test_client().post(
        "/api/scan", json={"paths": [str(tmp_path)]}, headers=headers
    )
    release.set()
    worker.join(5)

    assert refused.status_code == 409
    assert "file action already running" in refused.get_json()["error"]
    assert preview["status"] == 200
    assert scans == []
    status = app.test_client().get("/api/status").get_json()
    assert status["acting"] is False and status["scanning"] is False


def test_simultaneous_executes_sharing_one_preview_token_act_once(tmp_path: Path) -> None:
    result = _result(tmp_path)
    app = create_app(result)
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = app.test_client().get("/api/status").get_json()["scan_id"]
    base = _quarantine_action(tmp_path, scan_id)
    token = app.test_client().post(
        "/api/action", json={**base, "dry_run": True}, headers=headers
    ).get_json()["preview_token"]

    barrier = threading.Barrier(2)
    guard = threading.Lock()
    outcomes: list[tuple[int, dict]] = []

    def execute() -> None:
        client = app.test_client()
        barrier.wait(5)
        response = client.post(
            "/api/action", json={**base, "preview_token": token}, headers=headers
        )
        with guard:
            outcomes.append((response.status_code, response.get_json()))

    threads = [threading.Thread(target=execute) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert sorted(status for status, _ in outcomes) == [200, 409]
    assert sum(payload.get("success_count", 0) for _, payload in outcomes) == 1
    assert len(list((tmp_path / "quarantine").iterdir())) == 1
    assert len([record for record in result.files if Path(record.path).exists()]) == 1
    assert app.test_client().get("/api/status").get_json()["acting"] is False


def test_scan_cancel_racing_completion_leaves_state_coherent(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    completed = _result(root)
    entered = threading.Event()
    release = threading.Event()

    def blocking_scan(paths, **kwargs):
        entered.set()
        assert release.wait(5)
        return completed

    monkeypatch.setattr(web_app, "run_scan", blocking_scan)
    app = create_app(review_session_path=tmp_path / "review.json")
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    started = client.post(
        "/api/scan", json={"paths": [str(root)]}, headers=headers
    ).get_json()
    assert entered.wait(5)

    barrier = threading.Barrier(2)
    cancelled: dict = {}

    def cancel() -> None:
        barrier.wait(5)
        response = app.test_client().post(
            "/api/scan/cancel", json={"scan_id": started["scan_id"]}, headers=headers
        )
        cancelled["status"] = response.status_code

    canceller = threading.Thread(target=cancel)
    canceller.start()
    barrier.wait(5)
    release.set()
    canceller.join(5)
    status = _wait_idle(client)

    assert cancelled["status"] in (200, 409)
    assert status["scan_id"] == started["scan_id"]
    assert status["error"] is None
    assert status["progress"]["done"] is True
    assert status["progress"]["message"].startswith("Done")
    assert status["summary"]["group_count"] == len(completed.groups)
    stale_cancel = client.post(
        "/api/scan/cancel", json={"scan_id": status["scan_id"]}, headers=headers
    )
    assert stale_cancel.status_code == 409


def test_stale_scan_worker_never_overwrites_a_newer_scan(
    tmp_path: Path, monkeypatch
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    stale = _result(first_root)
    fresh = _result(second_root)
    captured: dict = {}
    entered = threading.Event()
    release = threading.Event()

    def blocking_scan(paths, **kwargs):
        if paths[0] == str(first_root):
            captured["progress"] = kwargs["progress"]
            captured["on_group"] = kwargs["on_group"]
            entered.set()
            assert release.wait(5)
            raise InterruptedError("scan cancelled")
        return fresh

    monkeypatch.setattr(web_app, "run_scan", blocking_scan)
    app = create_app(review_session_path=tmp_path / "review.json")
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}

    first = client.post(
        "/api/scan", json={"paths": [str(first_root)]}, headers=headers
    ).get_json()
    assert entered.wait(5)
    assert client.post(
        "/api/scan/cancel", json={"scan_id": first["scan_id"]}, headers=headers
    ).status_code == 200
    release.set()
    _wait_idle(client)

    second = client.post(
        "/api/scan", json={"paths": [str(second_root)]}, headers=headers
    ).get_json()
    before = _wait_idle(client)
    assert before["scan_id"] == second["scan_id"]

    # The abandoned worker's callbacks fire after a newer scan already owns the state.
    late = threading.Thread(
        target=lambda: (
            captured["progress"](ScanProgress(phase="hashing", message="stale progress")),
            captured["on_group"](stale.groups[0]),
        )
    )
    late.start()
    late.join(5)

    after = client.get("/api/status").get_json()
    group_ids = [group["id"] for group in client.get("/api/groups").get_json()["groups"]]
    assert after["scan_id"] == second["scan_id"]
    assert after["progress"] == before["progress"]
    assert after["groups_version"] == before["groups_version"]
    assert group_ids == [group.id for group in fresh.groups]
    assert stale.groups[0].id not in group_ids


def test_bulk_min_faces_rule_only_selects_analyzed_multi_face_files(tmp_path: Path) -> None:
    from dedupe.web.app import bulk_member_matches, parse_bulk_criteria

    def record(name: str, face_count: int | None) -> FileRecord:
        return FileRecord(
            path=str(tmp_path / name),
            size=100,
            mtime=1.0,
            media_type=MediaType.IMAGE,
            extension=".jpg",
            face_count=face_count,
        )

    two_faces = record("two.jpg", 2)
    one_face = record("one.jpg", 1)
    unanalyzed = record("unknown.jpg", None)
    group = build_groups([[two_faces, one_face, unanalyzed]], [])[0]

    criteria = parse_bulk_criteria({"min_faces": "2"})
    assert criteria == {"min_faces": 2}
    assert bulk_member_matches(two_faces, group, criteria)
    assert not bulk_member_matches(one_face, group, criteria)
    assert not bulk_member_matches(unanalyzed, group, criteria)

    with pytest.raises(ValueError):
        parse_bulk_criteria({"min_faces": 0})
    with pytest.raises(ValueError):
        parse_bulk_criteria({"min_faces": "not-a-number"})


def _two_exact_groups(tmp_path: Path):
    """Two exact-duplicate groups (four real files) for streaming tests."""
    records = []
    member_lists = []
    for label in ("one", "two"):
        data = f"identical-payload-{label}".encode()
        pair = []
        for suffix in ("a", "b"):
            path = tmp_path / f"{label}-{suffix}.jpg"
            path.write_bytes(data)
            stat = path.stat()
            pair.append(
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
        member_lists.append(pair)
        records.extend(pair)
    return records, build_groups(member_lists, [])


def test_status_summary_includes_faces_files(tmp_path: Path) -> None:
    status = create_app(_faces_result(tmp_path)).test_client().get("/api/status").get_json()
    assert status["summary"]["faces_files"] == 1


def test_status_poll_does_not_recompute_stats(tmp_path: Path, monkeypatch) -> None:
    """Serving a poll is O(1): stats are maintained where mutations happen."""
    calls = 0
    original = ScanResult.recompute_stats

    def counting(self):
        nonlocal calls
        calls += 1
        return original(self)

    monkeypatch.setattr(ScanResult, "recompute_stats", counting)
    app = create_app(_result(tmp_path))
    after_init = calls
    client = app.test_client()
    first = client.get("/api/status").get_json()
    second = client.get("/api/status").get_json()
    assert calls == after_init
    assert first["summary"]["selected_count"] == second["summary"]["selected_count"] == 1


def test_streamed_groups_update_stats_incrementally(tmp_path: Path, monkeypatch) -> None:
    """Mid-scan /api/status reflects streamed groups without a full recompute."""
    records, groups = _two_exact_groups(tmp_path)
    gate = threading.Event()
    streamed = threading.Event()

    def fake_run_scan(paths, **kwargs):
        on_group = kwargs["on_group"]
        on_group(groups[0])
        streamed.set()
        gate.wait(15)
        on_group(groups[1])
        result = ScanResult(roots=[str(tmp_path)], files=records, groups=list(groups))
        result.recompute_stats()
        return result

    monkeypatch.setattr(web_app, "run_scan", fake_run_scan)
    app = create_app()
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "cache.sqlite3")
    client = app.test_client()
    token = app.config["DEDUPE_CSRF_TOKEN"]

    started = client.post(
        "/api/scan",
        json={"paths": [str(tmp_path)], "parallel_streams": False, "use_cache": False},
        headers={"X-Dedupe-Token": token},
    )
    assert started.status_code == 200
    assert streamed.wait(10)

    status = {}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client.get("/api/status").get_json()
        if status.get("summary", {}).get("group_count") == 1:
            break
        time.sleep(0.02)
    # One streamed group: auto smart select picks one member for removal.
    assert status["scanning"] is True
    assert status["summary"]["exact_groups"] == 1
    assert status["summary"]["selected_count"] == 1
    assert status["summary"]["reclaimable_bytes"] > 0

    gate.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client.get("/api/status").get_json()
        if not status["scanning"]:
            break
        time.sleep(0.02)
    assert status["scanning"] is False
    # The two streamed exact groups plus the All-Files browse group added at
    # scan completion.
    assert status["summary"]["group_count"] == 3
    assert status["summary"]["selected_count"] == 2


def test_events_stream_delivers_groups_status_and_reset(tmp_path: Path, monkeypatch) -> None:
    records, groups = _two_exact_groups(tmp_path)
    gate = threading.Event()

    def fake_run_scan(paths, **kwargs):
        on_group = kwargs["on_group"]
        on_group(groups[0])
        gate.wait(15)
        on_group(groups[1])
        result = ScanResult(roots=[str(tmp_path)], files=records, groups=list(groups))
        result.recompute_stats()
        return result

    monkeypatch.setattr(web_app, "run_scan", fake_run_scan)
    app = create_app()
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "cache.sqlite3")
    client = app.test_client()
    token = app.config["DEDUPE_CSRF_TOKEN"]

    response = client.get("/api/events", buffered=False)
    chunks: list[str] = []
    stop_reading = threading.Event()

    def reader() -> None:
        try:
            for chunk in response.response:
                chunks.append(
                    chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                )
                if stop_reading.is_set():
                    break
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()

    deadline = time.monotonic() + 15
    while not chunks and time.monotonic() < deadline:
        time.sleep(0.02)
    assert chunks  # stream is live before the scan starts

    started = client.post(
        "/api/scan",
        json={"paths": [str(tmp_path)], "parallel_streams": False, "use_cache": False},
        headers={"X-Dedupe-Token": token},
    )
    assert started.status_code == 200

    def seen(needle: str) -> bool:
        return any(needle in chunk for chunk in chunks)

    while not seen(groups[0].id) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seen("event: group") and seen(groups[0].id)

    gate.set()
    while not (seen('"done": true') and seen("event: reset")) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seen('"done": true')
    # The completed result replaced the streaming one: a reset tells clients
    # to refetch instead of trusting per-connection append tracking. Group
    # deltas are best-effort mid-scan; the reset is authoritative, so g2 (which
    # landed in the same wakeup as completion) is covered by the refetch.
    assert seen("event: reset")
    final = client.get("/api/groups?kind=all").get_json()
    # The completed result also carries the per-root All-Files browse group.
    assert {g["id"] for g in final["groups"]} >= {groups[0].id, groups[1].id}
    assert any(g["kind"] == "all_files" for g in final["groups"])
    stop_reading.set()
    # Do not response.close(): the reader thread can be blocked inside the
    # generator's idle wait, and closing then raises "generator already
    # executing". The daemon reader and GC reclaim it at test end.


def test_selection_persist_debounced_for_large_results(tmp_path: Path, monkeypatch) -> None:
    """Above the group threshold, selection toggles coalesce session writes."""
    monkeypatch.setattr(web_app, "PERSIST_DEBOUNCE_MIN_GROUPS", 0)
    monkeypatch.setattr(web_app, "PERSIST_DEBOUNCE_SECONDS", 0.05)
    app = create_app(_result(tmp_path))
    client = app.test_client()
    token = app.config["DEDUPE_CSRF_TOKEN"]
    state = app.extensions["dedupe_state"]

    group = client.get("/api/groups").get_json()["groups"][0]
    scan_id = client.get("/api/status").get_json()["scan_id"]
    state["persist_dirty"] = False  # drain the startup persist

    response = client.post(
        "/api/selection",
        json={
            "group_id": group["id"],
            "selected": [group["members"][0]["path"]],
            "scan_id": scan_id,
        },
        headers={"X-Dedupe-Token": token},
    )
    assert response.status_code == 200
    assert state["persist_dirty"] is True  # debounced, not yet on disk

    deadline = time.monotonic() + 5
    while state["persist_dirty"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state["persist_dirty"] is False
    assert state["review_session"].saved_at is not None


def test_transparent_image_thumbnail_composites_on_white(tmp_path: Path) -> None:
    """Transparent pixels must not turn black in thumbnails."""
    from io import BytesIO

    from PIL import Image

    source = tmp_path / "transparent.png"
    image = Image.new("RGBA", (8, 8), (200, 0, 0, 0))
    image.save(source, format="PNG")

    data = web_media.image_thumbnail_bytes(source)
    with Image.open(BytesIO(data)) as thumb:
        pixel = thumb.convert("RGB").getpixel((0, 0))
    assert all(channel > 200 for channel in pixel)


def test_concurrent_thumbnail_generation_is_deduplicated(tmp_path: Path, monkeypatch) -> None:
    """Parallel requests for one uncached thumbnail share a single generation."""
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"not really a jpeg; generation is faked")

    calls = 0
    calls_lock = threading.Lock()

    def fake_generate(path: Path, *, variant: str) -> bytes:
        nonlocal calls
        time.sleep(0.05)
        with calls_lock:
            calls += 1
        return b"fake-jpeg-bytes"

    monkeypatch.setattr(web_media, "generate_thumbnail_bytes", fake_generate)
    cache_dir = tmp_path / "thumbs"

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                web_media.cached_thumbnail(source, cache_dir=cache_dir)
            )
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert calls == 1
    assert all(result is not None for result in results)
    assert len(results) == 8


def test_preview_variant_downscales_browser_safe_images(tmp_path: Path) -> None:
    """The lightbox preview variant is a cached ≤2560px JPEG, not the original."""
    from io import BytesIO

    from PIL import Image

    path = tmp_path / "big.jpg"
    Image.new("RGB", (4000, 3000), (24, 48, 96)).save(path, quality=90)
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
    )
    result = ScanResult(roots=[str(tmp_path)], files=[record], groups=[])
    client = create_app(result).test_client()

    response = client.get(
        "/api/thumbnail", query_string={"path": str(path), "variant": "preview"}
    )

    assert response.status_code == 200
    assert "immutable" in response.headers["Cache-Control"]
    with Image.open(BytesIO(response.data)) as preview:
        assert max(preview.size) <= 2560
        assert preview.format == "JPEG"
    assert len(list((tmp_path / "thumbs").rglob("*.jpg"))) == 1


def test_reveal_returns_scanned_path_and_rejects_outside(tmp_path: Path) -> None:
    result = _result(tmp_path)
    app = create_app(result)
    client = app.test_client()
    scanned = result.files[0].path

    ok = client.get("/api/reveal", query_string={"path": scanned})
    assert ok.status_code == 200
    assert ok.get_json() == {"path": scanned, "exists": True}

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"nope")
    forbidden = client.get("/api/reveal", query_string={"path": str(outside)})
    assert forbidden.status_code == 403


def test_reveal_open_requires_token_and_invokes_finder(tmp_path: Path, monkeypatch) -> None:
    result = _result(tmp_path)
    app = create_app(result)
    client = app.test_client()
    scanned = result.files[0].path

    # open=1 has a side effect: the mutating-token rule applies even to GET.
    no_token = client.get("/api/reveal", query_string={"path": scanned, "open": "1"})
    assert no_token.status_code == 403

    opened = []
    import subprocess as subprocess_module

    class FakePopen:
        def __init__(self, command):
            opened.append(command)

    monkeypatch.setattr(subprocess_module, "Popen", FakePopen)
    ok = client.get(
        "/api/reveal",
        query_string={"path": scanned, "open": "1"},
        headers={"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]},
    )
    assert ok.status_code == 200
    assert opened == [["open", "-R", scanned]]


def test_shutdown_stops_server_after_grace_and_new_request_cancels(
    tmp_path: Path,
) -> None:
    class FakeServer:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    # Cancellation: a request during the grace period keeps the server alive.
    app = create_app()
    fake = FakeServer()
    app.extensions["dedupe_server"] = fake
    client = app.test_client()
    token = app.config["DEDUPE_CSRF_TOKEN"]

    response = client.post("/api/shutdown", json={}, headers={"X-Dedupe-Token": token})
    assert response.status_code == 200
    client.get("/api/status")  # a reloaded page cancels the pending shutdown
    time.sleep(2.0)
    assert fake.shutdown_calls == 0

    # Uninterrupted: the grace timer fires and stops the server.
    response = client.post("/api/shutdown", json={}, headers={"X-Dedupe-Token": token})
    assert response.status_code == 200
    deadline = time.monotonic() + 5
    while not fake.shutdown_calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert fake.shutdown_calls == 1


def test_review_session_resume_and_discard_endpoints(tmp_path: Path) -> None:
    from dedupe.review_session import save_review_session

    session_path = tmp_path / "state" / "review-session.json"
    result = _result(tmp_path)

    # The app starts empty (nothing auto-loaded without a result), then resume
    # revalidates against disk and installs the saved review. The session is
    # written after create_app because an initial_result is persisted at boot.
    app = create_app(initial_result=ScanResult(roots=[], files=[], groups=[]),
                     review_session_path=session_path)
    save_review_session(result, session_path)
    client = app.test_client()
    token = app.config["DEDUPE_CSRF_TOKEN"]
    assert client.get("/api/groups").get_json()["groups"] == []

    resumed = client.post(
        "/api/review-session/resume", json={}, headers={"X-Dedupe-Token": token}
    )
    assert resumed.status_code == 200
    groups = client.get("/api/groups").get_json()["groups"]
    # Resume upgrades older sessions with the per-root All-Files browse group.
    assert len(groups) == len(result.groups) + 1
    browse = next(group for group in groups if group["kind"] == "all_files")
    assert {member["path"] for member in browse["members"]} == {
        record.path for record in result.files
    }

    discarded = client.delete(
        "/api/review-session",
        # The browser client always sends a JSON content type, even body-less.
        content_type="application/json",
        headers={"X-Dedupe-Token": token},
    )
    assert discarded.status_code == 200
    assert client.get("/api/groups").get_json()["groups"] == []
    assert not session_path.exists()

    # Resuming with nothing on disk is a clean 404.
    missing = client.post(
        "/api/review-session/resume", json={}, headers={"X-Dedupe-Token": token}
    )
    assert missing.status_code == 404


def test_review_session_discard_filesystem_failure_is_a_client_error(
    tmp_path: Path, monkeypatch
) -> None:
    app = create_app(
        initial_result=ScanResult(roots=[], files=[], groups=[]),
        review_session_path=tmp_path / "state" / "review-session.json",
    )
    monkeypatch.setattr(
        "dedupe.web.app.discard_review_session",
        lambda path: (_ for _ in ()).throw(OSError("read-only filesystem")),
    )
    response = app.test_client().delete(
        "/api/review-session",
        content_type="application/json",
        headers={"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]},
    )
    assert response.status_code == 400
    assert "read-only filesystem" in response.get_json()["error"]


def test_mark_distinct_cache_failure_is_a_structured_client_error(
    tmp_path: Path, monkeypatch
) -> None:
    result = _result(tmp_path)
    result.groups = build_groups([], [result.files])
    group = result.groups[0]
    app = create_app(result)
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "hashes.sqlite3")
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    monkeypatch.setattr(
        HashCache,
        "mark_distinct",
        lambda self, records: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )

    response = client.post(
        "/api/similar/mark-distinct",
        json={"group_id": group.id, "scan_id": scan_id},
        headers=headers,
    )

    assert response.status_code == 400
    assert "could not save distinct review" in response.get_json()["error"]
    # The failed write leaves the review untouched.
    assert len(client.get("/api/groups?kind=similar").get_json()["groups"]) == 1


def test_status_reports_capabilities_and_review_health(tmp_path: Path) -> None:
    status = create_app(_result(tmp_path)).test_client().get("/api/status").get_json()
    capabilities = status["capabilities"]
    assert set(capabilities) == {"opencv", "yunet_model", "photon", "ffmpeg", "ffprobe"}
    assert all(isinstance(value, bool) for value in capabilities.values())
    assert status["keep_decisions_error"] is None
    assert status["trash_undo_cleared"] == 0
    assert status["summary"]["errors_total"] == len(status["summary"]["errors"])


def test_check_exclusions_counts_matches_per_glob(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "a.jpg").write_bytes(b"x")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "b.jpg").write_bytes(b"x")
    (tmp_path / "keep.jpg").write_bytes(b"x")
    app = create_app()
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}

    response = client.post(
        "/api/scan/check-exclusions",
        json={
            "paths": [str(tmp_path)],
            "exclusions": ["exports", "cache/**", "nope-*"],
        },
        headers=headers,
    )

    assert response.status_code == 200
    patterns = {entry["pattern"]: entry for entry in response.get_json()["patterns"]}
    assert patterns["exports"]["matches"] >= 1
    assert patterns["cache/**"]["matches"] >= 1
    assert patterns["nope-*"]["matches"] == 0


def test_check_exclusions_requires_paths_and_globs(tmp_path: Path) -> None:
    app = create_app()
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    no_paths = client.post(
        "/api/scan/check-exclusions",
        json={"exclusions": ["exports"]},
        headers=headers,
    )
    no_globs = client.post(
        "/api/scan/check-exclusions",
        json={"paths": [str(tmp_path)]},
        headers=headers,
    )
    assert no_paths.status_code == 400
    assert no_globs.status_code == 400


def test_scan_start_reports_cleared_trash_undo_map(tmp_path: Path) -> None:
    app = create_app(_non_human_result(tmp_path))
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "cache.sqlite3")
    trashed = str(tmp_path / "landscape.jpg")
    app.extensions["dedupe_state"]["deleted_files"] = {trashed: trashed}
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}

    started = client.post(
        "/api/scan",
        json={
            "paths": [str(tmp_path)],
            "similar": False,
            "include_videos": False,
            "use_cache": False,
        },
        headers=headers,
    )
    assert started.status_code == 200

    status = client.get("/api/status").get_json()
    assert status["trash_undo_cleared"] == 1

    deadline = time.monotonic() + 15
    while status["scanning"] and time.monotonic() < deadline:
        time.sleep(0.05)
        status = client.get("/api/status").get_json()
    assert not status["scanning"]


def test_keep_decisions_write_failure_is_surfaced_in_status(
    tmp_path: Path, monkeypatch
) -> None:
    result = _result(tmp_path)
    for record in result.files:
        record.width = 320
        record.height = 240
    low_resolution = build_low_resolution_groups(result.files)[0]
    result.groups = [result.groups[0], low_resolution]
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    def broken_keeps(*, keep, clear):
        raise OSError("disk full")

    monkeypatch.setattr(web_app, "update_keep_decisions", broken_keeps)
    target = low_resolution.members[0].path
    decision = client.post(
        "/api/selection",
        json={
            "group_id": low_resolution.id,
            "decision_path": target,
            "decision_remove": False,
            "scan_id": scan_id,
        },
        headers=headers,
    )
    assert decision.status_code == 200

    status = client.get("/api/status").get_json()
    assert status["keep_decisions_error"] == "disk full"
