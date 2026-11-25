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
        monitor = app.extensions.get("dedupe_monitor")
        if monitor:
            monitor.stop_all()


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
        ("/api/jobs/monitor", {"id": bad_id, "enabled": True}),
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


def test_job_store_splits_baseline_file_supports_legacy_and_skips_failed_files(
    tmp_path: Path,
) -> None:
    from dedupe.models import FileRecord, MediaType

    root = tmp_path / "photos"
    root.mkdir()
    good_path = _image(root / "good.png")
    bad_path = _image(root / "bad.png")
    store = JobStore(tmp_path / "review.json")
    job = store.save("Daily", _settings(root))
    job_id = job["id"]

    good_rec = FileRecord(
        path=str(good_path.resolve()),
        size=good_path.stat().st_size,
        mtime=good_path.stat().st_mtime,
        media_type=MediaType.IMAGE,
        extension=".png",
    )
    bad_rec = FileRecord(
        path=str(bad_path.resolve()),
        size=bad_path.stat().st_size,
        mtime=bad_path.stat().st_mtime,
        media_type=MediaType.IMAGE,
        extension=".png",
        error="image hash failed: corrupt frame",
    )
    scan_res = ScanResult(
        roots=[str(root.resolve())],
        files=[good_rec, bad_rec],
        groups=[],
        errors=[bad_rec.error],
        job_id=job_id,
        scan_settings=_settings(root),
    )
    store.finish_scan(scan_res)

    meta_raw = json.loads(store._path(job_id).read_text())
    assert "baseline" not in meta_raw
    assert store.baseline_path(job_id).is_file()

    # Job listing must stay compact without loading baseline dictionaries.
    listed = store.list()
    assert len(listed) == 1
    assert "baseline" not in listed[0]

    # Loaded job merges the external baseline cleanly and excluded the failed record.
    loaded = store.get(job_id)
    assert set(loaded["baseline"]) == {good_rec.path}

    # Legacy inline baseline inside {job_id}.json still reads cleanly.
    store.baseline_path(job_id).unlink()
    meta_raw["baseline"] = {good_rec.path: file_identity(good_rec)}
    store._path(job_id).write_text(json.dumps(meta_raw))
    assert set(store.get(job_id)["baseline"]) == {good_rec.path}

    # Removing the job deletes both metadata and external baseline files.
    store.save("Daily", _settings(root), job_id=job_id)
    store.finish_scan(scan_res)
    store.remove(job_id)
    assert not store._path(job_id).exists()
    assert not store.baseline_path(job_id).exists()


def test_focus_new_arrivals_preserves_multi_member_sets_and_excludes_protected_independent(
    tmp_path: Path,
) -> None:
    from dedupe.models import DuplicateGroup, FileRecord, MediaType

    def make_rec(name: str, *, protected: bool = False) -> FileRecord:
        p = _image(tmp_path / name)
        st = p.stat()
        return FileRecord(
            path=str(p.resolve()),
            size=st.st_size,
            mtime=st.st_mtime,
            media_type=MediaType.IMAGE,
            extension=".png",
            device=st.st_dev,
            inode=st.st_ino,
            mtime_ns=st.st_mtime_ns,
            ctime_ns=st.st_ctime_ns,
            protected=protected,
        )

    old = make_rec("old.png")
    old2 = make_rec("old2.png")
    arrival = make_rec("new.png")
    prot_arrival = make_rec("prot_new.png", protected=True)
    baseline = {old.path: file_identity(old), old2.path: file_identity(old2)}

    result = ScanResult(
        roots=[str(tmp_path.resolve())],
        files=[old, old2, arrival, prot_arrival],
        groups=[
            DuplicateGroup(
                id="all-files",
                kind=GroupKind.ALL_FILES,
                media_type=MediaType.IMAGE,
                members=[old, arrival],
            ),
            DuplicateGroup(
                id="set-touching-arrival",
                kind=GroupKind.SETS,
                media_type=MediaType.IMAGE,
                members=[old, arrival],
            ),
            DuplicateGroup(
                id="set-old-only",
                kind=GroupKind.SETS,
                media_type=MediaType.IMAGE,
                members=[old, old2],
            ),
            DuplicateGroup(
                id="lowres",
                kind=GroupKind.LOW_RESOLUTION,
                media_type=MediaType.IMAGE,
                members=[old, arrival, prot_arrival],
                selected_for_removal=[old.path, arrival.path, prot_arrival.path],
            ),
            DuplicateGroup(
                id="exact",
                kind=GroupKind.EXACT,
                media_type=MediaType.IMAGE,
                members=[prot_arrival, arrival],
                suggested_keep=prot_arrival.path,
                selected_for_removal=[arrival.path],
            ),
        ],
    )

    arrivals_count = focus_new_arrivals(result, baseline)
    assert arrivals_count == 2
    by_id = {g.id: g for g in result.groups}
    assert "all-files" not in by_id
    assert "set-old-only" not in by_id
    assert {m.path for m in by_id["set-touching-arrival"].members} == {old.path, arrival.path}
    assert [m.path for m in by_id["lowres"].members] == [arrival.path]
    assert {m.path for m in by_id["exact"].members} == {prot_arrival.path, arrival.path}


