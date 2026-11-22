"""Semantic look-alikes through a stand-in for DigUp's ``digup worker`` helper."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from dedupe.engine import run_scan
from dedupe.grouping import apply_smart_select, apply_smart_select_all
from dedupe.models import FileRecord, GroupKind, MediaType, ScanResult, SmartRule
from dedupe.semantic import (
    HELPER_ENV,
    EmbeddingCache,
    EmbeddingWorker,
    embed_records,
    find_digup_helper,
    find_semantic_groups,
    semantic_neighbors,
)
from dedupe.web.app import create_app

# Speaks the worker protocol. An image's vector is one-hot on its average
# colour, quantized to 4 levels per channel: same colour family -> cosine 1,
# different -> 0. Every embedded input is appended to $FAKE_DIGUP_LOG.
FAKE_HELPER = r'''
import base64, json, os, struct, sys
from PIL import Image
assert sys.argv[1:] == ["worker"], sys.argv
print("llama.cpp chatter that is not JSON", flush=True)
print(json.dumps({"ready": True, "info": {"model": "fake/gemma", "revision": "abc",
      "runtime": "fake", "dtype": "f32", "dim": 64}}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request["op"] == "quit":
        print(json.dumps({"id": request["id"], "ok": True}), flush=True)
        break
    floats, errors = [], []
    for path in request["inputs"]:
        with open(os.environ["FAKE_DIGUP_LOG"], "a") as log:
            log.write(path + "\n")
        r, g, b = Image.open(path).convert("RGB").resize((1, 1)).getpixel((0, 0))
        vector = [0.0] * 64
        vector[(r // 64) * 16 + (g // 64) * 4 + b // 64] = 1.0
        floats += vector
        errors.append(None)
    data = base64.b64encode(struct.pack("<%df" % len(floats), *floats)).decode()
    print(json.dumps({"id": request["id"], "ok": True, "dim": 64,
                      "count": len(request["inputs"]), "vectors": data,
                      "errors": errors}), flush=True)
'''


@pytest.fixture
def fake_helper(tmp_path: Path, monkeypatch) -> Path:
    script = tmp_path / "fake_digup.py"
    script.write_text(FAKE_HELPER)
    helper = tmp_path / "digup"
    helper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    helper.chmod(0o755)
    log = tmp_path / "embedded.log"
    monkeypatch.setenv(HELPER_ENV, str(helper))
    monkeypatch.setenv("FAKE_DIGUP_LOG", str(log))
    return log


def _image(path: Path, color: tuple[int, int, int], stripes: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (64, 64), color)
    # Different structure, same overall colour: perceptually unlike, but the
    # fake model sees the same "subject".
    for x in range(64):
        for y in range(0, 64, 8 if stripes else 64):
            img.putpixel((x, y), tuple(min(255, c + 30) for c in color))
    img.save(path, quality=95)
    return path


def _media(root: Path) -> Path:
    _image(root / "beach.jpg", (20, 120, 220), stripes=False)
    _image(root / "beach-edit.jpg", (30, 110, 210), stripes=False)
    _image(root / "forest.png", (30, 200, 40), stripes=False)
    return root


def test_helper_override_must_be_executable(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "nope"
    monkeypatch.setenv(HELPER_ENV, str(missing))
    assert find_digup_helper() is None


def test_worker_embeds_and_skips_non_json_lines(tmp_path: Path, fake_helper: Path) -> None:
    image = _image(tmp_path / "a.jpg", (200, 10, 10), stripes=False)
    with EmbeddingWorker() as worker:
        assert worker.dim == 64
        assert worker.signature.startswith("fake/gemma|abc|fake|f32|")
        vectors, errors = worker.embed_images([str(image)])
    assert errors == [None]
    assert vectors[0] is not None and len(vectors[0]) == 64 * 4


def test_scan_groups_look_alikes_and_reuses_cached_vectors(
    tmp_path: Path, fake_helper: Path
) -> None:
    root = _media(tmp_path / "media")
    cache = tmp_path / "cache.sqlite3"

    result = run_scan([root], similar=False, semantic=True, cache_path=cache)

    (beach_set,) = [g for g in result.groups if g.kind == GroupKind.SETS]
    assert [Path(m.path).name for m in beach_set.members][1:] in (
        ["beach.jpg"], ["beach-edit.jpg"]
    )
    assert beach_set.selected_for_removal == [] and beach_set.suggested_keep is None
    similar = [g for g in result.groups if g.kind == GroupKind.SIMILAR]
    assert len(similar) == 1
    group = similar[0]
    assert group.match_basis == "semantic"
    assert {Path(m.path).name for m in group.members} == {"beach.jpg", "beach-edit.jpg"}
    (other,) = [m.path for m in group.members if m.path != group.suggested_keep]
    assert group.match_scores == {other: pytest.approx(1.0)}
    assert group.selected_for_removal == []  # look-alikes are opt-in, never preselected
    apply_smart_select_all(result.groups, SmartRule.AUTOMATIC)  # as `dedupe scan` does
    assert group.selected_for_removal == []
    apply_smart_select(group, SmartRule.NEWEST)  # a rule chosen for this one group applies
    assert len(group.selected_for_removal) == 1
    apply_smart_select(group, SmartRule.DESELECT_ALL)
    assert result.diagnostics.stages["semantic"].attempted == 3
    assert len(fake_helper.read_text().splitlines()) == 3

    again = run_scan([root], similar=False, semantic=True, cache_path=cache)
    assert [g.id for g in again.groups if g.kind == GroupKind.SIMILAR] == [group.id]
    assert [g.id for g in again.groups if g.kind == GroupKind.SETS] == [beach_set.id]
    assert len(fake_helper.read_text().splitlines()) == 3  # all from the cache

    restored = ScanResult.from_dict(again.to_dict())
    (restored_group,) = [g for g in restored.groups if g.kind == GroupKind.SIMILAR]
    assert restored_group.match_basis == "semantic"
    assert restored_group.match_scores == {other: pytest.approx(1.0)}


def _records(root: Path) -> list[FileRecord]:
    return [
        FileRecord(path=str(p), size=p.stat().st_size, mtime=p.stat().st_mtime,
                   media_type=MediaType.IMAGE, extension=p.suffix)
        for p in sorted(root.iterdir())
    ]


def test_files_in_perceptual_groups_and_exact_copies_are_left_out(
    tmp_path: Path, fake_helper: Path
) -> None:
    root = _media(tmp_path / "media")
    copy = root / "beach-copy.jpg"
    copy.write_bytes((root / "beach.jpg").read_bytes())
    records = _records(root)
    exact = [{str(root / "beach.jpg"), str(copy)}]

    sets, duplicates, errors = find_semantic_groups(
        records, exact_path_sets=exact, use_cache=False
    )
    assert errors == []
    for layer in (sets, duplicates):
        (members, _scores), = layer
        assert "beach-edit.jpg" in {Path(m.path).name for m in members}

    # Identical copies alone are the exact layer's business, not a look-alike;
    # a file a perceptual group holds still joins a Set but not a duplicate.
    edit = str(root / "beach-edit.jpg")
    sets, duplicates, _ = find_semantic_groups(
        [r for r in records if r.path != edit],
        exact_path_sets=exact,
        use_cache=False,
    )
    assert sets == [] and duplicates == []
    sets, duplicates, _ = find_semantic_groups(
        records, exclude_from_duplicates={edit}, exact_path_sets=exact, use_cache=False
    )
    assert len(sets) == 1 and duplicates == []


def test_duplicates_need_agreeing_brightness(tmp_path: Path, fake_helper: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    Image.new("RGB", (64, 64), (128, 128, 128)).save(root / "gray.png")
    split = Image.new("RGB", (64, 64), (0, 0, 0))
    split.paste((255, 255, 255), (0, 0, 64, 32))
    split.save(root / "split.png")  # same average colour, opposite tones

    sets, duplicates, _ = find_semantic_groups(_records(root), use_cache=False)

    assert len(sets) == 1 and duplicates == []


def test_missing_digup_is_a_warning_not_a_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(HELPER_ENV, str(tmp_path / "missing"))
    root = _media(tmp_path / "media")

    result = run_scan([root], similar=False, semantic=True, use_cache=False)

    assert not [g for g in result.groups if g.kind == GroupKind.SIMILAR]
    stage = result.diagnostics.stages["semantic"]
    assert stage.attempted == 0
    assert stage.warnings and "DigUp is not installed" in stage.warnings[0]


def test_web_payload_labels_semantic_members(tmp_path: Path, fake_helper: Path) -> None:
    root = _media(tmp_path / "media")
    result = run_scan([root], similar=False, semantic=True, use_cache=False)
    client = create_app(result).test_client()

    payload = client.get("/api/groups?kind=similar").get_json()
    groups = payload["groups"] if isinstance(payload, dict) else payload
    (group,) = [g for g in groups if g["kind"] == "similar"]
    assert group["match_basis"] == "semantic"
    labels = {
        os.path.basename(m["path"]): m.get("match_label")
        for m in group["members"]
    }
    keeper = os.path.basename(group["suggested_keep"])
    assert labels.pop(keeper) is None
    assert list(labels.values()) == ["100% look-alike"]
    assert all(m["similarity_percent"] is None for m in group["members"])

    (sets_group,) = client.get("/api/groups?kind=sets").get_json()["groups"]
    assert sets_group["policy"] == "independent_candidates"
    assert [m.get("match_label") for m in sets_group["members"]] == [
        None, "100% like the first image"
    ]


def test_set_member_can_be_previewed_for_one_click_trash(
    tmp_path: Path, fake_helper: Path
) -> None:
    root = _media(tmp_path / "media")
    result = run_scan([root], similar=False, semantic=True, use_cache=False)
    (sets_group,) = [g for g in result.groups if g.kind == GroupKind.SETS]
    app = create_app(result)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    target = sets_group.members[1].path

    preview = client.post(
        "/api/review-candidate/delete",
        json={"group_id": sets_group.id, "path": target, "scan_id": scan_id},
        headers=headers,
    )

    assert preview.status_code == 200
    assert preview.get_json()["success_count"] == 1
    assert Path(target).exists()


def test_scan_form_thresholds_reach_the_engine(tmp_path: Path, monkeypatch) -> None:
    import dedupe.web.app as web

    seen: dict = {}

    def fake_scan(roots, **kwargs):
        seen.update(kwargs)
        return ScanResult(roots=[str(r) for r in roots], files=[], groups=[])

    monkeypatch.setattr(web, "run_scan", fake_scan)
    app = create_app(None)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    response = client.post(
        "/api/scan",
        json={
            "paths": [str(tmp_path)],
            "parallel": False,
            "semantic": True,
            "semantic_threshold": 0.93,
            "semantic_duplicate_threshold": 5,  # clamped
        },
        headers=headers,
    )
    assert response.status_code == 200
    for _ in range(200):
        if seen:
            break
        time.sleep(0.01)
    assert seen["semantic"] is True
    assert seen["semantic_threshold"] == pytest.approx(0.93)
    assert seen["semantic_duplicate_threshold"] == pytest.approx(0.999)


def test_judge_cannot_approve_pairs_without_local_evidence(
    tmp_path: Path, fake_helper: Path
) -> None:
    root = tmp_path / "media"
    root.mkdir()
    Image.new("RGB", (64, 64), (128, 128, 128)).save(root / "gray.png")
    split = Image.new("RGB", (64, 64), (0, 0, 0))
    split.paste((255, 255, 255), (0, 0, 64, 32))
    split.save(root / "split.png")  # brightness alone would reject this pair
    asked: list[tuple[str, str]] = []

    def judge(a: FileRecord, b: FileRecord) -> float:
        asked.append((Path(a.path).name, Path(b.path).name))
        return 0.85

    sets, duplicates, _ = find_semantic_groups(_records(root), use_cache=False, judge=judge)
    assert len(sets) == 1 and duplicates == []
    assert asked == []

    # No confidence threshold or unavailable judge bypasses the local veto.
    _, duplicates, _ = find_semantic_groups(
        _records(root), use_cache=False, judge=judge, judge_min=0.9
    )
    assert duplicates == []
    _, duplicates, _ = find_semantic_groups(
        _records(root), use_cache=False, judge=lambda _a, _b: None
    )
    assert duplicates == []


def test_judge_cannot_override_an_explicit_detail_veto(
    tmp_path: Path, fake_helper: Path, monkeypatch
) -> None:
    root = _media(tmp_path / "media")
    monkeypatch.setattr("dedupe.semantic.edited_copy_verdict", lambda *_args: False)

    def judge(*_args):
        pytest.fail("A local difference must be final, even if the model would say 100%")

    sets, duplicates, _ = find_semantic_groups(_records(root), use_cache=False, judge=judge)
    assert len(sets) == 1
    assert duplicates == []


def test_same_subject_with_different_detail_stays_only_in_sets(
    tmp_path: Path, fake_helper: Path, monkeypatch
) -> None:
    root = tmp_path / "media"
    _image(root / "a.png", (20, 120, 220), stripes=False)
    _image(root / "b.png", (20, 120, 220), stripes=True)
    # Featureless media / absent OpenCV must not fall back to histogram or
    # embedding similarity alone, regardless of the model's confidence.
    monkeypatch.setattr("dedupe.semantic.edited_copy_verdict", lambda *_args: None)
    for judge in (None, lambda *_args: 1.0):
        sets, duplicates, _ = find_semantic_groups(_records(root), use_cache=False, judge=judge)
        assert len(sets) == 1
        assert duplicates == []


def test_fallback_checks_pixels_again_after_another_scan(
    tmp_path: Path, fake_helper: Path, monkeypatch
) -> None:
    root = _media(tmp_path / "media")
    monkeypatch.setattr("dedupe.semantic.edited_copy_verdict", lambda *_args: None)
    _, duplicates, _ = find_semantic_groups(_records(root), use_cache=False)
    assert len(duplicates) == 1
    changed = root / "beach-edit.jpg"
    with Image.open(changed) as image:
        image = image.convert("RGB")
    image.paste((200, 80, 40), (20, 20, 36, 36))
    image.save(changed, quality=95)
    sets, duplicates, _ = find_semantic_groups(_records(root), use_cache=False)
    assert len(sets) == 1
    assert duplicates == []


def test_judge_can_veto_a_pair_brightness_would_accept(
    tmp_path: Path, fake_helper: Path
) -> None:
    root = _media(tmp_path / "media")

    _, duplicates, _ = find_semantic_groups(
        _records(root), use_cache=False, judge=lambda _a, _b: 0.05
    )

    assert duplicates == []


def test_decider_not_running_is_a_warning_and_falls_back(
    tmp_path: Path, fake_helper: Path, monkeypatch
) -> None:
    from dedupe.decider import URL_ENV

    monkeypatch.setenv(URL_ENV, "http://127.0.0.1:9")
    root = _media(tmp_path / "media")

    result = run_scan([root], similar=False, semantic=True, decider=True, use_cache=False)

    assert len([g for g in result.groups if g.kind == GroupKind.SIMILAR]) == 1
    warnings = result.diagnostics.stages["semantic"].warnings
    assert any("Decider verification skipped" in w for w in warnings)


def test_scan_form_decider_needs_look_alikes(tmp_path: Path, monkeypatch) -> None:
    import dedupe.web.app as web

    seen: list[dict] = []

    def fake_scan(roots, **kwargs):
        seen.append(kwargs)
        return ScanResult(roots=[str(r) for r in roots], files=[], groups=[])

    monkeypatch.setattr(web, "run_scan", fake_scan)
    for semantic in (True, False):
        app = create_app(None)
        headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
        before = len(seen)
        response = app.test_client().post(
            "/api/scan",
            json={"paths": [str(tmp_path)], "parallel": False,
                  "semantic": semantic, "decider": True},
            headers=headers,
        )
        assert response.status_code == 200
        for _ in range(200):
            if len(seen) > before:
                break
            time.sleep(0.01)
        assert seen[-1]["decider"] is semantic


def _script_helper(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "worker.py"
    script.write_text(source)
    helper = tmp_path / "worker"
    helper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    helper.chmod(0o755)
    return helper


def test_stalled_worker_startup_has_a_deadline_and_is_reaped(tmp_path: Path) -> None:
    pid_path = tmp_path / "pid"
    helper = _script_helper(tmp_path, f"""
