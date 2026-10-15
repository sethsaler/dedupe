"""Semantic look-alikes through a stand-in for DigUp's ``digup worker`` helper."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from dedupe.engine import run_scan
from dedupe.grouping import apply_smart_select, apply_smart_select_all
from dedupe.models import FileRecord, GroupKind, MediaType, ScanResult, SmartRule
from dedupe.semantic import (
    HELPER_ENV,
    EmbeddingWorker,
    find_digup_helper,
    find_semantic_groups,
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
    _image(root / "beach-edit.jpg", (30, 110, 210), stripes=True)
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
