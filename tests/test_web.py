"""Local web API security and state-isolation tests."""

import json
import os
import platform
import subprocess
import threading
import time
from pathlib import Path

import pytest

from dedupe.cache import HashCache
from dedupe.grouping import (
    build_groups,
    build_low_resolution_groups,
    build_no_human_groups,
    build_random_review_groups,
)
from dedupe.human_detection import human_detection_signature
from dedupe.models import (
    FileRecord,
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
    assert "Preview trash" in html
    assert "Preview quarantine" in html
    assert "Preview isolate" in html
    assert 'id="memberPagination"' in html
    assert 'id="memberPaginationBottom"' in html
    assert 'class="btn ghost member-prev"' in html
    assert 'class="btn ghost member-next"' in html
    assert 'id="lbVideo"' in html
    assert 'id="lbSpeed"' in html
    assert 'id="scanQuality"' in html
    assert 'id="resultSearch"' in html
    assert 'id="similarityPreset"' in html
    assert 'id="btnDiscardSession"' in html
    assert 'id="nonHumanBanner"' in html
    assert 'id="candidateReviewBanner"' in html
    assert 'id="optLowResolution"' in html
    assert 'id="optRandomReview"' in html
    assert 'data-kind="low_resolution"' in html
    assert 'data-kind="random_review"' in html
    assert "←</kbd> Delete" in html
    assert "Keep <kbd>→" in html
    assert 'id="lbOpacity"' in html
    assert 'id="lbFlicker"' in html

    script = app.test_client().get("/static/app.js").get_data(as_text=True)
    assert 'class="hover-video"' in script
    assert 'class="thumb-image ${m.media_type === "gif" ? "hover-gif"' in script
    assert 'data-preview-width="${mediaWidth}"' in script
    assert 'setPreviewAspectRatio(image.closest(".thumb-wrap")' in script
    assert 'video.muted = true' in script
    assert 'method: "DELETE"' in script
    assert 'dry_run: true' in script
    assert 'await reviewCandidate(current, member.path, e.key === "ArrowLeft")' in script

    stylesheet = app.test_client().get("/static/app.css").get_data(as_text=True)
    assert "aspect-ratio: var(--preview-aspect-ratio);" in stylesheet
    assert "aspect-ratio: 16 / 10;" not in stylesheet


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

    rejected = client.post(
        "/api/non-human/delete", json={**payload, "dry_run": False}, headers=headers
    )
    assert rejected.status_code == 409
    assert original.exists()

    preview = client.post("/api/non-human/delete", json=payload, headers=headers)
    assert preview.status_code == 200
    assert preview.get_json()["success_count"] == 1
    assert original.exists()
    deleted = client.post(
        "/api/non-human/delete",
        json={**payload, "dry_run": False, "preview_token": preview.get_json()["preview_token"]},
        headers=headers,
    )
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

    preview = client.post(
        "/api/non-human/delete",
        json={"group_id": group_id, "path": deleted_record.path, "scan_id": scan_id},
        headers=headers,
    )
    deleted = client.post(
        "/api/non-human/delete",
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
    assert after["summary"]["group_count"] == 1
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
    assert body["group_count"] == 2
    assert body["selected_count"] == 2
    for group in result.groups:
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
        group.suggested_keep not in group.selected_for_removal for group in result.groups
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

    def fake_thumbnail(path: Path, *, full: bool = False) -> bytes:
        calls.append((str(path), full))
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

    def fake_thumbnail(path: Path, *, full: bool = False) -> bytes:
        calls.append(str(path))
        return b"jpeg-%d" % len(calls)

    monkeypatch.setattr(web_media, "image_thumbnail_bytes", fake_thumbnail)

    original = client.get("/api/thumbnail", query_string={"path": str(scanned)})
    scanned.write_bytes(b"same duplicate but edited")
    stat = scanned.stat()
    os.utime(scanned, ns=(stat.st_mtime_ns + 1_000_000_000, stat.st_mtime_ns + 1_000_000_000))
    refreshed = client.get("/api/thumbnail", query_string={"path": str(scanned)})
    full_variant = client.get("/api/thumbnail", query_string={"path": str(scanned), "full": "1"})

    assert len(calls) == 3
    assert original.headers["ETag"] != refreshed.headers["ETag"]
    assert refreshed.headers["ETag"] != full_variant.headers["ETag"]


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

    assert len(everything["groups"]) == 2
    assert everything["total"] == 2
    assert [g["id"] for g in first["groups"] + second["groups"]] == [
        g["id"] for g in everything["groups"]
    ]
    assert first["total"] == second["total"] == 2
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
