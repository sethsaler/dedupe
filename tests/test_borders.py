"""Black-border detection, crop suggestions, and in-place cropping."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from dedupe import borders
from dedupe.borders import (
    BORDER_DISMISSED,
    BORDER_SIGNATURE,
    USER_LOCK_FLAGS,
    CropBox,
    copy_xattrs,
    crop_file_in_place,
    detect_black_borders,
    detect_borders_in_array,
    file_flags,
    restore_original_in_place,
    set_file_flags,
    suggested_crop,
)
from dedupe.cache import HashCache
from dedupe.engine import run_scan
from dedupe.models import FileRecord, GroupKind, MediaType
from dedupe.web import app as web_app
from dedupe.web.app import create_app


def _picture(width: int, height: int, seed: int = 0) -> np.ndarray:
    """A noisy mid-tone picture with structure (never reads as black)."""
    rng = np.random.default_rng(seed)
    base = rng.integers(70, 230, size=(height, width, 3), dtype=np.uint8)
    base[:, : width // 2, 0] = 200
    return base


def _framed(
    width: int,
    height: int,
    *,
    top: int = 0,
    bottom: int = 0,
    left: int = 0,
    right: int = 0,
    bar_value: int = 3,
) -> np.ndarray:
    canvas = np.full((height, width, 3), bar_value, dtype=np.uint8)
    inner = _picture(width - left - right, height - top - bottom)
    canvas[top : height - bottom, left : width - right] = inner
    return canvas


def _save(array: np.ndarray, path: Path, **kwargs) -> Path:
    Image.fromarray(array).save(path, **kwargs)
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


def _fake_trash(directory: Path):
    def send(src: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / src.name
        n = 1
        while target.exists():
            target = directory / f"{src.stem}_{n}{src.suffix}"
            n += 1
        shutil.move(str(src), str(target))
        return target

    return send


class _FakeBatch:
    trash_dir: Path

    def send(self, src: Path) -> Path:
        return _fake_trash(self.trash_dir)(Path(src))


# --- detection --------------------------------------------------------------


def test_letterbox_bars_are_found_exactly(tmp_path: Path) -> None:
    path = _save(_framed(640, 480, top=60, bottom=60), tmp_path / "letterbox.png")
    box = detect_black_borders(path)
    assert box == CropBox(left=0, top=60, right=640, bottom=420, width=640, height=480)
    assert box.crop_width == 640 and box.crop_height == 360


def test_pillarbox_bars_survive_jpeg_noise(tmp_path: Path) -> None:
    path = _save(
        _framed(1600, 900, left=200, right=200, bar_value=8),
        tmp_path / "pillar.jpg",
        quality=82,
    )
    box = detect_black_borders(path)
    assert box is not None
    # JPEG blocks bleed a few pixels into the bars; the crop stays tight.
    assert 196 <= box.left <= 204
    assert 1396 <= box.right <= 1404
    assert box.top == 0 and box.bottom == 900


def test_one_sided_bar_is_found(tmp_path: Path) -> None:
    path = _save(_framed(800, 600, right=120), tmp_path / "side.png")
    box = detect_black_borders(path)
    assert box is not None
    assert (box.left, box.top, box.right, box.bottom) == (0, 0, 680, 600)


def test_clean_image_has_no_suggestion(tmp_path: Path) -> None:
    path = _save(_picture(640, 480), tmp_path / "clean.png")
    assert detect_black_borders(path) is None


def test_hairline_border_is_ignored() -> None:
    luma = np.full((1000, 1000), 150, dtype=np.uint8)
    luma[:1] = 0  # one dark line: below the minimum bar thickness
    assert detect_borders_in_array(luma) is None


def test_mostly_black_frame_is_not_a_crop() -> None:
    luma = np.zeros((1000, 1000), dtype=np.uint8)
    luma[450:550, 450:550] = 200  # a small bright subject on black
    assert detect_borders_in_array(luma) is None
    assert detect_borders_in_array(np.zeros((200, 200), dtype=np.uint8)) is None


def test_transparent_margins_are_not_black(tmp_path: Path) -> None:
    rgba = np.zeros((400, 400, 4), dtype=np.uint8)
    rgba[50:350, 50:350, :3] = 180
    rgba[50:350, 50:350, 3] = 255
    path = tmp_path / "logo.png"
    Image.fromarray(rgba, "RGBA").save(path)
    assert detect_black_borders(path) is None


def _studio_portrait(*, hair_top: int, seed: int = 3) -> np.ndarray:
    """A person against a black studio backdrop: dark textured hair over a
    lit face, framed in the middle of an 800×1000 portrait."""
    rng = np.random.default_rng(seed)
    height, width = 1000, 800
    luma = rng.integers(0, 7, size=(height, width)).astype(np.uint8)  # backdrop
    yy, xx = np.mgrid[0:height, 0:width]
    head = ((xx - 400) / 230) ** 2 + ((yy - (hair_top + 300)) / 300) ** 2 <= 1
    # Black hair: mostly under the old "black" cut-off of 28, with strands
    # catching a little light.
    hair = rng.integers(8, 34, size=(height, width)).astype(np.uint8)
    hair[rng.random((height, width)) < 0.01] = 70
    luma[head] = hair[head]
    face = ((xx - 400) / 170) ** 2 + ((yy - (hair_top + 380)) / 220) ** 2 <= 1
    luma[face] = rng.integers(140, 200, size=(height, width)).astype(np.uint8)[face]
    # Shoulders: a rounded shape running off the bottom, never a straight edge.
    body = ((xx - 400) / 340) ** 2 + ((yy - (hair_top + 1000)) / 420) ** 2 <= 1
    luma[body] = rng.integers(90, 160, size=(height, width)).astype(np.uint8)[body]
    return luma


def test_dark_hair_against_a_black_backdrop_is_not_a_bar() -> None:
    # The hair runs off the top of the frame: the old detector read the hair
    # rows as a black bar and cut the top of the head off.
    assert detect_borders_in_array(_studio_portrait(hair_top=-80)) is None


def test_black_headroom_above_dark_hair_is_not_a_bar() -> None:
    # Black backdrop above the head fades into dark hair with no hard edge;
    # cropping it would trim down to (and into) the hair.
    assert detect_borders_in_array(_studio_portrait(hair_top=120)) is None


def test_letterbox_stops_at_dark_hair_instead_of_cutting_into_it() -> None:
    luma = _studio_portrait(hair_top=40)
    luma[:60] = 2
    luma[-60:] = 2
    found = detect_borders_in_array(luma)
    # Either the bars are left alone or the crop stops at the bar; it never
    # runs into the hair below row 60.
    assert found is None or (found[0] <= 60 and found[1] <= 60)


def test_limb_reaching_the_edge_is_not_cut_off() -> None:
    rng = np.random.default_rng(5)
    luma = rng.integers(0, 7, size=(900, 1200)).astype(np.uint8)
    yy, xx = np.mgrid[0:900, 0:1200]
    body = ((xx - 550) / 250) ** 2 + ((yy - 500) / 300) ** 2 <= 1
    luma[body] = rng.integers(90, 200, size=(900, 1200)).astype(np.uint8)[body]
    luma[420:428, 800:1200] = 160  # a thin arm out to the right edge (<1% of a column)
    luma[600:900, 900:1200] = rng.integers(8, 30, size=(300, 300))  # a dark sleeve
    assert detect_borders_in_array(luma) is None


def test_a_real_bar_beside_a_dark_subject_still_has_a_hard_edge(tmp_path: Path) -> None:
    # Pillarboxed video frame whose picture is mid-tone: still found exactly.
    path = _save(_framed(1280, 720, left=160, right=160, bar_value=16), tmp_path / "pillar.png")
    box = detect_black_borders(path)
    assert box is not None and (box.left, box.right) == (160, 1120)


def test_box_follows_exif_orientation(tmp_path: Path) -> None:
    # Stored landscape with bars on the left/right; orientation 6 rotates it
    # 90° clockwise for display, so the bars sit on top/bottom on screen.
    stored = _framed(600, 400, left=50, right=50)
    image = Image.fromarray(stored)
    exif = image.getexif()
    exif[0x0112] = 6
    path = tmp_path / "rotated.jpg"
    image.save(path, quality=95, exif=exif.tobytes())
    box = detect_black_borders(path)
    assert box is not None
    assert (box.width, box.height) == (400, 600)
    assert box.left == 0 and box.right == 400
    assert 46 <= box.top <= 54 and 546 <= box.bottom <= 554


def test_crop_box_round_trips_and_rejects_bad_values() -> None:
    box = CropBox(1, 2, 30, 40, 50, 60)
    assert CropBox.decode(box.encode()) == box
    assert CropBox.decode("") is None
    assert CropBox.decode(BORDER_DISMISSED) is None
    assert CropBox.decode("0,0,50,60,50,60") is None  # the whole frame is no crop
    assert CropBox.decode("5,5,1,1,10,10") is None
    assert CropBox.decode("garbage") is None


def test_suggestion_requires_the_current_signature() -> None:
    record = FileRecord(path="/x.jpg", size=1, mtime=0.0, media_type=MediaType.IMAGE,
                        extension=".jpg", border_crop="0,10,100,90,100,100")
    assert suggested_crop(record) is None
    record.border_signature = BORDER_SIGNATURE
    assert suggested_crop(record) == CropBox(0, 10, 100, 90, 100, 100)


# --- cropping ---------------------------------------------------------------


def test_crop_replaces_file_and_trashes_original(tmp_path: Path) -> None:
    path = _save(_framed(640, 480, top=60, bottom=60), tmp_path / "movie.jpg", quality=90)
    os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    original_bytes = path.read_bytes()
    record = _record(path)
    box = detect_black_borders(path)

    outcome = crop_file_in_place(record, box, send_to_trash=_fake_trash(tmp_path / "Trash"))

    assert Path(outcome.original_in_trash).read_bytes() == original_bytes
    with Image.open(path) as cropped:
        assert cropped.format == "JPEG"
        assert cropped.size == (box.crop_width, box.crop_height)
    # Date-sorted folders keep their order.
    assert path.stat().st_mtime_ns == 1_600_000_000_000_000_000
    assert detect_black_borders(path) is None
    assert not list(tmp_path.glob(".movie.dedupe-crop-*"))


def test_crop_resets_orientation_and_keeps_upright_pixels(tmp_path: Path) -> None:
    stored = _framed(600, 400, left=50, right=50)
    image = Image.fromarray(stored)
    exif = image.getexif()
    exif[0x0112] = 6
    path = tmp_path / "rotated.jpg"
    image.save(path, quality=95, exif=exif.tobytes())
    box = detect_black_borders(path)

    crop_file_in_place(_record(path), box, send_to_trash=_fake_trash(tmp_path / "Trash"))

    with Image.open(path) as cropped:
        assert cropped.getexif().get(0x0112, 1) == 1
        assert cropped.size == (box.crop_width, box.crop_height)


def test_crop_failure_before_swap_leaves_original(tmp_path: Path, monkeypatch) -> None:
    path = _save(_framed(320, 240, top=30, bottom=30), tmp_path / "keep.png")
    before = path.read_bytes()
    box = detect_black_borders(path)

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(borders, "write_cropped_copy", broken)
    with pytest.raises(OSError):
        crop_file_in_place(_record(path), box, send_to_trash=_fake_trash(tmp_path / "Trash"))
    assert path.read_bytes() == before
    assert not (tmp_path / "Trash").exists()


def test_crop_refuses_when_dimensions_changed(tmp_path: Path) -> None:
    path = _save(_framed(320, 240, top=30, bottom=30), tmp_path / "changed.png")
    stale = CropBox(0, 30, 320, 210, 400, 300)
    with pytest.raises(ValueError, match="dimensions changed"):
        crop_file_in_place(_record(path), stale, send_to_trash=_fake_trash(tmp_path / "T"))
    assert path.exists()


# --- locked files and metadata ---------------------------------------------

needs_file_flags = pytest.mark.skipif(
    not hasattr(os, "chflags") or not hasattr(stat, "UF_HIDDEN"),
    reason="BSD file flags (macOS)",
)
LOCKED = getattr(stat, "UF_IMMUTABLE", 0)
HIDDEN = getattr(stat, "UF_HIDDEN", 0)


@pytest.fixture
def unlock_after(tmp_path: Path):
    """Locked files and folders would otherwise outlive pytest's temp cleanup."""
    yield
    if hasattr(os, "chflags"):
        subprocess.run(["chflags", "-R", "nouchg", str(tmp_path)], check=False)