def test_new_arrivals_scan_passes_baseline_to_engine(tmp_path: Path, make_app, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    _image(inbox / "original.png")
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Imports", _settings(inbox))
    calls = []
    real_run_scan = web.run_scan

    def spy_run_scan(*args, **kwargs):
        calls.append(kwargs)
        return real_run_scan(*args, **kwargs)

    monkeypatch.setattr(web, "run_scan", spy_run_scan)
    _run_job(client, app, job_id)
    _run_job(client, app, job_id, new_only=True)

    assert len(calls) == 2
    assert calls[1]["new_only"] is True
    stored = JobStore(tmp_path / "review.json").get(job_id)
    assert calls[1]["baseline"] == stored["baseline"]


def test_monitor_requires_a_baseline_and_can_be_disabled(tmp_path: Path, make_app) -> None:
    inbox = tmp_path / "inbox"
    _image(inbox / "original.png")
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Imports", _settings(inbox))

    response = client.post("/api/jobs/monitor", json={"id": job_id, "enabled": True},
                           headers=_headers(app))
    assert response.status_code == 400
    _run_job(client, app, job_id)
    response = client.post("/api/jobs/monitor", json={"id": job_id, "enabled": True},
                           headers=_headers(app))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["monitor"] is True
    assert response.get_json()["monitor_active"] is True
    assert app.extensions["dedupe_monitor"].running(job_id)

    response = client.post("/api/jobs/monitor", json={"id": job_id, "enabled": False},
                           headers=_headers(app))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["monitor"] is False
    assert not app.extensions["dedupe_monitor"].running(job_id)


def test_monitor_setting_is_restored_when_the_app_restarts(tmp_path: Path, make_app) -> None:
    inbox = tmp_path / "inbox"
    _image(inbox / "original.png")
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Imports", _settings(inbox))
    _run_job(client, app, job_id)
    response = client.post("/api/jobs/monitor", json={"id": job_id, "enabled": True},
                           headers=_headers(app))
    assert response.status_code == 200, response.get_json()
    # Simulate the first backend exiting before the replacement app starts.
    app.extensions["dedupe_monitor"].stop(job_id)

    restarted = make_app()
    assert restarted.extensions["dedupe_monitor"].running(job_id)
    jobs_payload = restarted.test_client().get("/api/jobs").get_json()["jobs"]
    assert jobs_payload[0]["monitor"] is True
    assert jobs_payload[0]["monitor_active"] is True
    restarted.test_client().post(
        "/api/jobs/monitor", json={"id": job_id, "enabled": False}, headers=_headers(restarted)
    )


def test_monitor_scans_media_changes_automatically(tmp_path: Path, make_app, monkeypatch) -> None:
    monkeypatch.setattr(web, "MONITOR_DEBOUNCE_SECONDS", 0.05)
    monkeypatch.setattr(web, "MONITOR_BUSY_RETRY_SECONDS", 0.05)
    inbox = tmp_path / "inbox"
    original = _image(inbox / "original.png")
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Imports", _settings(inbox))
    _run_job(client, app, job_id)
    response = client.post("/api/jobs/monitor", json={"id": job_id, "enabled": True},
                           headers=_headers(app))
    assert response.status_code == 200, response.get_json()

    arrival = inbox / "arrival.png"
    arrival.write_bytes(original.read_bytes())
    deadline = time.monotonic() + 6
    seen_done = False
    while time.monotonic() < deadline:
        status = client.get("/api/status").get_json()
        if not status["scanning"] and "1 new or changed files" in status["progress"]["message"]:
            seen_done = True
            break
        time.sleep(0.02)
    assert seen_done, client.get("/api/status").get_json()
    (group,) = _groups(client)
    assert {m["path"] for m in group["members"]} == {str(original), str(arrival)}

    client.post("/api/jobs/monitor", json={"id": job_id, "enabled": False}, headers=_headers(app))


def test_monitor_preserves_unfinished_review_and_merges_arrival_group(
    tmp_path: Path, make_app, monkeypatch
) -> None:
    monkeypatch.setattr(web, "MONITOR_DEBOUNCE_SECONDS", 0.05)
    monkeypatch.setattr(web, "MONITOR_BUSY_RETRY_SECONDS", 0.05)
    inbox = tmp_path / "inbox"
    first = _image(inbox / "first.png")
    second = _image(inbox / "second.png")
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Imports", _settings(inbox))
    _run_job(client, app, job_id)
    (group,) = _groups(client)
    assert {m["path"] for m in group["members"]} == {str(first), str(second)}

    response = client.post("/api/jobs/monitor", json={"id": job_id, "enabled": True},
                           headers=_headers(app))
    assert response.status_code == 200, response.get_json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = client.get("/api/jobs").get_json()["jobs"][0]
        if payload.get("monitor_last_scan_at"):
            break
        time.sleep(0.02)
    assert payload.get("monitor_last_scan_at")
    (group,) = _groups(client)
    assert {m["path"] for m in group["members"]} == {str(first), str(second)}

    third = _image(inbox / "third.png")
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        status = client.get("/api/status").get_json()
        if not status["scanning"] and "1 new or changed files" in status["progress"]["message"]:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"monitor scan did not finish: {client.get('/api/status').get_json()}")
    (group,) = _groups(client)
    assert {m["path"] for m in group["members"]} == {str(first), str(second), str(third)}
    client.post("/api/jobs/monitor", json={"id": job_id, "enabled": False}, headers=_headers(app))


