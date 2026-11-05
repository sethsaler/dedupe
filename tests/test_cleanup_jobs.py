"""Protected-library and named cleanup-job regression tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PIL import Image

from dedupe.engine import run_scan
from dedupe.jobs import JobStore, file_identity, focus_new_arrivals
from dedupe.models import GroupKind, ScanResult, effective_selected_paths
from dedupe.review_session import load_review_session, save_review_session
from dedupe.web import app as web


@pytest.fixture
def make_app(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web, "detect_capabilities", dict)
    applications = []

    def make(result=None):
        app = web.create_app(result, review_session_path=tmp_path / "review.json",
                             cache_path=tmp_path / "cache.sqlite3")
        applications.append(app)
        return app

    yield make
    for app in applications:
        timer = app.extensions["dedupe_state"].get("persist_timer")
        if timer:
            timer.cancel()


def _image(path: Path, color=(30, 80, 140)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path)
    return path


def _scan(root: Path, protected=None) -> ScanResult:
    return run_scan([root], protected_folders=protected, similar=False,
                    find_low_resolution=False, random_review_count=0, use_cache=False)


def _headers(app):
    return {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}


def _scan_id(client):
    return client.get("/api/status").get_json()["scan_id"]


def _settings(root: Path, protected=None):
    return {"paths": [str(root)], "protected_folders": protected or [],
            "similar": False, "find_low_resolution": False,
            "random_review_count": 0, "use_cache": False}


def _save_job(client, app, name, settings):
    response = client.post("/api/jobs", json={"name": name, "settings": settings},
                           headers=_headers(app))
    assert response.status_code == 200, response.get_json()
    return response.get_json()["id"]


def _run_job(client, app, job_id, **kwargs):
    response = client.post("/api/scan", json={"job_id": job_id, **kwargs},
                           headers=_headers(app))
    assert response.status_code == 200, response.get_json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get("/api/status").get_json()
        if not status["scanning"]:
            assert not status.get("last_error"), status
            return status
        time.sleep(0.01)
    pytest.fail("cleanup job did not finish")


def _groups(client, kind="exact"):
    return client.get(f"/api/groups?kind={kind}").get_json()["groups"]


def test_protected_library_is_compared_but_never_selected_or_moved(
    tmp_path: Path, make_app,
) -> None:
    original = _image(tmp_path / "library" / "original.png")
    library_copy = _image(tmp_path / "library" / "second.png")
    incoming = _image(tmp_path / "inbox" / "incoming.png")
    result = _scan(incoming.parent, [str(original.parent)])
    (group,) = result.groups
    assert {m.path for m in group.members} == {str(original), str(library_copy), str(incoming)}
    assert group.suggested_keep in {str(original), str(library_copy)}
    assert effective_selected_paths(result.groups) == [str(incoming)]

    app = make_app(result)
    client = app.test_client()
    response = client.post("/api/selection", headers=_headers(app), json={
        "scan_id": _scan_id(client), "group_id": group.id,
        "selected": [m.path for m in group.members],
    })
    assert response.status_code == 200
    assert response.get_json()["selected_for_removal"] == [str(incoming)]
    destination = tmp_path / "quarantine"
    request = {"action": "quarantine", "kinds": "exact", "quarantine_dir": str(destination),
               "scan_id": _scan_id(client)}
    preview = client.post("/api/action", headers=_headers(app),
                          json={**request, "dry_run": True})
    assert preview.status_code == 200
    assert preview.get_json()["success_count"] == 1
    executed = client.post("/api/action", headers=_headers(app), json={
        **request, "dry_run": False, "preview_token": preview.get_json()["preview_token"],
    })
    assert executed.status_code == 200, executed.get_json()
    assert executed.get_json()["success_count"] == 1
    assert original.exists() and library_copy.exists()
    assert not incoming.exists()
    assert list(destination.rglob("incoming.png"))


def test_overlapping_roots_keep_protection_and_do_not_duplicate_inventory(tmp_path: Path) -> None:
    original = _image(tmp_path / "media" / "library" / "original.png")
    incoming = _image(tmp_path / "media" / "incoming.png")
    result = _scan(tmp_path / "media", [str(original.parent), str(original.parent)])
    assert len(result.files) == 2
    assert {m.path for m in result.files if m.protected} == {str(original)}
    assert effective_selected_paths(result.groups) == [str(incoming)]
    assert _scan(original.parent, [str(original.parent)]).groups == []


def test_protection_matches_filesystem_identity_on_case_insensitive_volumes(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    _image(library / "one.png")
    _image(library / "two.png")
    alias = tmp_path / "library"
    if not alias.exists():
        pytest.skip("requires a case-insensitive filesystem")
    result = _scan(tmp_path, [str(alias)])
    assert all(member.protected for member in result.files)
    assert effective_selected_paths(result.groups) == []


def test_protection_survives_saved_review_and_blocks_direct_candidate_actions(
    tmp_path: Path, make_app,
) -> None:
    original = _image(tmp_path / "library" / "original.png")
    incoming = _image(tmp_path / "inbox" / "incoming.png")
    result = _scan(incoming.parent, [str(original.parent)])
    save_review_session(result, tmp_path / "review.json")
    restored = load_review_session(tmp_path / "review.json").result
    assert restored is not None
    assert restored.protected_folders == [str(original.parent)]
    assert {m.path for m in restored.files if m.protected} == {str(original)}
    app = make_app()
    client = app.test_client()
    response = client.post("/api/review-session/resume", json={}, headers=_headers(app))
    assert response.status_code == 200
    all_files = _groups(client, "all_files")
    group = next(g for g in all_files if any(m["path"] == str(original) for m in g["members"]))
    request = {"scan_id": _scan_id(client), "group_id": group["id"], "path": str(original)}
    for dry_run in (True, False):
        blocked = client.post("/api/review-candidate/delete", headers=_headers(app),
                              json={**request, "dry_run": dry_run})
        assert blocked.status_code == 400
        assert "protected" in blocked.get_json()["error"]
    blocked = client.post("/api/selection", headers=_headers(app), json={
        "scan_id": _scan_id(client), "group_id": group["id"],
        "decision_path": str(original), "decision_remove": True,
    })
    assert blocked.status_code == 400
    assert original.exists() and incoming.exists()


def test_named_jobs_retain_separate_reviews_across_restart(tmp_path: Path, make_app) -> None:
    first = _image(tmp_path / "first" / "a.png")
    _image(first.parent / "b.png")
    second = _image(tmp_path / "second" / "c.png", (180, 20, 10))
    _image(second.parent / "d.png", (180, 20, 10))
    app = make_app()
    client = app.test_client()
    first_id = _save_job(client, app, "First", _settings(first.parent))
    second_id = _save_job(client, app, "Second", _settings(second.parent))
    _run_job(client, app, first_id)
    (first_group,) = _groups(client)
    response = client.post("/api/selection", headers=_headers(app), json={
        "scan_id": _scan_id(client), "group_id": first_group["id"], "selected": [],
    })
    assert response.status_code == 200
    # Wait for the same debounced save the browser uses before switching jobs.
    store = JobStore(tmp_path / "review.json")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        saved = load_review_session(store.session_path(first_id)).result
        if saved and not effective_selected_paths(saved.groups):
            break
        time.sleep(0.01)
    else:
        pytest.fail("first job selections were not saved")
    _run_job(client, app, second_id)
    assert all(str(second.parent) in m["path"] for g in _groups(client) for m in g["members"])
    restarted = make_app()
    client = restarted.test_client()
    for job_id, root, selected in ((first_id, first.parent, []), (second_id, second.parent, None)):
        response = client.post("/api/review-session/resume", json={"job_id": job_id},
                               headers=_headers(restarted))
        assert response.status_code == 200
        (group,) = _groups(client)
        assert all(Path(m["path"]).parent == root for m in group["members"])
        if selected is not None:
            assert group["selected_for_removal"] == selected
        else:
            assert len(group["selected_for_removal"]) == 1


def test_new_arrivals_include_existing_protected_keeper_then_clear_when_unchanged(
    tmp_path: Path, make_app,
) -> None:
    original = _image(tmp_path / "library" / "original.png")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Imports", _settings(inbox, [str(original.parent)]))
    _run_job(client, app, job_id)
    assert _groups(client) == []
    incoming = _image(inbox / "incoming.png")
    status = _run_job(client, app, job_id, new_only=True)
    assert "1 new or changed files" in status["progress"]["message"]
    (group,) = _groups(client)
    assert {m["path"] for m in group["members"]} == {str(original), str(incoming)}
    assert group["suggested_keep"] == str(original)
    assert group["selected_for_removal"] == [str(incoming)]
    status = _run_job(client, app, job_id, new_only=True)
    assert "0 new or changed files" in status["progress"]["message"]
    assert _groups(client) == []
    assert _groups(client, "all_files") == []
    assert original.exists() and incoming.exists()


def test_new_arrivals_retain_full_duplicate_group_but_filter_independent_candidates(
    tmp_path: Path,
) -> None:
    old = _image(tmp_path / "old.png")
    baseline_result = _scan(tmp_path)
    baseline = {r.path: file_identity(r) for r in baseline_result.files}
    new = _image(tmp_path / "new.png")
    result = run_scan([tmp_path], similar=False, use_cache=False, find_low_resolution=True,
                      random_review_count=50)
    assert focus_new_arrivals(result, baseline) == 1
    (exact,) = [g for g in result.groups if g.kind == GroupKind.EXACT]
    assert {m.path for m in exact.members} == {str(old), str(new)}
    independent = [g for g in result.groups if g.kind in
                   (GroupKind.LOW_RESOLUTION, GroupKind.RANDOM_REVIEW)]
    assert independent
    assert all({m.path for m in g.members} == {str(new)} for g in independent)


@pytest.mark.parametrize("bad_id", ["../review", "a" * 31, "g" * 32, [], 123])
def test_invalid_job_ids_are_rejected_without_touching_files(
    tmp_path: Path, make_app, bad_id,
) -> None:
    original = _image(tmp_path / "media" / "original.png")
    app = make_app()
    client = app.test_client()
    for route, payload in (
        ("/api/jobs", {"id": bad_id, "name": "Invalid", "settings": _settings(original.parent)}),
        ("/api/jobs/delete", {"id": bad_id}),
        ("/api/scan", {"job_id": bad_id, **_settings(original.parent)}),
        ("/api/review-session/resume", {"job_id": bad_id}),
    ):
        response = client.post(route, json=payload, headers=_headers(app))
        assert response.status_code in (400, 404), (route, response.status_code, response.get_json())
    assert original.exists()


def test_deleting_job_removes_saved_review_and_leaves_media(tmp_path: Path, make_app) -> None:
    original = _image(tmp_path / "media" / "a.png")
    copy = _image(original.parent / "b.png")
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Delete metadata only", _settings(original.parent))
    _run_job(client, app, job_id)
    store = JobStore(tmp_path / "review.json")
    assert store.session_path(job_id).exists()
    response = client.post("/api/jobs/delete", json={"id": job_id}, headers=_headers(app))
    assert response.status_code == 200
    assert client.get("/api/jobs").get_json()["jobs"] == []
    assert not store.session_path(job_id).exists()
    assert original.exists() and copy.exists()
    assert json.loads((tmp_path / "review.json").read_text())["result"]["job_id"] is None


def test_deleting_job_before_resuming_does_not_leave_a_dangling_global_review(
    tmp_path: Path, make_app,
) -> None:
    original = _image(tmp_path / "media" / "a.png")
    copy = _image(original.parent / "b.png")
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Imports", _settings(original.parent))
    _run_job(client, app, job_id)
    restarted = make_app()
    response = restarted.test_client().post(
        "/api/jobs/delete", json={"id": job_id}, headers=_headers(restarted),
    )
    assert response.status_code == 200
    saved = load_review_session(tmp_path / "review.json").result
    assert saved is None or saved.job_id is None
    assert original.exists() and copy.exists()


def test_new_only_requires_baseline_and_settings_changes_invalidate_it(
    tmp_path: Path, make_app,
) -> None:
    original = _image(tmp_path / "first" / "a.png")
    second = _image(tmp_path / "second" / "b.png")
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Imports", _settings(original.parent))
    request = {"job_id": job_id, "new_only": True}
    assert client.post("/api/scan", json=request, headers=_headers(app)).status_code == 400
    _run_job(client, app, job_id)
    response = client.post("/api/jobs", headers=_headers(app), json={
        "id": job_id, "name": "Imports", "settings": _settings(second.parent),
    })
    assert response.status_code == 200
    assert client.post("/api/scan", json=request, headers=_headers(app)).status_code == 400


def test_new_only_cannot_apply_an_existing_baseline_to_different_folders(
    tmp_path: Path, make_app,
) -> None:
    original = _image(tmp_path / "first" / "a.png")
    second = _image(tmp_path / "second" / "b.png")
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Imports", _settings(original.parent))
    _run_job(client, app, job_id)
    response = client.post("/api/scan", headers=_headers(app), json={
        "job_id": job_id, "new_only": True, "paths": [str(second.parent)],
    })
    assert response.status_code == 400


@pytest.mark.parametrize("next_action", ["scan", "resume"])
def test_switching_jobs_flushes_pending_review_decisions(
    tmp_path: Path, make_app, monkeypatch, next_action,
) -> None:
    # Exercise the large-review debounce without creating thousands of files.
    monkeypatch.setattr(web, "PERSIST_DEBOUNCE_MIN_GROUPS", 1)
    monkeypatch.setattr(web, "PERSIST_DEBOUNCE_SECONDS", 30)
    first = _image(tmp_path / "first" / "a.png")
    _image(first.parent / "b.png")
    second = _image(tmp_path / "second" / "c.png", (180, 20, 10))
    _image(second.parent / "d.png", (180, 20, 10))
    app = make_app()
    client = app.test_client()
    first_id = _save_job(client, app, "First", _settings(first.parent))
    second_id = _save_job(client, app, "Second", _settings(second.parent))
    _run_job(client, app, second_id)
    _run_job(client, app, first_id)
    (group,) = _groups(client)
    response = client.post("/api/selection", headers=_headers(app), json={
        "scan_id": _scan_id(client), "group_id": group["id"], "selected": [],
    })
    assert response.status_code == 200
    if next_action == "scan":
        _run_job(client, app, second_id)
    else:
        response = client.post("/api/review-session/resume", json={"job_id": second_id},
                               headers=_headers(app))
        assert response.status_code == 200
    saved = load_review_session(JobStore(tmp_path / "review.json").session_path(first_id)).result
    assert saved is not None
    assert effective_selected_paths(saved.groups) == []