def _xattr_names(path: Path) -> set[str]:
    return set(borders._list_xattrs(path))


@needs_file_flags
def test_crop_edits_a_finder_locked_hidden_file(tmp_path: Path, unlock_after) -> None:
    path = _save(_framed(320, 240, top=30, bottom=30), tmp_path / "locked.png")
    set_file_flags(path, LOCKED | HIDDEN)
    box = detect_black_borders(path)

    outcome = crop_file_in_place(_record(path), box, send_to_trash=_fake_trash(tmp_path / "Trash"))

    with Image.open(path) as cropped:
        assert cropped.size == (box.crop_width, box.crop_height)
    # The cropped file comes back locked and hidden, like the original.
    assert file_flags(path) & (LOCKED | HIDDEN) == LOCKED | HIDDEN
    # The original waits in the Trash unlocked, so emptying the Trash never stalls.
    assert not file_flags(outcome.original_in_trash) & USER_LOCK_FLAGS
    assert file_flags(outcome.original_in_trash) & HIDDEN
    assert outcome.stat.st_flags == file_flags(path)


@needs_file_flags
def test_failed_trash_leaves_a_locked_file_locked(tmp_path: Path, unlock_after) -> None:
    path = _save(_framed(320, 240, top=30, bottom=30), tmp_path / "locked.png")
    before = path.read_bytes()
    set_file_flags(path, LOCKED)

    def refuse(_src: Path) -> Path:
        raise OSError("Trash unavailable")

    with pytest.raises(OSError, match="Trash unavailable"):
        crop_file_in_place(_record(path), detect_black_borders(path), send_to_trash=refuse)
    assert path.read_bytes() == before
    assert file_flags(path) & LOCKED
    assert not list(tmp_path.glob(".locked.dedupe-crop-*"))