def test_monitor_waits_instead_of_scanning_when_a_folder_is_missing(
    tmp_path: Path, make_app, monkeypatch
) -> None:
    monkeypatch.setattr(web, "MONITOR_DEBOUNCE_SECONDS", 0.05)
    monkeypatch.setattr(web, "MONITOR_BUSY_RETRY_SECONDS", 0.05)
    inbox = tmp_path / "inbox"
    side = tmp_path / "side"
    _image(inbox / "one.png")
    _image(side / "two.png")
    settings = _settings(inbox)
    settings["paths"].append(str(side))
    app = make_app()
    client = app.test_client()
    job_id = _save_job(client, app, "Imports", settings)
    _run_job(client, app, job_id)
    response = client.post("/api/jobs/monitor", json={"id": job_id, "enabled": True},
                           headers=_headers(app))
    assert response.status_code == 200, response.get_json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = client.get("/api/jobs").get_json()["jobs"][0]
        if payload.get("monitor_last_scan_at"):
            break
        time.sleep(0.02)
    assert payload.get("monitor_last_scan_at")

    moved = tmp_path / "side-away"
    side.rename(moved)
    app.extensions["dedupe_monitor"].on_change(job_id)

    payload = client.get("/api/jobs").get_json()["jobs"][0]
    assert "Waiting for missing folder" in payload["monitor_error"]
    assert str(side) in payload["monitor_error"]
    assert not client.get("/api/status").get_json()["scanning"]
    client.post("/api/jobs/monitor", json={"id": job_id, "enabled": False}, headers=_headers(app))


def test_monitor_recovery_after_missing_drive_marks_runtime_active(
    tmp_path: Path, make_app, monkeypatch
) -> None:
    monkeypatch.setattr(web, "MONITOR_DEBOUNCE_SECONDS", 0.05)
    monkeypatch.setattr(web, "MONITOR_BUSY_RETRY_SECONDS", 0.05)
    drive = tmp_path / "drive"
    inbox = drive / "inbox"
    _image(inbox / "one.png")
    store = JobStore(tmp_path / "review.json")
    job = store.save("Drive", _settings(inbox))
    result = _scan(inbox)
    result.job_id = job["id"]
    store.finish_scan(result)
    store.set_monitor(job["id"], True)

    moved = tmp_path / "drive-away"
    drive.rename(moved)
    app = make_app()
    client = app.test_client()
    payload = client.get("/api/jobs").get_json()["jobs"][0]
    assert payload["monitor"] is True
    assert payload["monitor_active"] is False
    assert "No existing folders" in payload["monitor_error"]

    moved.rename(drive)
    app.extensions["dedupe_monitor"].refresh(job["id"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = client.get("/api/jobs").get_json()["jobs"][0]
        if payload.get("monitor_last_scan_at"):
            break
        time.sleep(0.02)
    assert payload.get("monitor_last_scan_at")
    assert payload["monitor_active"] is True
    assert payload["monitor_error"] is None
