"""Crop aggressiveness levels: conservative / balanced / aggressive.

Balanced is the pre-levels detector exactly; conservative keeps only what
the strict pass proves; aggressive loosens past balanced (human guard still
enforced). A scan picks the default level whose suggestions become Crop
candidates, every candidate carries all three boxes, and the review applies
the level chosen per file. Skipping a crop dismisses every level.
"""

from __future__ import annotations

import itertools
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from dedupe import borders
from dedupe.borders import (
    BORDER_DISMISSED,
    BORDER_LEVELS_SIGNATURE,
    BORDER_SIGNATURE,
    CropBox,
    analyze_borders,
    detect_crop_levels,
    suggested_crop,
)
from dedupe.cache import HashCache
from dedupe.engine import run_scan
from dedupe.grouping import build_crop_groups
from dedupe.models import FileRecord, GroupKind, MediaType
from dedupe.web import app as web_app
from dedupe.web.app import create_app


def _picture(width: int, height: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.integers(70, 230, size=(height, width, 3), dtype=np.uint8)
    base[:, : width // 2, 0] = 200
    return base


def _save(array: np.ndarray, path: Path) -> Path:
    Image.fromarray(array).save(path)
    return path


def _record(path: Path) -> FileRecord:
    stat = path.stat()
    with Image.open(path) as image:
        width, height = image.size
    return FileRecord(
        path=str(path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        media_type=MediaType.IMAGE,
        extension=path.suffix.lower(),
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
        width=width,
        height=height,
    )


def _framed(width, height, *, top=0, bar_value=3, caption_rows=None) -> np.ndarray:
    canvas = np.full((height, width, 3), bar_value, dtype=np.uint8)
    canvas[top:] = _picture(width, height - top)
    if caption_rows is not None:
        start, stop = caption_rows
        # Bright subtitle glyphs burned into the bar: a few percent of a row.
        for x in range(60, width - 60, 80):
            canvas[start:stop, x : x + 12] = 230
    return canvas


def _contains(outer: CropBox, inner: CropBox) -> bool:
    """``outer`` crops at least as much as ``inner`` on every edge."""
    return (
        outer.left >= inner.left
        and outer.top >= inner.top
        and outer.right <= inner.right
        and outer.bottom <= inner.bottom
    )


# --- detector levels ----------------------------------------------------------


def test_plain_letterbox_agrees_at_every_level(tmp_path: Path) -> None:
    path = _save(_framed(640, 480, top=60), tmp_path / "plain.png")
    levels = detect_crop_levels(path)
    assert set(levels) == {"conservative", "balanced", "aggressive"}
    assert levels["balanced"] == CropBox(0, 60, 640, 480, 640, 480)
    assert levels["conservative"] == levels["balanced"]
    assert levels["aggressive"] == levels["balanced"]


def test_caption_in_the_bar_needs_balanced(tmp_path: Path) -> None:
    # Subtitle ink inside the bar defeats the strict pass; the balanced
    # aggressive pass crops bar and caption together.
    path = _save(
        _framed(640, 480, top=90, caption_rows=(40, 58)), tmp_path / "captioned.png"
    )
    levels = detect_crop_levels(path)
    balanced = levels["balanced"]
    assert balanced is not None and balanced.top >= 85
    conservative = levels["conservative"]
    assert conservative is None or conservative.top < 40
    assert levels["aggressive"] is not None
    assert _contains(levels["aggressive"], balanced)


def test_dark_gray_bar_needs_aggressive(tmp_path: Path) -> None:
    # A flat bar at luma 56 is past the balanced pass's "black" (48) but
    # inside the aggressive level's.
    path = _save(_framed(640, 480, top=70, bar_value=56), tmp_path / "gray.png")
    levels = detect_crop_levels(path)
    assert levels["balanced"] is None
    assert levels["conservative"] is None
    assert levels["aggressive"] is not None and levels["aggressive"].top >= 65


def test_levels_are_monotonic_on_a_noisy_bar(tmp_path: Path) -> None:
    rng = np.random.default_rng(7)
    canvas = _framed(640, 480, top=80)
    noise = rng.integers(0, 30, size=(80, 640, 1))
    canvas[:80] = np.clip(canvas[:80].astype(int) + noise, 0, 255).astype(np.uint8)
    levels = detect_crop_levels(_save(canvas, tmp_path / "noisy.png"))
    present = [levels[name] for name in ("conservative", "balanced", "aggressive")]
    for smaller, bigger in itertools.pairwise(present):
        if smaller is not None:
            assert bigger is not None and _contains(bigger, smaller)


# --- records, cache, analysis -------------------------------------------------


def test_suggested_crop_reads_each_level_and_dismissal_covers_all() -> None:
    record = FileRecord(
        path="/x.jpg", size=1, mtime=0.0, media_type=MediaType.IMAGE, extension=".jpg",
        border_crop="0,10,100,90,100,100", border_signature=BORDER_SIGNATURE,
        border_crop_conservative="0,5,100,95,100,100",
        border_crop_aggressive="0,20,100,80,100,100",
        border_levels_signature=BORDER_LEVELS_SIGNATURE,
    )
    assert suggested_crop(record) == CropBox(0, 10, 100, 90, 100, 100)
    assert suggested_crop(record, "conservative") == CropBox(0, 5, 100, 95, 100, 100)
    assert suggested_crop(record, "aggressive") == CropBox(0, 20, 100, 80, 100, 100)
    with pytest.raises(ValueError):
        suggested_crop(record, "wild")
    record.border_crop = BORDER_DISMISSED
    assert suggested_crop(record) is None
    assert suggested_crop(record, "conservative") is None
    assert suggested_crop(record, "aggressive") is None


def test_analyze_stores_all_levels(tmp_path: Path) -> None:
    path = _save(_framed(640, 480, top=70, bar_value=56), tmp_path / "gray.png")
    record = _record(path)
    assert analyze_borders([record]) == []
    assert record.border_signature == BORDER_SIGNATURE
    assert record.border_levels_signature == BORDER_LEVELS_SIGNATURE
    assert record.border_crop == ""  # balanced: checked, clean
    assert suggested_crop(record, "aggressive") is not None


def test_balanced_cache_stays_valid_without_remeasuring_clean_files(
    tmp_path: Path, monkeypatch
) -> None:
    path = _save(_framed(640, 480, top=60), tmp_path / "plain.png")
    clean = _record(path)
    clean.border_crop, clean.border_signature = "", BORDER_SIGNATURE
    candidate = _record(path)
    candidate.border_crop = "0,60,640,480,640,480"
    candidate.border_signature = BORDER_SIGNATURE

    measured: list[str] = []

    def fake_levels(p):
        measured.append(str(p))
        return {name: CropBox(0, 60, 640, 480, 640, 480) for name in borders.CROP_LEVELS}

    monkeypatch.setattr(borders, "detect_crop_levels", fake_levels)
    # Balanced scan: the known-clean file is not re-measured; the candidate
    # is, so its other levels exist for the review toggle.
    assert analyze_borders([clean, candidate]) == []
    assert measured == [candidate.path]
    assert suggested_crop(candidate, "conservative") is not None
    # An aggressive scan cannot trust a balanced "clean": it re-measures.
    measured.clear()
    assert analyze_borders([clean], level="aggressive") == []
    assert measured == [clean.path]


def test_cache_round_trips_level_verdicts(tmp_path: Path) -> None:
    path = _save(_framed(640, 480, top=70, bar_value=56), tmp_path / "gray.png")
    record = _record(path)
    analyze_borders([record])
    cache = HashCache(tmp_path / "cache.sqlite3")
    cache.store_all([record])
    fresh = _record(path)
    cache.hydrate([fresh])
    cache.close()
    assert fresh.border_levels_signature == BORDER_LEVELS_SIGNATURE
    assert suggested_crop(fresh, "aggressive") == suggested_crop(record, "aggressive")
    assert suggested_crop(fresh) is None


def test_groups_follow_the_scan_level(tmp_path: Path) -> None:
    gray = _record(_save(_framed(640, 480, top=70, bar_value=56), tmp_path / "gray.png"))
    plain = _record(_save(_framed(640, 480, top=60), tmp_path / "plain.png"))
    analyze_borders([gray, plain])
    balanced = build_crop_groups([gray, plain])
    assert [Path(m.path).name for m in balanced[0].members] == ["plain.png"]
    aggressive = build_crop_groups([gray, plain], level="aggressive")
    assert {Path(m.path).name for m in aggressive[0].members} == {"gray.png", "plain.png"}
    assert aggressive[0].match_details["crop_level"] == "aggressive"


def test_scan_level_picks_the_candidates(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    _save(_framed(640, 480, top=70, bar_value=56), media / "gray.png")
    _save(_framed(640, 480, top=60), media / "plain.png")
    balanced = run_scan([media], find_black_borders=True, use_cache=False)
    names = {
        Path(m.path).name
        for g in balanced.groups if g.kind == GroupKind.CROP for m in g.members
    }
    assert names == {"plain.png"}
    aggressive = run_scan(
        [media], find_black_borders=True, crop_aggressiveness="aggressive", use_cache=False
    )
    names = {
        Path(m.path).name
        for g in aggressive.groups if g.kind == GroupKind.CROP for m in g.members
    }
    assert names == {"plain.png", "gray.png"}


# --- web review ---------------------------------------------------------------


def _fake_trash(directory: Path):
    def send(src: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / src.name
        shutil.move(str(src), str(target))
        return target

    return send


class _FakeBatch:
    trash_dir: Path

    def send(self, src: Path) -> Path:
        return _fake_trash(self.trash_dir)(Path(src))


def _level_app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("DEDUPE_THUMBNAIL_CACHE_DIR", str(tmp_path / "thumbs"))
    media = tmp_path / "media"
    media.mkdir()
    # Captioned bar: balanced/aggressive crop ~90 rows, conservative little/none.
    _save(_framed(320, 240, top=45, caption_rows=(18, 30)), media / "captioned.png")
    _save(_framed(320, 240, top=30), media / "plain.png")
    _FakeBatch.trash_dir = tmp_path / "Trash"
    monkeypatch.setattr(web_app, "TrashBatch", _FakeBatch)
    cache_path = tmp_path / "cache.sqlite3"
    result = run_scan([media], find_black_borders=True, cache_path=cache_path)
    app = create_app(
        result, review_session_path=tmp_path / "session.json", cache_path=cache_path
    )
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    group = next(g for g in result.groups if g.kind == GroupKind.CROP)
    return client, headers, scan_id, group


def _member(client, group_id: str, path: str) -> dict:
    fetched = client.get(f"/api/groups/{group_id}").get_json()
    return next(m for m in fetched["members"] if m["path"] == path)


def test_web_level_switch_changes_what_apply_cuts(tmp_path: Path, monkeypatch) -> None:
    client, headers, scan_id, group = _level_app(tmp_path, monkeypatch)
    target = next(m.path for m in group.members if m.path.endswith("captioned.png"))
    base = {"group_id": group.id, "path": target, "scan_id": scan_id}

    member = _member(client, group.id, target)
    assert member["crop_level"] == "balanced"
    assert set(member["crop_levels"]) == {"conservative", "balanced", "aggressive"}
    balanced_top = member["crop"]["top"]
    assert balanced_top >= 40

    refused = client.post(
        "/api/crop/level", json={**base, "level": "wild"}, headers=headers
    )
    assert refused.status_code == 400

    switched = client.post(
        "/api/crop/level", json={**base, "level": "conservative"}, headers=headers
    )
    assert switched.status_code == 200
    member = _member(client, group.id, target)
    assert member["crop_level"] == "conservative"
    conservative = member["crop_levels"]["conservative"]
    assert member["crop"] == conservative
    if conservative is not None:
        assert conservative["top"] < balanced_top

    # Back to balanced, then Crop cuts the balanced box.
    client.post("/api/crop/level", json={**base, "level": "balanced"}, headers=headers)
    applied = client.post("/api/crop/apply", json=base, headers=headers)
    assert applied.status_code == 200
    with Image.open(target) as cropped:
        assert cropped.size == (320, 240 - balanced_top)


def test_web_bulk_level_and_crop_all_use_it(tmp_path: Path, monkeypatch) -> None:
    client, headers, scan_id, group = _level_app(tmp_path, monkeypatch)
    bulk = client.post(
        "/api/crop/level",
        json={"group_id": group.id, "level": "aggressive", "scan_id": scan_id},
        headers=headers,
    )
    assert bulk.status_code == 200
    expected = {}
    for record in group.members:
        member = _member(client, group.id, record.path)
        assert member["crop_level"] == "aggressive"
        box = member["crop_levels"]["aggressive"]
        assert box is not None
        expected[record.path] = (box["crop_width"], box["crop_height"])
    applied = client.post(
        "/api/crop/apply-all", json={"group_id": group.id, "scan_id": scan_id},
        headers=headers,
    )
    assert applied.status_code == 200
    assert applied.get_json()["cropped_count"] == len(expected)
    for path, size in expected.items():
        with Image.open(path) as cropped:
            assert cropped.size == size