@needs_file_flags
def test_crop_works_in_a_locked_folder_and_relocks_it(tmp_path: Path, unlock_after) -> None:
    folder = tmp_path / "Album"
    folder.mkdir()
    path = _save(_framed(320, 240, left=40, right=40), folder / "pic.jpg", quality=92)
    record = _record(path)
    box = detect_black_borders(path)
    set_file_flags(folder, LOCKED)

    crop_file_in_place(record, box, send_to_trash=_fake_trash(tmp_path / "Trash"))

    with Image.open(path) as cropped:
        assert cropped.size == (box.crop_width, box.crop_height)
    assert file_flags(folder) & LOCKED
    assert [entry.name for entry in folder.iterdir()] == ["pic.jpg"]


def test_crop_works_in_a_read_only_folder_and_restores_it(tmp_path: Path) -> None:
    folder = tmp_path / "Album"
    folder.mkdir()
    path = _save(_framed(320, 240, top=30, bottom=30), folder / "pic.png")
    path.chmod(0o444)
    record = _record(path)
    box = detect_black_borders(path)
    folder.chmod(0o555)
    try:
        crop_file_in_place(record, box, send_to_trash=_fake_trash(tmp_path / "Trash"))
        assert stat.S_IMODE(folder.stat().st_mode) == 0o555
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
        with Image.open(path) as cropped:
            assert cropped.size == (box.crop_width, box.crop_height)
    finally:
        folder.chmod(0o755)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended attributes")