import os, time
open({str(pid_path)!r}, 'w').write(str(os.getpid()))
time.sleep(30)
""")
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="retry the deep scan"):
        EmbeddingWorker(str(helper), startup_timeout=0.2)
    assert time.monotonic() - started < 2
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


def test_cancel_interrupts_a_partial_helper_reply_and_reaps_process(tmp_path: Path) -> None:
    helper = _script_helper(tmp_path, """
import json, sys, time
print(json.dumps({'ready': True}), flush=True)
sys.stdin.readline()
print('{', end='', flush=True)
time.sleep(30)
""")
    cancelled = threading.Event()
    with EmbeddingWorker(str(helper), cancelled=cancelled.is_set) as worker:
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(InterruptedError, match="scan cancelled"):
                worker.embed_images(["unused.jpg"])
            assert time.monotonic() - started < 1
            assert worker._process.poll() is not None
        finally:
            timer.cancel()


def test_later_helper_failure_preserves_completed_batches_for_retry(
    tmp_path: Path, monkeypatch,
) -> None:
    from dedupe import semantic

    helper = _script_helper(tmp_path, FAKE_HELPER.replace(
        'floats, errors = [], []',
        'if request["id"] == 2:\n        import time\n        time.sleep(30)\n'
        '    floats, errors = [], []',
    ))
    monkeypatch.setenv("FAKE_DIGUP_LOG", str(tmp_path / "log"))
    monkeypatch.setattr(semantic, "REQUEST_BATCH", 1)
    monkeypatch.setattr(semantic, "REQUEST_TIMEOUT", 0.15)
    records = _records(_media(tmp_path / "media"))
    cache = tmp_path / "cache.db"
    vectors, errors = embed_records(records, helper=str(helper), cache_path=cache)
    # Worker restarts once after batch id=2 times out, completing the third item.
    assert len(vectors) == 2 and len(errors) == 1
    helper = _script_helper(tmp_path, FAKE_HELPER)
    vectors, errors = embed_records(records, helper=str(helper), cache_path=cache)
    assert len(vectors) == 3 and errors == []
    assert len((tmp_path / "log").read_text().splitlines()) == 3


def test_embeddings_follow_renames_and_copies(
    tmp_path: Path, fake_helper: Path,
) -> None:
    root = _media(tmp_path / "media")
    cache = tmp_path / "cache.db"
    embed_records(_records(root), cache_path=cache)
    (root / "beach.jpg").rename(root / "renamed.jpg")
    (root / "copied.jpg").write_bytes((root / "forest.png").read_bytes())
    vectors, errors = embed_records(_records(root), cache_path=cache)
    assert len(vectors) == 4 and errors == []
    assert len(fake_helper.read_text().splitlines()) == 3


def test_embedding_cache_invalidates_content_change_with_preserved_size_and_mtime(
    tmp_path: Path,
) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(b"aaa")
    stat = path.stat()
    record = _records(tmp_path)[0]
    cache = EmbeddingCache(tmp_path / "cache.db")
    try:
        cache.put_many([(record, b"vector")], "model")
        assert cache.get_many([record], "model") == {str(path): b"vector"}
        path.write_bytes(b"bbb")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert cache.get_many([record], "model") == {}
    finally:
        cache.close()


def test_incremental_neighbors_match_full_scan_and_only_compare_new_vectors(
    tmp_path: Path, monkeypatch,
) -> None:
    import numpy as np

    from dedupe import semantic

    rng = np.random.default_rng(57)
    vectors = {f"p{i}": rng.random(12).astype("<f4").tobytes() for i in range(6)}
    cache = tmp_path / "cache.db"
    work: list[int] = []
    real = semantic._cosine_block

    def measured(rows, matrix):
        work.append(len(rows) * len(matrix))
        return real(rows, matrix)

    monkeypatch.setattr(semantic, "_cosine_block", measured)

    def check(current, expected_work, threshold=0.7, same_set=None):
        full = semantic_neighbors(current, threshold, same_set=same_set)
        work.clear()
        result = semantic_neighbors(current, threshold, same_set=same_set,
                                    use_cache=True, cache_path=cache)
        assert result[0] == full[0]
        assert result[1] == pytest.approx(full[1])
        assert sum(work) == expected_work

    check(vectors, 36)
    renamed = {f"renamed/{p}": v for p, v in vectors.items()}
    check(renamed, 0)
    renamed["new"] = rng.random(12).astype("<f4").tobytes()
    check(renamed, 7)
    renamed["renamed/p0"] = rng.random(12).astype("<f4").tobytes()
    check(renamed, 7)
    check(renamed, 0, threshold=0.8)
    check(renamed, 49, threshold=0.6)
    renamed["copy"] = renamed["new"]
    check(renamed, 0)
    check(renamed, 0, same_set={"copy": 0, "new": 0})


def test_cancelled_neighbor_pass_does_not_publish_incomplete_coverage(
    tmp_path: Path, monkeypatch,
) -> None:
    import numpy as np

    from dedupe import semantic

    vectors = {f"p{i}": np.array([1, i, i / 2], dtype="<f4").tobytes() for i in range(5)}
    cache = tmp_path / "cache.db"
    monkeypatch.setattr(semantic, "SIMILARITY_BLOCK", 1)
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 2

    with pytest.raises(InterruptedError):
        semantic_neighbors(vectors, 0.5, use_cache=True, cache_path=cache, cancelled=cancelled)
    full = semantic_neighbors(vectors, 0.5)
    retried = semantic_neighbors(vectors, 0.5, use_cache=True, cache_path=cache)
    assert retried == full


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS background policy")
def test_helper_runs_under_the_macos_background_policy(
    tmp_path: Path, fake_helper: Path,
) -> None:
    from dedupe.semantic import TASKPOLICY

    with EmbeddingWorker() as worker:
        assert worker._process.args[:2] == [TASKPOLICY, "-b"]
        # taskpolicy execs in place: the tracked pid is the helper itself.
        policy = os.popen(f"/bin/ps -o comm= -p {worker._process.pid}").read()
        assert TASKPOLICY not in policy
        vectors, _errors = worker.embed_images([str(_image(tmp_path / "a.jpg", (9, 9, 9), False))])
    assert vectors[0] is not None


def test_thermal_pause_is_a_duty_cycle() -> None:
    from dedupe.semantic import THERMAL_PAUSE_MIN, thermal_pause_seconds, thermal_pressure

    assert thermal_pause_seconds(0, 5.0) == 0
    assert thermal_pause_seconds(1, 5.0) == 5.0
    assert thermal_pause_seconds(1, 0.1) == THERMAL_PAUSE_MIN
    assert thermal_pause_seconds(2, 5.0) == 15.0
    assert thermal_pause_seconds(4, 1.0) > thermal_pause_seconds(2, 1.0)
    assert thermal_pressure() >= 0  # readable (or 0) on this machine


def test_embedding_pauses_under_thermal_pressure_and_still_finishes(
    tmp_path: Path, fake_helper: Path, monkeypatch,
) -> None:
    from dedupe import semantic

    monkeypatch.setattr(semantic, "REQUEST_BATCH", 1)
    monkeypatch.setattr(semantic, "THERMAL_PAUSE_MIN", 0.05)
    monkeypatch.setattr(semantic, "thermal_pressure", lambda: 1)
    phases: list[str] = []
    vectors, errors = embed_records(
        _records(_media(tmp_path / "media")), use_cache=False,
        progress=lambda phase, _done, _total: phases.append(phase),
    )
    assert len(vectors) == 3 and errors == []
    # Paused after the first and second batches, never after the last.
    assert phases.count("semantic-embed-cooling") == 2
    assert phases[-1] == "semantic-embed"


def test_cancel_during_a_thermal_pause_stops_promptly(
    tmp_path: Path, fake_helper: Path, monkeypatch,
) -> None:
    from dedupe import semantic

    monkeypatch.setattr(semantic, "REQUEST_BATCH", 1)
    monkeypatch.setattr(semantic, "THERMAL_PAUSE_MIN", 30.0)
    monkeypatch.setattr(semantic, "thermal_pressure", lambda: 2)
    cancelled = threading.Event()

    def progress(phase: str, _done: int, _total: int) -> None:
        if phase == "semantic-embed-cooling":
            threading.Timer(0.1, cancelled.set).start()

    started = time.monotonic()
    with pytest.raises(InterruptedError):
        embed_records(
            _records(_media(tmp_path / "media")), use_cache=False,
            progress=progress, cancelled=cancelled.is_set,
        )
    assert time.monotonic() - started < 5


def test_embedding_starts_before_image_hashing_finishes(
    tmp_path: Path, fake_helper: Path, monkeypatch,
) -> None:
    from dedupe import engine

    embedding_started = threading.Event()
    real_embed = engine.embed_records
    real_similar = engine.find_similar_image_groups
    overlapped: list[bool] = []

    def embed(*args, **kwargs):
        embedding_started.set()
        return real_embed(*args, **kwargs)

    def similar(*args, **kwargs):
        # Before, embedding waited for this stage; now it runs alongside it.
        overlapped.append(embedding_started.wait(timeout=10))
        return real_similar(*args, **kwargs)

    monkeypatch.setattr(engine, "embed_records", embed)
    monkeypatch.setattr(engine, "find_similar_image_groups", similar)
    root = _media(tmp_path / "media")
    result = run_scan([root], semantic=True, cache_path=tmp_path / "cache.sqlite3")
    assert overlapped == [True]
    assert [g for g in result.groups if g.kind == GroupKind.SETS]
    assert not result.diagnostics.stages["semantic"].warnings


def test_fully_cached_scan_never_starts_the_helper(
    tmp_path: Path, fake_helper: Path, monkeypatch,
) -> None:
    starts = tmp_path / "starts.log"
    script = tmp_path / "fake_digup.py"
    script.write_text(script.read_text().replace(
        'print("llama.cpp chatter',
        'open(os.environ["FAKE_DIGUP_STARTS"], "a").write("start\\n")\nprint("llama.cpp chatter',
    ))
    monkeypatch.setenv("FAKE_DIGUP_STARTS", str(starts))
    root = _media(tmp_path / "media")
    cache = tmp_path / "cache.sqlite3"

    first, _ = embed_records(_records(root), cache_path=cache)
    again, errors = embed_records(_records(root), cache_path=cache)
    assert again == first and errors == []
    assert starts.read_text().splitlines() == ["start"]  # loaded the model once

    _image(root / "new.jpg", (200, 30, 30), stripes=False)
    vectors, _ = embed_records(_records(root), cache_path=cache)
    assert len(vectors) == 4 and len(starts.read_text().splitlines()) == 2
    assert len(fake_helper.read_text().splitlines()) == 4  # only the new file embedded


def test_a_changed_helper_reporting_a_new_model_reembeds(
    tmp_path: Path, fake_helper: Path,
) -> None:
    root = _media(tmp_path / "media")
    cache = tmp_path / "cache.sqlite3"
    embed_records(_records(root), cache_path=cache)
    # An app update replaces the helper; its model revision changed too.
    script = tmp_path / "fake_digup.py"
    script.write_text(script.read_text().replace('"revision": "abc"', '"revision": "def"'))
    helper = tmp_path / "digup"
    helper.write_text(helper.read_text() + "\n# updated\n")
    vectors, errors = embed_records(_records(root), cache_path=cache)
    assert len(vectors) == 3 and errors == []
    assert len(fake_helper.read_text().splitlines()) == 6  # all three embedded again


def test_full_speed_option_reaches_the_helper_launch(tmp_path: Path, monkeypatch) -> None:
    import dedupe.web.app as web
    from dedupe.semantic import TASKPOLICY, _worker_command

    assert _worker_command("/x/digup", background=False) == ["/x/digup", "worker"]
    if sys.platform == "darwin":
        assert _worker_command("/x/digup")[:2] == [TASKPOLICY, "-b"]

    seen: list[dict] = []
    monkeypatch.setattr(web, "run_scan", lambda roots, **kw: seen.append(kw) or ScanResult(
        roots=[str(r) for r in roots], files=[], groups=[]))
    app = create_app(None)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    for body, expected in (
        ({"semantic": True, "semantic_full_speed": True}, True),
        ({"semantic": False, "semantic_full_speed": True}, False),  # needs look-alikes
        ({"semantic": True}, False),  # cool by default
    ):
        seen.clear()
        response = client.post(
            "/api/scan", json={"paths": [str(tmp_path)], "parallel": False, **body},
            headers=headers,
        )
        assert response.status_code == 200
        for _ in range(200):
            if seen:
                break
            time.sleep(0.01)
        assert seen[0]["semantic_full_speed"] is expected
        for _ in range(200):
            if client.get("/api/status").get_json().get("state") != "running":
                break
            time.sleep(0.01)


def test_default_duplicate_threshold_is_092(tmp_path: Path, monkeypatch) -> None:
    """DEFAULT_DUPLICATE_THRESHOLD is 0.92 and separates 0.93 copies from 0.90 look-alikes."""
    import numpy as np

    from dedupe import semantic

    assert semantic.DEFAULT_DUPLICATE_THRESHOLD == pytest.approx(0.92)
    monkeypatch.setattr(semantic, "edited_copy_verdict", lambda *_a, **_kw: True)

    def _unit_at(angle: float) -> bytes:
        vec = np.zeros(8, dtype=np.float32)
        vec[0] = float(np.cos(angle))
        vec[1] = float(np.sin(angle))
        return vec.tobytes()

    pa = _image(tmp_path / "a.png", (40, 80, 120), False)
    pb = _image(tmp_path / "b.png", (40, 80, 120), False)
    pc = _image(tmp_path / "c.png", (40, 80, 120), False)
    recs = _records(tmp_path)
    vectors = {
        str(pa): _unit_at(0.0),
        str(pb): _unit_at(float(np.arccos(0.93))),
        str(pc): _unit_at(float(-np.arccos(0.90))),
    }
    sets, duplicates = semantic.group_semantic_vectors(
        recs, vectors, use_cache=False, workers=1
    )
    assert len(sets) == 1
    assert len(duplicates) == 1
    assert {r.path for r in duplicates[0][0]} == {str(pa), str(pb)}


def test_excluded_pairs_suppresses_edge_without_banning_files(tmp_path: Path, monkeypatch) -> None:
    """excluded_pairs removes specific edges while still letting those files match others."""
    import numpy as np

    from dedupe import semantic

    monkeypatch.setattr(semantic, "edited_copy_verdict", lambda *_a, **_kw: True)
    pa = _image(tmp_path / "a.png", (40, 80, 120), False)
    pb = _image(tmp_path / "b.png", (40, 80, 120), False)
    pc = _image(tmp_path / "c.png", (40, 80, 120), False)
    recs = _records(tmp_path)

    # cos(a, b) = 0.96, cos(a, c) = 0.95, cos(b, c) = 0.83 -> only a connects b and c.
    va = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    ang_ab = float(np.arccos(0.96))
    ang_ac = float(np.arccos(0.95))
    vb = np.array([np.cos(ang_ab), np.sin(ang_ab), 0.0], dtype=np.float32)
    vc = np.array([np.cos(ang_ac), -np.sin(ang_ac), 0.0], dtype=np.float32)
    vectors = {str(pa): va.tobytes(), str(pb): vb.tobytes(), str(pc): vc.tobytes()}

    _sets, duplicates = semantic.group_semantic_vectors(
        recs,
        vectors,
        excluded_pairs={(str(pa), str(pb))},
        use_cache=False,
        workers=1,
    )
    assert len(duplicates) == 1
    assert {r.path for r in duplicates[0][0]} == {str(pa), str(pc)}


def test_embedding_cache_skips_sha256_on_empty_sig_and_reuses_record_sha256(
    tmp_path: Path, monkeypatch
) -> None:
    """get_many skips SHA-256 hashing when a signature has no rows and reuses record.sha256."""
    media = _media(tmp_path / "media")
    recs = _records(media)
    cache = EmbeddingCache(tmp_path / "cache.db")
    try:
        sha_calls = 0

        def boom_sha(*_args, **_kwargs):
            nonlocal sha_calls
            sha_calls += 1
            raise AssertionError("_digest should not run here")

        monkeypatch.setattr(cache, "_digest", boom_sha)
        assert cache.get_many(recs, "empty-sig") == {}
        assert sha_calls == 0

        # Store using known sha256 on record, then query another path carrying the same record.sha256.
        vec = b"\x01\x02\x03\x04"
        recs[0].sha256 = "known-sha-abc"
        cache.put_many([(recs[0], vec)], "sig-1")
        moved = _records(media)[0]
        moved.path = str(tmp_path / "moved.jpg")
        Path(moved.path).write_bytes(Path(recs[0].path).read_bytes())
        moved.sha256 = "known-sha-abc"
        hit = cache.get_many([moved], "sig-1")
        assert hit == {moved.path: vec}
        assert sha_calls == 0
    finally:
        cache.close()


def test_neighbor_cache_multi_corpus_wal_and_large_chunked_graph(tmp_path: Path) -> None:
    """NeighborCache enables WAL, isolates corpora, and round-trips >250k edges cleanly."""
    from dedupe.semantic import NeighborCache

    db = tmp_path / "neighbors.db"
    cache = NeighborCache(db)
    try:
        mode = cache._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"

        keys_a = ["sha-a0", "sha-a1", "sha-a2"]
        keys_b = ["sha-b0", "sha-b1", "sha-b2"]
        cache.save(keys_a, {("sha-a0", "sha-a1"): 0.94}, 0.88)
        cache.save(keys_b, {("sha-b0", "sha-b2"): 0.91}, 0.88)

        loaded_keys_a, edges_a = cache.load(0.88, keys_a)
        loaded_keys_b, edges_b = cache.load(0.88, keys_b)
        assert loaded_keys_a == set(keys_a)
        assert edges_a == {("sha-a0", "sha-a1"): pytest.approx(0.94)}
        assert loaded_keys_b == set(keys_b)
        assert edges_b == {("sha-b0", "sha-b2"): pytest.approx(0.91)}

        # Save and reload >250k edges across multiple chunks (EDGE_CHUNK_SIZE = 100k).
        n_keys = 725
        big_keys = [f"k{i:04d}" for i in range(n_keys)]
        big_edges: dict[tuple[str, str], float] = {}
        count = 0
        for i in range(n_keys):
            for j in range(i + 1, n_keys):
                big_edges[(big_keys[i], big_keys[j])] = 0.92
                count += 1
                if count == 255_000:
                    break
            if count == 255_000:
                break
        cache.save(big_keys, big_edges, 0.88)
        cache.close()

        reopened = NeighborCache(db)
        try:
            chunks = reopened._conn.execute(
                "SELECT count(*) FROM semantic_neighbor_chunks WHERE corpus_hash = ?",
                (reopened._corpus_hash(big_keys),),
            ).fetchone()[0]
            assert chunks == 3
            reloaded_keys, reloaded_edges = reopened.load(0.88, big_keys)
            assert reloaded_keys == set(big_keys)
            assert len(reloaded_edges) == 255_000
        finally:
            reopened.close()
    finally:
        cache.close()


def test_embed_records_bisects_poisoned_batch_to_isolate_failing_item(
    tmp_path: Path, monkeypatch,
) -> None:
    """Multi-item batch failure bisects down to isolate only the poisoned file."""
    from dedupe import semantic

    media = tmp_path / "media"
    _image(media / "good1.jpg", (20, 120, 220), False)
    Image.new("RGB", (64, 64), (255, 0, 255)).save(media / "poison.jpg", quality=95)
    _image(media / "good2.jpg", (30, 200, 40), False)
    _image(media / "good3.jpg", (200, 40, 40), False)

    poison_hook = (
        "def _is_poison(p):\n"
        '        r, g, b = Image.open(p).convert("RGB").resize((1, 1)).getpixel((0, 0))\n'
        "        return r > 220 and g < 30 and b > 220\n"
        '    if any(_is_poison(p) for p in request["inputs"]):\n'
        "        import time\n"
        "        time.sleep(30)\n"
        "    floats, errors = [], []"
    )
    helper = _script_helper(
        tmp_path,
        FAKE_HELPER.replace("floats, errors = [], []", poison_hook),
    )
    monkeypatch.setenv("FAKE_DIGUP_LOG", str(tmp_path / "log"))
    monkeypatch.setattr(semantic, "REQUEST_BATCH", 4)
    monkeypatch.setattr(semantic, "REQUEST_TIMEOUT", 0.15)
    monkeypatch.setattr(semantic, "MAX_CONSECUTIVE_WORKER_FAILURES", 10)

    records = _records(media)
    vectors, errors = embed_records(records, helper=str(helper), cache_path=tmp_path / "cache.db")
    assert len(vectors) == 3
    assert all(not p.endswith("poison.jpg") for p in vectors)
    assert len(errors) == 1 and "poison.jpg" in errors[0]


def test_mirrored_copy_verified_locally_cannot_be_vetoed_by_judge(
    tmp_path: Path, monkeypatch
) -> None:
    """Locally mirrored image pairs are accepted without letting Decider veto them."""
    import numpy as np

    from dedupe import semantic

    img = Image.new("RGB", (64, 96), "navy")
    from PIL import ImageDraw

    ImageDraw.Draw(img).rectangle((6, 12, 34, 60), fill="gold")
    orig, mir = tmp_path / "orig.png", tmp_path / "mirror.png"
    img.save(orig)
    img.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(mir)

    recs = _records(tmp_path)
    vec = np.array([1.0, 0.0, 0.0], dtype=np.float32).tobytes()
    vectors = {r.path: vec for r in recs}

    judge_calls: list[tuple[str, str]] = []

    def strict_veto_judge(a: FileRecord, b: FileRecord) -> float:
        judge_calls.append((a.path, b.path))
        return 0.01

    monkeypatch.setattr(semantic, "edited_copy_verdict", lambda *_a, **_kw: None)
    _sets, duplicates = semantic.group_semantic_vectors(
        recs, vectors, use_cache=False, workers=1, judge=strict_veto_judge
    )
    assert len(duplicates) == 1
    assert {r.path for r in duplicates[0][0]} == {str(orig), str(mir)}
    assert judge_calls == []