def test_crop_keeps_finder_metadata_but_not_bookkeeping(tmp_path: Path) -> None:
    path = _save(_framed(320, 240, top=30, bottom=30), tmp_path / "download.png")
    where_from = "com.apple.metadata:kMDItemWhereFroms"
    borders._set_xattr(path, where_from, b"bplist00-placeholder")
    borders._set_xattr(path, "com.example.tag", b"blue")
    borders._set_xattr(path, "com.apple.ResourceFork", b"stale icon")

    crop_file_in_place(_record(path), detect_black_borders(path),
                       send_to_trash=_fake_trash(tmp_path / "Trash"))

    names = _xattr_names(path)
    assert {where_from, "com.example.tag"} <= names
    assert "com.apple.ResourceFork" not in names
    assert borders._get_xattr(path, "com.example.tag") == b"blue"


def test_copy_xattrs_skips_attributes_the_system_refuses(tmp_path: Path, monkeypatch) -> None:
    source, target = tmp_path / "a", tmp_path / "b"
    source.write_bytes(b"a")
    target.write_bytes(b"b")
    monkeypatch.setattr(borders, "_list_xattrs", lambda _p: ["x.refused", "x.kept"])
    monkeypatch.setattr(borders, "_get_xattr", lambda _p, name: name.encode())
    written = {}

    def setter(_path, name, value):
        if name == "x.refused":
            raise PermissionError("not permitted")
        written[name] = value

    monkeypatch.setattr(borders, "_set_xattr", setter)
    assert copy_xattrs(source, target) == ["x.kept"]
    assert written == {"x.kept": b"x.kept"}


@needs_file_flags
def test_undo_restores_original_with_the_current_lock(tmp_path: Path, unlock_after) -> None:
    path = _save(_framed(320, 240, top=30, bottom=30), tmp_path / "pic.png")
    original_bytes = path.read_bytes()
    outcome = crop_file_in_place(_record(path), detect_black_borders(path),
                                 send_to_trash=_fake_trash(tmp_path / "Trash"))
    # Locked after the crop; undo honours the file as it is now.
    set_file_flags(path, file_flags(path) | LOCKED)

    restored = restore_original_in_place(path, outcome.original_in_trash,
                                         send_to_trash=_fake_trash(tmp_path / "Trash"))

    assert path.read_bytes() == original_bytes
    assert file_flags(path) & LOCKED
    assert restored.st_size == len(original_bytes)
    # The original left the Trash; the cropped version took its place there.
    (trashed,) = (tmp_path / "Trash").iterdir()
    assert trashed.read_bytes() != original_bytes
    assert not file_flags(trashed) & USER_LOCK_FLAGS


# --- scan pipeline ----------------------------------------------------------


def test_scan_publishes_crop_review_and_caches_verdicts(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    _save(_framed(640, 480, top=80, bottom=80), media / "wide.png")
    _save(_framed(640, 480, left=40, right=40), media / "narrow.png")
    _save(_picture(640, 480, seed=3), media / "clean.png")
    cache_path = tmp_path / "cache.sqlite3"

    result = run_scan([media], find_black_borders=True, cache_path=cache_path)
    crop_groups = [g for g in result.groups if g.kind == GroupKind.CROP]
    assert len(crop_groups) == 1
    # Most border first.
    assert [Path(m.path).name for m in crop_groups[0].members] == ["wide.png", "narrow.png"]
    assert result.crop_files == 2
    assert result.diagnostics.stages["black_borders"].attempted == 3

    off = run_scan([media], cache_path=cache_path)
    assert not [g for g in off.groups if g.kind == GroupKind.CROP]

    # A dismissed suggestion stays dismissed for the same file content.
    wide = next(m for m in crop_groups[0].members if m.path.endswith("wide.png"))
    cache = HashCache(cache_path)
    from dataclasses import replace

    cache.store_all([replace(wide, border_crop=BORDER_DISMISSED)])
    cache.close()
    again = run_scan([media], find_black_borders=True, cache_path=cache_path)
    names = [
        Path(m.path).name
        for g in again.groups
        if g.kind == GroupKind.CROP
        for m in g.members
    ]
    assert names == ["narrow.png"]


def test_crop_review_never_vetoes_or_selects(tmp_path: Path) -> None:
    from dedupe.grouping import apply_smart_select, build_crop_groups
    from dedupe.models import SmartRule, effective_selected_paths

    path = _save(_framed(320, 240, top=30, bottom=30), tmp_path / "a.png")
    record = _record(path)
    record.border_crop = detect_black_borders(path).encode()
    record.border_signature = BORDER_SIGNATURE
    group = build_crop_groups([record])[0]
    group.reviewed_paths = [record.path]  # skipped
    apply_smart_select(group, SmartRule.SELECT_CANDIDATES)
    assert group.selected_for_removal == []
    assert group.reclaimable_bytes == 0

    from dedupe.grouping import build_one_group

    copy = FileRecord(**{**record.to_dict(), "path": str(tmp_path / "b.png"),
                         "media_type": MediaType.IMAGE})
    exact = build_one_group(GroupKind.EXACT, [record, copy])
    exact.selected_for_removal = [record.path]
    exact.suggested_keep = copy.path
    # A skipped crop is not a Keep decision: the duplicate pick still stands.
    assert effective_selected_paths([exact, group]) == [record.path]


# --- web API ----------------------------------------------------------------


def _crop_app(tmp_path: Path, monkeypatch, names=("one.png", "two.png")):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("DEDUPE_THUMBNAIL_CACHE_DIR", str(tmp_path / "thumbs"))
    media = tmp_path / "media"
    media.mkdir()
    for index, name in enumerate(names):
        _save(_framed(320, 240, top=30 + index * 10, bottom=30), media / name)
    _FakeBatch.trash_dir = tmp_path / "Trash"
    monkeypatch.setattr(web_app, "TrashBatch", _FakeBatch)
    cache_path = tmp_path / "cache.sqlite3"
    result = run_scan([media], find_black_borders=True, cache_path=cache_path)
    app = create_app(result, review_session_path=tmp_path / "session.json",
                     cache_path=cache_path)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    status = client.get("/api/status").get_json()
    group = next(g for g in result.groups if g.kind == GroupKind.CROP)
    return app, client, headers, status["scan_id"], group, result


def test_web_crop_apply_and_undo(tmp_path: Path, monkeypatch) -> None:
    _app, client, headers, scan_id, group, _result = _crop_app(tmp_path, monkeypatch)
    target = group.members[0].path
    original = Path(target).read_bytes()
    payload = {"group_id": group.id, "path": target, "scan_id": scan_id}

    fetched = client.get(f"/api/groups/{group.id}").get_json()
    member = next(m for m in fetched["members"] if m["path"] == target)
    assert member["crop_state"] == "pending"
    assert member["crop"]["crop_height"] < member["crop"]["height"]

    applied = client.post("/api/crop/apply", json=payload, headers=headers)
    assert applied.status_code == 200, applied.get_json()
    body = applied.get_json()
    assert body["cropped_paths"] == [target]
    member = next(m for m in body["members"] if m["path"] == target)
    assert member["crop_state"] == "cropped"
    assert member["height"] == member["crop"]["crop_height"]
    with Image.open(target) as cropped:
        assert cropped.size == (member["crop"]["crop_width"], member["crop"]["crop_height"])
    assert body["log_path"] and Path(body["log_path"]).exists()

    # Repeating the request is harmless.
    assert client.post("/api/crop/apply", json=payload, headers=headers).status_code == 200

    undone = client.post("/api/crop/undo", json=payload, headers=headers)
    assert undone.status_code == 200, undone.get_json()
    assert Path(target).read_bytes() == original
    member = next(m for m in undone.get_json()["members"] if m["path"] == target)
    assert member["crop_state"] == "pending"
    # The cropped version went to the Trash rather than vanishing.
    assert len(list((tmp_path / "Trash").iterdir())) == 1


def test_web_crop_skip_is_remembered(tmp_path: Path, monkeypatch) -> None:
    app, client, headers, scan_id, group, _result = _crop_app(tmp_path, monkeypatch)
    target = group.members[0].path
    payload = {"group_id": group.id, "path": target, "scan_id": scan_id}

    skipped = client.post("/api/crop/skip", json=payload, headers=headers)
    assert skipped.status_code == 200
    member = next(m for m in skipped.get_json()["members"] if m["path"] == target)
    assert member["crop_state"] == "skipped"
    assert member["crop"] is not None  # the suggestion is still shown

    rescan = run_scan([Path(target).parent], find_black_borders=True,
                      cache_path=app.config["DEDUPE_CACHE_PATH"])
    paths = [m.path for g in rescan.groups if g.kind == GroupKind.CROP for m in g.members]
    assert target not in paths

    restored = client.post("/api/crop/skip", json={**payload, "skip": False}, headers=headers)
    member = next(m for m in restored.get_json()["members"] if m["path"] == target)
    assert member["crop_state"] == "pending"
    rescan = run_scan([Path(target).parent], find_black_borders=True,
                      cache_path=app.config["DEDUPE_CACHE_PATH"])
    paths = [m.path for g in rescan.groups if g.kind == GroupKind.CROP for m in g.members]
    assert target in paths


def test_web_crop_all_skips_decided_files(tmp_path: Path, monkeypatch) -> None:
    _app, client, headers, scan_id, group, _result = _crop_app(
        tmp_path, monkeypatch, names=("a.png", "b.png", "c.png")
    )
    skipped = group.members[0].path
    client.post("/api/crop/skip",
                json={"group_id": group.id, "path": skipped, "scan_id": scan_id},
                headers=headers)
    before = Path(skipped).read_bytes()

    response = client.post("/api/crop/apply-all",
                           json={"group_id": group.id, "scan_id": scan_id}, headers=headers)
    assert response.status_code == 200
    body = response.get_json()
    assert body["cropped_count"] == 2
    assert body["failures"] == []
    assert Path(skipped).read_bytes() == before
    assert set(body["cropped_paths"]) == {m.path for m in group.members} - {skipped}


def test_web_crop_refuses_changed_file_and_selection(tmp_path: Path, monkeypatch) -> None:
    _app, client, headers, scan_id, group, _result = _crop_app(tmp_path, monkeypatch)
    target = Path(group.members[0].path)
    payload = {"group_id": group.id, "path": str(target), "scan_id": scan_id}
    _save(_picture(320, 240, seed=9), target)  # edited after the scan

    refused = client.post("/api/crop/apply", json=payload, headers=headers)
    assert refused.status_code == 400
    assert "changed since scan" in refused.get_json()["error"]
    assert not (tmp_path / "Trash").exists()

    selection = client.post(
        "/api/selection",
        json={"group_id": group.id, "selected": [str(target)], "reviewed": [str(target)],
              "scan_id": scan_id},
        headers=headers,
    )
    assert selection.status_code == 400


def test_cropped_file_leaves_exact_groups(tmp_path: Path, monkeypatch) -> None:
    app, client, headers, scan_id, group, _result = _crop_app(tmp_path, monkeypatch)
    target = group.members[0].path
    twin = Path(target).with_name("twin.png")
    shutil.copy2(target, twin)
    # Rebuild with an exact pair alongside the crop review.
    rescan = run_scan([Path(target).parent], find_black_borders=True,
                      cache_path=app.config["DEDUPE_CACHE_PATH"])
    assert any(g.kind == GroupKind.EXACT for g in rescan.groups)
    app = create_app(rescan, review_session_path=tmp_path / "session2.json",
                     cache_path=app.config["DEDUPE_CACHE_PATH"])
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    crop_group = next(g for g in rescan.groups if g.kind == GroupKind.CROP)

    applied = client.post("/api/crop/apply",
                          json={"group_id": crop_group.id, "path": target, "scan_id": scan_id},
                          headers=headers)
    assert applied.status_code == 200
    groups = client.get("/api/groups").get_json()["groups"]
    assert not any(g["kind"] == "exact" for g in groups)


@needs_file_flags
def test_web_crop_and_undo_a_locked_file(tmp_path: Path, monkeypatch, unlock_after) -> None:
    _app, client, headers, scan_id, group, _result = _crop_app(tmp_path, monkeypatch)
    target = group.members[0].path
    original = Path(target).read_bytes()
    set_file_flags(target, LOCKED)
    payload = {"group_id": group.id, "path": target, "scan_id": scan_id}

    applied = client.post("/api/crop/apply", json=payload, headers=headers)
    assert applied.status_code == 200, applied.get_json()
    assert file_flags(target) & LOCKED

    undone = client.post("/api/crop/undo", json=payload, headers=headers)
    assert undone.status_code == 200, undone.get_json()
    assert Path(target).read_bytes() == original
    assert file_flags(target) & LOCKED


def test_done_summary_names_only_the_checks_that_ran(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    _save(_framed(320, 240, top=30, bottom=30), media / "bars.png")
    _save(_picture(320, 240), media / "clean.png")
    kwargs = {"exact": False, "similar": False, "find_black_borders": True,
              "find_low_resolution": False, "random_review_count": 0}
    result = run_scan([media], **kwargs)
    assert web_app.scan_done_summary(result, kwargs) == "1 to crop"

    duplicates = {"exact": True, "similar": True, "find_low_resolution": False,
                  "random_review_count": 0}
    result = run_scan([media], **duplicates)
    assert web_app.scan_done_summary(result, duplicates) == "0 exact, 0 similar"
    assert web_app.scan_done_summary(result, {}) == "2 files"


# --- screenshot interface ---------------------------------------------------


def _text_rows(draw, x0: int, x1: int, y0: int, y1: int, *, line: int, gap: int,
               fill=(40, 40, 40)) -> None:
    """Paragraph-like ink: lines of words made of letter strokes, about as
    dense as real text (roughly half of a line's width is ink)."""
    y = y0
    while y + line <= y1:
        x = x0
        while x < x1 - 40:
            word = 30 + (x * 7 + y) % 90
            for letter in range(x, min(x + word, x1), 16):
                draw.rectangle([letter, y, letter + 9, y + line], fill=fill)
            x += word + 18
        y += line + gap


def _status_glyphs(draw, width: int, y0: int, y1: int, fill) -> None:
    """A clock on the left and signal/battery icons on the right."""
    for x in (70, 100, 130, 160):
        draw.rectangle([x, y0, x + 20, y1], fill=fill)
    for x in (width - 230, width - 180, width - 120):
        draw.rectangle([x, y0 + 4, x + 38, y1 - 2], fill=fill)


def _iphone_light(path: Path) -> Path:
    from PIL import ImageDraw

    width, height = 1170, 2532
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _text_rows(draw, 48, width - 48, 300, 900, line=34, gap=22)
    image.paste(Image.fromarray(_picture(width - 96, 700)), (48, 960))
    _text_rows(draw, 48, width - 48, 1700, 2380, line=34, gap=22)
    draw.rounded_rectangle([395, 2496, 775, 2510], radius=7, fill=(0, 0, 0))  # home pill
    image.save(path)
    return path


def test_iphone_status_bar_and_home_indicator_are_suggested(tmp_path: Path) -> None:
    path = _iphone_light(tmp_path / "IMG_0042.PNG")
    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    # Cut inside the white gaps: past the clock, before the first line of text.
    assert 92 < box.top < 300
    assert 2380 < box.bottom < 2496
    assert (box.left, box.right) == (0, 1170)
    assert CropBox.decode(box.encode()) == box
    assert box.to_dict()["chrome"] == ["top", "bottom"]


def test_android_dark_bars_end_at_the_app_bar(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 22, 52, (235, 235, 235))
    draw.rectangle([0, 80, width, 230], fill=(31, 59, 115))  # coloured app bar
    image.paste(Image.fromarray(_picture(width, height - 230 - 130)), (0, 230))
    for x in (250, 520, 790):  # back / home / recents
        draw.rectangle([x, height - 82, x + 40, height - 46], fill=(220, 220, 220))
    path = tmp_path / "Screenshot_20240101-101010.jpg"
    image.save(path, quality=90)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert abs(box.top - 80) <= 2  # the status bar ends where the app bar begins
    assert abs((height - box.bottom) - 130) <= 3


def test_desktop_menu_bar_is_suggested(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1920, 1080
    image = Image.new("RGB", (width, height), (236, 236, 236))
    draw = ImageDraw.Draw(image)
    for x in (20, 70, 140, 210, 290):  # menus
        draw.rectangle([x, 8, x + 40, 20], fill=(20, 20, 20))
    for x in (1700, 1760, 1820):  # status items
        draw.rectangle([x, 7, x + 30, 21], fill=(20, 20, 20))
    draw.rectangle([0, 28, width, height], fill=(52, 120, 200))
    image.paste(Image.fromarray(_picture(1600, 900)), (160, 100))
    path = tmp_path / "Screen Shot 2024-05-01 at 10.00.00.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert box.top == 28 and box.bottom == height


def test_meme_caption_is_not_a_status_bar(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1080, 1920
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    _text_rows(draw, 120, width - 120, 30, 130, line=60, gap=20)  # centred caption
    image.paste(Image.fromarray(_picture(width, height - 220)), (0, 220))
    path = tmp_path / "funny.png"
    image.save(path)
    assert detect_black_borders(path) is None


def test_camera_photos_are_never_treated_as_screenshots(tmp_path: Path) -> None:
    source = _iphone_light(tmp_path / "source.png")
    image = Image.open(source)
    exif = image.getexif()
    exif[0x010F] = "Apple"
    exif[0x0110] = "iPhone 15"
    path = tmp_path / "IMG_1234.jpg"
    image.save(path, quality=92, exif=exif.tobytes())
    assert detect_black_borders(path) is None


def test_status_bar_drawn_over_a_photo_is_left_alone(tmp_path: Path) -> None:
    from PIL import ImageDraw

    image = Image.fromarray(_picture(1170, 2532, seed=3))
    _status_glyphs(ImageDraw.Draw(image), 1170, 52, 92, (255, 255, 255))
    path = tmp_path / "Screenshot fullscreen.png"
    image.save(path)
    box = detect_black_borders(path)
    assert box is None or "top" not in box.chrome


def test_ios_screenshot_tag_counts_as_evidence(tmp_path: Path) -> None:
    image = Image.new("RGB", (900, 700))
    exif = image.getexif()
    exif.get_ifd(0x8769)[0x9286] = b"ASCII\0\0\0Screenshot"
    path = tmp_path / "anything.jpg"
    image.save(path, exif=exif.tobytes())
    with Image.open(path) as opened:
        assert borders.screenshot_evidence(path, opened) == "metadata"
    with Image.open(_save(_picture(900, 700), tmp_path / "plain.png")) as opened:
        assert borders.screenshot_evidence(tmp_path / "plain.png", opened) is None


def test_earlier_dismissals_survive_the_new_detector(tmp_path: Path) -> None:
    path = _iphone_light(tmp_path / "IMG_0042.PNG")
    record = _record(path)
    record.border_crop = BORDER_DISMISSED
    record.border_signature = "black-borders-v1"
    stale = _record(path)
    stale.border_crop = ""
    stale.border_signature = "black-borders-v1"

    borders.analyze_borders([record, stale])

    assert record.border_crop == BORDER_DISMISSED
    assert record.border_signature == BORDER_SIGNATURE
    assert suggested_crop(stale) is not None  # re-checked with the new rules


# --- review order and resumed reviews ---------------------------------------


def _suggested(path: Path) -> FileRecord:
    record = _record(path)
    box = detect_black_borders(path)
    record.border_crop = box.encode() if box else ""
    record.border_signature = BORDER_SIGNATURE
    return record


def test_screenshots_with_interface_bars_lead_the_crop_review(tmp_path: Path) -> None:
    from dedupe.grouping import build_crop_groups

    letterbox = _suggested(_save(_framed(640, 480, top=120, bottom=120), tmp_path / "movie.png"))
    screenshot = _suggested(_iphone_light(tmp_path / "IMG_0042.PNG"))
    pillar = _suggested(_save(_framed(640, 480, left=40, right=40), tmp_path / "pillar.png"))
    # The letterbox removes far more of its frame, but the screenshot comes first.
    group = build_crop_groups([letterbox, pillar, screenshot])[0]
    assert [Path(m.path).name for m in group.members] == [
        "IMG_0042.PNG", "movie.png", "pillar.png",
    ]


def test_resumed_crop_review_is_measured_again(tmp_path: Path) -> None:
    from dedupe.grouping import _build_independent_group, refresh_crop_groups
    from dedupe.models import ScanResult

    portrait = tmp_path / "portrait.png"
    Image.fromarray(_studio_portrait(hair_top=-80)).save(portrait)
    letterbox = _save(_framed(640, 480, top=60, bottom=60), tmp_path / "movie.png")
    screenshot = _iphone_light(tmp_path / "IMG_0042.PNG")
    done = _save(_framed(640, 480, top=30, bottom=30), tmp_path / "done.png")

    records = [_record(path) for path in (portrait, letterbox, screenshot, done)]
    # Saved by the previous detector, which cut the top of the head off.
    records[0].border_crop = "0,80,800,1000,800,1000"
    records[1].border_crop = "0,60,640,420,640,480"
    records[2].border_crop = "0,0,1170,2400,1170,2532"
    records[3].border_crop = "0,30,640,450,640,480"
    for record in records:
        record.border_signature = "black-borders-v2"
    (group,) = _build_independent_group(GroupKind.CROP, records)
    group.reviewed_paths = [str(portrait), str(done)]
    group.match_details["cropped"] = {str(done): {"box": "0,30,640,450,640,480"}}
    result = ScanResult(roots=[str(tmp_path)], files=records, groups=[group])

    measured = refresh_crop_groups(result)

    assert {Path(r.path).name for r in measured} == {"portrait.png", "movie.png", "IMG_0042.PNG"}
    assert all(r.border_signature == BORDER_SIGNATURE for r in measured)
    (refreshed,) = result.groups
    # The portrait no longer has a suggestion and leaves; the screenshot
    # leads; the cropped file stays so its crop can still be undone.
    assert [Path(m.path).name for m in refreshed.members] == [
        "IMG_0042.PNG", "movie.png", "done.png",
    ]
    assert refreshed.reviewed_paths == [str(done)]
    assert suggested_crop(refreshed.members[1]).top == 60
