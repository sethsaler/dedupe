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
    LEGACY_BORDER_SIGNATURES,
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


def _nav_buttons(draw, width: int, y0: int, y1: int, fill) -> None:
    """Android's back / home / recents buttons: a triangle, a ring, three bars."""
    mid = (y0 + y1) // 2
    left, centre, right = width // 4, width // 2 - 18, width * 3 // 4
    draw.polygon([(left + 40, y0), (left, mid), (left + 40, y1)], fill=fill)
    draw.ellipse([centre, y0, centre + 36, y1], outline=fill, width=5)
    for x in (right, right + 14, right + 28):
        draw.rectangle([x, y0, x + 8, y1], fill=fill)


def _icon_row(draw, y0: int, y1: int, xs, fill, *, counts: bool = False) -> None:
    """Outlined round icons, each followed by a two-digit count when asked."""
    size = y1 - y0
    for x in xs:
        draw.ellipse([x, y0, x + size, y1], outline=fill, width=max(3, size // 10))
        if counts:
            for digit in (x + size + 14, x + size + 34):
                draw.rectangle([digit, y0 + 8, digit + 12, y1 - 8], fill=fill)


def _avatar(image, x: int, y: int, size: int, seed: int = 3) -> None:
    """A round avatar: a disc of picture pasted through a circular mask."""
    from PIL import ImageDraw

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    image.paste(Image.fromarray(_picture(size, size, seed)), (x, y), mask)


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
    draw.rectangle([380, 26, 410, 48], fill=(235, 235, 235))  # a notification, left of centre
    draw.rectangle([0, 80, width, 230], fill=(31, 59, 115))  # coloured app bar
    image.paste(Image.fromarray(_picture(width, height - 230 - 130)), (0, 230))
    _nav_buttons(draw, width, height - 82, height - 46, (220, 220, 220))
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


def test_the_previous_signature_is_legacy() -> None:
    """Each bump keeps the signature before it, so its dismissals carry over."""
    version = int(BORDER_SIGNATURE.rsplit("-v", 1)[1])
    assert f"black-borders-v{version - 1}" in LEGACY_BORDER_SIGNATURES
    assert BORDER_SIGNATURE not in LEGACY_BORDER_SIGNATURES


@pytest.mark.parametrize("signature", sorted(LEGACY_BORDER_SIGNATURES))
def test_earlier_dismissals_survive_the_new_detector(tmp_path: Path, signature: str) -> None:
    path = _iphone_light(tmp_path / "IMG_0042.PNG")
    record = _record(path)
    record.border_crop = BORDER_DISMISSED
    record.border_signature = signature
    stale = _record(path)
    stale.border_crop = ""
    stale.border_signature = signature

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


def _feed_post(path: Path, *, dots_shift: int = 0, quality: int | None = None) -> None:
    """A feed post: status bar, header and author row above the picture; page
    dots, action row, caption, time and navigation bar below it."""
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    black = (0, 0, 0)
    _status_glyphs(draw, width, 31, 70, black)
    draw.polygon([(80, 128), (44, 150), (80, 172)], fill=black)  # back arrow
    draw.rectangle([44, 147, 110, 153], fill=black)
    for x in (150, 190, 230, 270):  # title
        draw.rectangle([x, 135, x + 28, 165], fill=black)
    draw.rounded_rectangle([850, 125, 1030, 185], radius=16, fill=(239, 239, 239))  # Follow
    for x in range(880, 1000, 20):
        draw.rectangle([x, 145, x + 10, 165], fill=black)
    _avatar(image, 40, 300, 100)
    _text_rows(draw, 160, 460, 320, 342, line=22, gap=0)
    _text_rows(draw, 160, 400, 356, 374, line=18, gap=0, fill=(120, 120, 120))
    draw.rounded_rectangle([640, 325, 840, 380], radius=14, fill=(239, 239, 239))  # Following
    for x in range(670, 810, 18):
        draw.rectangle([x, 342, x + 9, 362], fill=black)
    for y in (337, 350, 363):  # menu
        draw.rectangle([1000, y, 1040, y + 5], fill=black)
    image.paste(Image.fromarray(_picture(width, 1441)), (0, 419))
    for i, x in enumerate(range(500 + dots_shift, 580 + dots_shift, 20)):  # page dots
        draw.ellipse([x, 1890, x + 10, 1900], fill=(60, 100, 230) if i == 2 else (200, 200, 200))
    _icon_row(draw, 1940, 2000, (40, 190, 340), black, counts=True)
    draw.rectangle([990, 1940, 1040, 2000], outline=black, width=5)  # bookmark
    _text_rows(draw, 40, 800, 2030, 2064, line=34, gap=0)  # caption
    _text_rows(draw, 40, 260, 2100, 2118, line=18, gap=0, fill=(140, 140, 140))  # time
    _nav_buttons(draw, width, height - 82, height - 46, (60, 60, 60))
    if quality is None:
        image.save(path)
    else:
        image.save(path, quality=quality)


def test_post_interface_is_cropped_to_the_picture(tmp_path: Path) -> None:
    path = tmp_path / "Screenshot_20240101-101010.png"
    _feed_post(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert abs(box.top - 419) <= 4
    assert abs(box.bottom - 1860) <= 4


@pytest.mark.parametrize("quality, shift", [(75, 13), (75, 15), (70, 3), (70, 15)])
def test_page_dots_in_a_jpeg_are_cropped_with_the_action_row(
    tmp_path: Path, quality: int, shift: int
) -> None:
    """A screenshot a messaging app saved again as a JPEG: the compression
    rings beside each saturated dot and nudges a dot's rim a column either
    way, and the dots still read as page dots."""
    path = tmp_path / "Screenshot_20240101-101010.jpg"
    _feed_post(path, dots_shift=shift, quality=quality)

    box = detect_black_borders(path)
    assert box is not None and "bottom" in box.chrome
    assert abs(box.bottom - 1860) <= 4


@pytest.mark.parametrize(
    "icons", [((255, 255, 255), (255, 255, 255)), ((70, 150, 255), (255, 70, 70))],
    ids=["white", "coloured"],
)
def test_reply_field_and_home_indicator_are_cropped_together(tmp_path: Path, icons) -> None:
    from PIL import ImageDraw

    width, height = 1170, 2532
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    white = (255, 255, 255)
    _status_glyphs(draw, width, 52, 92, white)
    image.paste(Image.fromarray(_picture(width, 2050)), (0, 150))
    draw.rounded_rectangle([40, 2260, 880, 2340], radius=40, outline=(140, 140, 140), width=3)
    _text_rows(draw, 90, 500, 2288, 2312, line=24, gap=0, fill=(170, 170, 170))
    for x, fill in zip((930, 1040), icons):
        _icon_row(draw, 2270, 2330, (x,), fill)
    draw.rounded_rectangle([395, 2496, 775, 2510], radius=7, fill=white)  # home pill
    path = tmp_path / "IMG_4242.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert abs(box.top - 150) <= 4
    assert abs(box.bottom - 2200) <= 4


def test_post_text_above_the_picture_is_kept(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1170, 2532
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    black = (0, 0, 0)
    _status_glyphs(draw, width, 52, 92, black)
    draw.rectangle([0, 110, width, 111], fill=(220, 220, 220))  # hairline
    _avatar(image, 48, 140, 96)
    _text_rows(draw, 170, 500, 150, 174, line=24, gap=0)
    _text_rows(draw, 170, 420, 188, 208, line=20, gap=0, fill=(120, 120, 120))
    for x in (1060, 1080, 1100):  # menu dots
        draw.ellipse([x, 180, x + 8, 188], fill=black)
    _text_rows(draw, 48, width - 48, 270, 354, line=34, gap=16)  # two full lines
    _text_rows(draw, 48, 520, 370, 404, line=34, gap=0)  # a short last line
    image.paste(Image.fromarray(_picture(width - 96, 1460)), (48, 440))
    _icon_row(draw, 1940, 2000, (60, 330, 600, 870), (90, 90, 90), counts=True)
    draw.rounded_rectangle([395, 2496, 775, 2510], radius=7, fill=black)  # home pill
    path = tmp_path / "IMG_0002.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert 236 <= box.top <= 270  # between the author row and the post's words
    assert abs(box.bottom - 1900) <= 4


def test_picture_with_a_flat_top_is_cut_at_its_edge(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    _avatar(image, 40, 130, 90)
    _text_rows(draw, 150, 420, 150, 172, line=22, gap=0)
    for x in (990, 1005, 1020):
        draw.ellipse([x, 170, x + 8, 178], fill=(0, 0, 0))
    picture = _picture(width, 1500)
    picture[:90] = (92, 140, 198)  # a clear sky, flat where it meets the interface
    image.paste(Image.fromarray(picture), (0, 250))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert abs(box.top - 250) <= 4


def test_action_row_clipped_by_the_edge_is_still_interface(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1179, 1659
    image = Image.new("RGB", (width, height), (255, 255, 255))
    image.paste(Image.fromarray(_picture(width, 1360)), (0, 60))
    draw = ImageDraw.Draw(image)
    for i, x in enumerate(range(550, 630, 20)):  # page dots
        draw.ellipse([x, 1450, x + 10, 1460], fill=(60, 100, 230) if i == 2 else (200, 200, 200))
    _icon_row(draw, 1580, 1700, (40, 190, 340), (0, 0, 0), counts=True)  # cut by the edge
    draw.rectangle([1070, 1580, 1130, 1700], outline=(0, 0, 0), width=6)
    path = tmp_path / "IMG_0101.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("bottom",)
    assert box.top == 0
    assert abs(box.bottom - 1420) <= 4


def test_rounded_corners_are_not_a_status_bar(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1170, 2074
    image = Image.new("RGB", (width, height), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    radius = 48
    draw.rectangle([0, 0, radius, radius], fill=(20, 20, 20))
    draw.rectangle([width - radius, 0, width, radius], fill=(20, 20, 20))
    draw.ellipse([0, 0, 2 * radius, 2 * radius], fill=(245, 245, 245))
    draw.ellipse([width - 2 * radius, 0, width, 2 * radius], fill=(245, 245, 245))
    _text_rows(draw, 40, 900, 260, 300, line=40, gap=0)
    image.paste(Image.fromarray(_picture(width, 1170)), (0, 330))
    path = tmp_path / "Screenshot 2024-01-01 at 10.10.10.png"
    image.save(path)

    assert detect_black_borders(path) is None


def test_objects_cut_by_the_edge_are_not_a_navigation_bar(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 750, 1040
    image = Image.new("RGB", (width, height), (240, 236, 230))
    image.paste(Image.fromarray(_picture(width, 980)), (0, 0))
    draw = ImageDraw.Draw(image)
    for x in (170, 280, 470):  # drawer knobs, cut by the bottom edge
        draw.ellipse([x, height - 12, x + 24, height + 12], fill=(170, 140, 90))
    path = tmp_path / "Screenshot 2024-01-01 at 10.10.11.png"
    image.save(path)

    assert detect_black_borders(path) is None


# --- the walk past the first bar ---------------------------------------------


def _birds(draw, y: int, xs, fill, *, size=(28, 10)) -> None:
    for x in xs:
        draw.rectangle([x, y, x + size[0], y + size[1]], fill=fill)


def _author_row(image, draw, y: int, *, width: int, size: int = 96) -> None:
    """An avatar, two lines of text, and the menu dots at the right."""
    _avatar(image, 48, y, size)
    _text_rows(draw, 170, 500, y + 10, y + 34, line=24, gap=0)
    _text_rows(draw, 170, 420, y + 48, y + 68, line=20, gap=0, fill=(120, 120, 120))
    for x in (width - 110, width - 90, width - 70):
        draw.ellipse([x, y + 40, x + 8, y + 48], fill=(0, 0, 0))


def test_android_filled_buttons_are_a_navigation_bar(tmp_path: Path) -> None:
    """Older Android drew back / home / recents as three filled shapes."""
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
    assert abs(box.top - 80) <= 2
    assert abs((height - box.bottom) - 130) <= 3


def test_status_bar_set_low_by_a_cutout_is_still_a_bar(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 105, 140, (0, 0, 0))  # first ink 4.5% down
    image.paste(Image.fromarray(_picture(width, 1500)), (0, 260))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert abs(box.top - 260) <= 4


def test_header_under_a_dark_status_bar_is_cropped_with_it(tmp_path: Path) -> None:
    """The walk carries on into the app's own colour when words follow."""
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    black = (0, 0, 0)
    draw.rectangle([0, 0, width, 80], fill=black)  # dark status bar over a white app
    _status_glyphs(draw, width, 22, 52, (235, 235, 235))
    draw.polygon([(80, 128), (44, 150), (80, 172)], fill=black)  # back arrow
    draw.rectangle([44, 147, 110, 153], fill=black)
    _text_rows(draw, 150, 420, 136, 164, line=28, gap=0)  # title
    for x in (990, 1005, 1020):
        draw.ellipse([x, 145, x + 8, 153], fill=black)
    _author_row(image, draw, 220, width=width, size=90)
    image.paste(Image.fromarray(_picture(width, 1500)), (0, 360))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert abs(box.top - 360) <= 4


def test_cut_falls_where_the_background_changes_before_the_kept_words(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1170, 2532
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, 249], fill=(235, 235, 235))  # a grey header area
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _author_row(image, draw, 140, width=width)
    _text_rows(draw, 48, width - 48, 270, 354, line=34, gap=16)  # the post's words
    _text_rows(draw, 48, 520, 370, 404, line=34, gap=0)
    image.paste(Image.fromarray(_picture(width - 96, 1460)), (48, 520))
    path = tmp_path / "IMG_0003.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and "top" in box.chrome
    assert box.top == 250


def test_flat_sky_with_birds_under_the_status_bar_is_cut_at_its_edge(tmp_path: Path) -> None:
    """A picture's flat top in its own colour, taller than the look-ahead and
    holding a row of birds, is not more interface."""
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1500)
    picture[:300] = (92, 140, 198)  # sky
    image.paste(Image.fromarray(picture), (0, 120))
    _birds(draw, 200, (90, 300, 520, 760, 950), (30, 30, 30))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert abs(box.top - 120) <= 4


def test_flat_lake_with_boats_above_the_action_row_is_cut_at_its_edge(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1520, seed=22)
    picture[1260:] = (70, 100, 140)  # a lake at the bottom of the photo
    image.paste(Image.fromarray(picture), (0, 400))
    _birds(draw, 1800, (120, 330, 560, 800, 980), (240, 240, 240), size=(30, 12))  # boats
    _icon_row(draw, 1950, 2010, (40, 190, 340), (0, 0, 0), counts=True)
    _nav_buttons(draw, width, height - 82, height - 46, (60, 60, 60))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert abs(box.top - 400) <= 4
    assert abs(box.bottom - 1920) <= 4


def test_stars_in_a_black_sky_are_cropped_with_the_bar(tmp_path: Path) -> None:
    """Sparse stars are ink in a dark band to the aggressive pass: the whole
    sky goes with the status bar. Only a detected person pulls an edge back
    now; a reviewer who wants the sky skips the suggestion."""
    _require_guard()
    from PIL import ImageDraw

    width, height = 1170, 2532
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 52, 92, (255, 255, 255))
    picture = _picture(width, 1800, seed=5)
    picture[:350] = (0, 0, 0)  # night sky, the band's own colour
    image.paste(Image.fromarray(picture), (0, 150))
    for x, y in ((100, 180), (640, 182), (1000, 230), (300, 232), (820, 300), (450, 380),
                 (1100, 383), (200, 450), (700, 452)):
        draw.ellipse([x, y, x + 6, y + 6], fill=(255, 255, 255))
    path = tmp_path / "IMG_0001.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and "top" in box.chrome
    assert box.top >= 450  # through the stars, to where the picture starts


def test_a_head_against_a_white_sky_is_not_interface(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1170, 2532
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _author_row(image, draw, 140, width=width)
    picture = _picture(width, 1500, seed=9)
    picture[:420] = (255, 255, 255)  # an overexposed sky
    image.paste(Image.fromarray(picture), (0, 300))
    draw.ellipse([540, 420, 630, 520], fill=(70, 45, 30))  # a head, then shoulders
    draw.rectangle([560, 515, 610, 560], fill=(200, 160, 130))
    for i, y in enumerate(range(560, 720, 4)):
        half = 30 + i * 6
        draw.rectangle([585 - half, y, 585 + half, y + 4], fill=(30, 60, 140))
    path = tmp_path / "IMG_0002.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 236 <= box.top <= 420  # past the author row, never into the head


def test_legs_on_a_white_floor_are_not_interface(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1520, seed=23)
    picture[1250:] = (255, 255, 255)  # a white studio floor
    image.paste(Image.fromarray(picture), (0, 400))
    for x in (490, 555):  # legs and shoes down to the photo's edge
        draw.rectangle([x, 1700, x + 40, 1900], fill=(200, 160, 130))
        draw.rectangle([x - 10, 1895, x + 50, 1920], fill=(20, 20, 20))
    _icon_row(draw, 1950, 2010, (40, 190, 340), (0, 0, 0), counts=True)
    _nav_buttons(draw, width, height - 82, height - 46, (60, 60, 60))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert abs(box.bottom - 1920) <= 4


def test_narrow_top_of_the_picture_is_not_cut_off(tmp_path: Path) -> None:
    """A bottle's cap and neck touch its textured body: the picture's own outline."""
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    draw.rounded_rectangle([40, 100, 1040, 170], radius=30, fill=(240, 240, 240))  # search
    _text_rows(draw, 90, 500, 122, 148, line=26, gap=0, fill=(150, 150, 150))
    draw.rectangle([510, 260, 570, 300], fill=(40, 40, 40))  # cap
    draw.rectangle([515, 300, 565, 380], fill=(60, 110, 60))  # neck
    for i, y in enumerate(range(380, 420, 4)):  # shoulders
        half = 25 + i * 12
        draw.rectangle([540 - half, y, 540 + half, y + 4], fill=(60, 110, 60))
    image.paste(Image.fromarray(_picture(300, 480, seed=7)), (390, 420))  # body
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert abs(box.top - 260) <= 4


def test_a_caption_line_above_the_picture_is_kept(tmp_path: Path) -> None:
    """A meme's short caption on a white panel is the picture's own words."""
    from PIL import ImageDraw

    width, height = 1170, 2532
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _author_row(image, draw, 140, width=width)
    _text_rows(draw, 330, 840, 340, 380, line=40, gap=0, fill=(0, 0, 0))  # caption
    image.paste(Image.fromarray(_picture(width, 980, seed=4)), (0, 420))
    path = tmp_path / "IMG_0004.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 236 <= box.top <= 340


def test_two_page_dots_are_cropped_with_the_action_row(tmp_path: Path) -> None:
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (255, 255, 255))
    image.paste(Image.fromarray(_picture(width, 1860)), (0, 0))
    draw = ImageDraw.Draw(image)
    black = (0, 0, 0)
    for i, x in enumerate((525, 545)):  # two page dots
        draw.ellipse([x, 1890, x + 10, 1900], fill=(60, 100, 230) if i else (200, 200, 200))
    _icon_row(draw, 1940, 2000, (40, 190, 340), black, counts=True)
    draw.rectangle([990, 1940, 1040, 2000], outline=black, width=5)  # bookmark
    _text_rows(draw, 40, 800, 2030, 2064, line=34, gap=0)  # caption
    _nav_buttons(draw, width, height - 82, height - 46, (60, 60, 60))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("bottom",)
    assert abs(box.bottom - 1860) <= 4


def test_status_bar_over_a_gradient_is_cut_by_a_margin_only(tmp_path: Path) -> None:
    """A gradient drifting past the colour tolerance is no change of background."""
    from PIL import ImageDraw

    width, height = 1170, 2532
    picture = _picture(width, height, seed=6)
    ramp = np.linspace(0, 49, 700)[:, None]
    picture[:700] = (np.array([40, 60, 120]) + ramp).astype(np.uint8)[:, None, :]
    image = Image.fromarray(picture)
    _status_glyphs(ImageDraw.Draw(image), width, 52, 92, (255, 255, 255))
    path = tmp_path / "Screenshot gradient.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",) and 92 < box.top <= 150


def _wallpaper_birds_landscape() -> Image.Image:
    from PIL import ImageDraw

    picture = _picture(1920, 1080, seed=11)
    picture[:400] = (120, 170, 220)
    image = Image.fromarray(picture)
    _birds(ImageDraw.Draw(image), 40, (150, 480, 900, 1300, 1700), (25, 25, 25), size=(34, 12))
    return image


def _wallpaper_birds_portrait() -> Image.Image:
    from PIL import ImageDraw

    picture = _picture(1080, 1920, seed=12)
    picture[:500] = (120, 170, 220)
    image = Image.fromarray(picture)
    _birds(ImageDraw.Draw(image), 60, (60, 200, 340, 740, 880, 1000), (25, 25, 25))
    return image


def _sea_with_boats() -> Image.Image:
    from PIL import ImageDraw

    picture = _picture(1920, 1080, seed=13)
    picture[700:] = (60, 90, 130)
    image = Image.fromarray(picture)
    _birds(ImageDraw.Draw(image), 1030, (120, 500, 900, 1350, 1750), (240, 240, 240),
           size=(40, 20))
    return image


def _sunset() -> Image.Image:
    from PIL import ImageDraw

    picture = _picture(3840, 2160, seed=31)
    picture[:900] = (250, 170, 90)
    image = Image.fromarray(picture)
    ImageDraw.Draw(image).ellipse([1580, 60, 2260, 150], fill=(255, 240, 200))  # the sun
    return image


@pytest.mark.parametrize(
    "build", [_wallpaper_birds_landscape, _wallpaper_birds_portrait, _sea_with_boats, _sunset],
    ids=["birds-landscape", "birds-portrait", "boats", "sun"],
)
def test_photos_of_screen_size_need_more_than_one_bar(tmp_path: Path, build) -> None:
    """A row of objects along a flat edge reads as a bar; on a file that is only
    shaped like a screenshot, one bar with nothing in reach is not enough."""
    path = tmp_path / "wallpaper.jpg"
    build().save(path, quality=92)
    assert detect_black_borders(path) is None


def _meme_caption_below(width: int, height: int) -> Image.Image:
    from PIL import ImageDraw

    image = Image.new("RGB", (width, height), (255, 255, 255))
    image.paste(Image.fromarray(_picture(width, height - 220)), (0, 0))
    _text_rows(ImageDraw.Draw(image), 120, width - 120, height - 190, height - 90, line=60,
               gap=20)
    return image


def _title_above(width: int, height: int) -> Image.Image:
    from PIL import ImageDraw

    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for x in range(width // 2 - 100, width // 2 + 100, 40):  # a sparse centred title
        draw.rectangle([x, 60, x + 28, 100], fill=(20, 20, 20))
    image.paste(Image.fromarray(_picture(width, height - 400)), (0, 220))
    return image


def _glyphs_at_one_side(width: int, height: int) -> Image.Image:
    from PIL import ImageDraw

    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for x in range(40, 240, 40):  # five glyphs at the left only
        draw.rectangle([x, 40, x + 24, 70], fill=(20, 20, 20))
    image.paste(Image.fromarray(_picture(width, height - 400)), (0, 200))
    return image


@pytest.mark.parametrize(
    ("build", "size"),
    [
        (_meme_caption_below, (1080, 1920)),
        (_title_above, (1080, 1920)),
        (_title_above, (1920, 1080)),
        (_glyphs_at_one_side, (1080, 1920)),
    ],
    ids=["caption-below", "title-portrait", "title-landscape", "one-sided"],
)
def test_captions_and_titles_are_not_bars(tmp_path: Path, build, size) -> None:
    path = tmp_path / "Screenshot 2024-01-01 at 10.10.10.png"
    build(*size).save(path)
    assert detect_black_borders(path) is None


def test_status_bar_with_a_call_pill_is_still_a_bar(tmp_path: Path) -> None:
    """An active call's green capsule beside the clock is a second ink about
    as heavy as the first, and one run wider than any glyph."""
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    white = (255, 255, 255)
    _status_glyphs(draw, width, 32, 68, white)
    draw.rounded_rectangle([200, 22, 370, 78], radius=28, fill=(60, 180, 90))
    for x in range(250, 340, 26):
        draw.rectangle([x, 36, x + 14, 64], fill=white)  # the call's timer
    for x in (880, 904, 928, 952, 976):
        draw.rectangle([x, 250, x + 14, 278], fill=white)  # a "13/20" counter: five glyphs
    image.paste(Image.fromarray(_picture(width, 1470)), (0, 416))
    path = tmp_path / "Screenshot_20240101-101010.jpg"
    image.save(path, quality=85)

    box = detect_black_borders(path)
    assert box is not None and "top" in box.chrome
    assert abs(box.top - 416) <= 4


def test_two_inks_of_like_weight_peel_apart() -> None:
    """A white clock beside a green pill, each blended into its surroundings:
    the median of the ink is a colour that is neither, and explains
    nothing, until the peel settles on the ink within reach of it."""
    from dedupe.borders import _measure_rows, _peel_colours

    window = np.zeros((3, 540, 3), dtype=np.int16)
    row = window[1]
    row[36:40] = row[120:124] = (45, 88, 49)  # the pill's edges, blended with the band
    row[40:120] = (89, 176, 98)  # the pill
    row[50:60] = (168, 215, 172)  # its timer, blended with the green
    row[140:200] = (247, 247, 247)  # the clock and icons
    row[200:240] = np.array([(60, 60, 60), (120, 120, 120), (180, 180, 180)] * 14)[:40]

    rows = _measure_rows(window)
    plain = _peel_colours(window, rows.medians, rows.far, rows.one_colour)
    assert plain[1] >= 0.9 and not rows.busy[1]


def test_a_privacy_dot_does_not_end_a_black_bar(tmp_path: Path) -> None:
    """The orange dot a phone shows over a full-screen video sits in the bar."""
    from PIL import ImageDraw

    image = Image.fromarray(_framed(1242, 2688, top=70, bottom=68))
    ImageDraw.Draw(image).ellipse([960, 20, 972, 32], fill=(255, 150, 20))
    path = tmp_path / "video.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box == CropBox(left=0, top=70, right=1242, bottom=2620, width=1242, height=2688)


def _shot(width: int, height: int, colour=(255, 255, 255)):
    """A blank screenshot-sized canvas and its drawing handle."""
    from PIL import ImageDraw

    image = Image.new("RGB", (width, height), colour)
    return image, ImageDraw.Draw(image)


def test_two_birds_at_the_centre_of_a_white_sky_are_the_picture(tmp_path: Path) -> None:
    """Two small marks near the centre of the picture's flat top are its
    birds: wider than tall and alike in colour, they read as neither page
    dots nor a row of icons, and the cut stays above them."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1500, seed=43)
    picture[:250] = (255, 255, 255)  # a white-out sky
    image.paste(Image.fromarray(picture), (0, 150))
    _birds(draw, 250, (505, 560), (30, 30, 30))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 70 < box.top <= 250


def test_three_small_birds_at_the_centre_are_the_picture(tmp_path: Path) -> None:
    """Three marks close together are not a row of icons: icons spread
    across the row, and a word has more letters."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1500, seed=49)
    picture[:250] = (255, 255, 255)
    image.paste(Image.fromarray(picture), (0, 150))
    _birds(draw, 250, (470, 525, 580), (30, 30, 30), size=(20, 8))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 70 < box.top <= 250


def test_two_stars_in_a_black_sky_are_cropped_with_the_bar(tmp_path: Path) -> None:
    _require_guard()
    width, height = 1170, 2532
    image, draw = _shot(width, height, (0, 0, 0))
    _status_glyphs(draw, width, 52, 92, (255, 255, 255))
    picture = _picture(width, 1800, seed=5)
    picture[:250] = (0, 0, 0)  # the night sky
    image.paste(Image.fromarray(picture), (0, 150))
    for x in (560, 610):
        draw.ellipse([x, 260, x + 6, 266], fill=(255, 255, 255))
    path = tmp_path / "IMG_0001.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert box.top >= 380  # the aggressive pass takes the sky with the bar


def test_two_boats_above_the_action_row_are_the_picture(tmp_path: Path) -> None:
    """Under the picture: the navigation bar and the action row go, but two
    small boats on the picture's flat lake, right above the action row, are
    not page dots — they stand wider than tall."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1520, seed=45)
    picture[1300:] = (255, 255, 255)  # fog on the lake
    image.paste(Image.fromarray(picture), (0, 400))
    _birds(draw, 1850, (505, 560), (30, 30, 30), size=(30, 12))  # two boats
    _icon_row(draw, 1950, 2010, (40, 190, 340), (0, 0, 0), counts=True)
    _nav_buttons(draw, width, height - 82, height - 46, (60, 60, 60))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert 70 < box.top <= 400
    assert 1862 <= box.bottom <= 1920


@pytest.mark.parametrize("count", [6, 7])
def test_six_or_seven_page_dots_are_cropped_with_the_action_row(tmp_path: Path, count) -> None:
    """A long carousel's dots read as dots, not as a line of text to keep."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    image.paste(Image.fromarray(_picture(width, 1860, seed=60 + count)), (0, 0))
    x0 = (width - (count * 10 + (count - 1) * 10)) // 2
    for i in range(count):
        fill = (60, 100, 230) if i == count // 2 else (200, 200, 200)
        draw.ellipse([x0 + i * 20, 1890, x0 + i * 20 + 10, 1900], fill=fill)
    _icon_row(draw, 1940, 2000, (40, 190, 340), (0, 0, 0), counts=True)
    draw.rectangle([990, 1940, 1040, 2000], outline=(0, 0, 0), width=5)
    _text_rows(draw, 40, 800, 2030, 2064, line=34, gap=0)
    _nav_buttons(draw, width, height - 82, height - 46, (60, 60, 60))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("bottom",)
    assert box.top == 0 and 1856 <= box.bottom <= 1880


def test_a_poster_of_words_in_the_picture_is_kept(tmp_path: Path) -> None:
    """A narrow block of text 230 rows tall on the picture's flat top is a
    poster in the photo, not an author row: the cut stays above it."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1500, seed=41)
    picture[:350] = (255, 255, 255)  # an overexposed wall
    image.paste(Image.fromarray(picture), (0, 120))
    _text_rows(draw, 300, 760, 170, 400, line=22, gap=6)  # the poster's words
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 70 < box.top <= 170


def test_a_memes_two_short_caption_lines_are_kept(tmp_path: Path) -> None:
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _author_row(image, draw, 140, width=width)
    _text_rows(draw, 330, 840, 330, 370, line=40, gap=0, fill=(0, 0, 0))
    _text_rows(draw, 380, 790, 390, 430, line=40, gap=0, fill=(0, 0, 0))
    image.paste(Image.fromarray(_picture(width, 980, seed=4)), (0, 470))
    path = tmp_path / "IMG_0004.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 236 <= box.top <= 330


def test_a_posts_short_lines_above_its_photo_are_kept(tmp_path: Path) -> None:
    """Three short lines (a list, a poem) are the post's words, kept with
    its picture; only the author row above them goes."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _author_row(image, draw, 140, width=width)
    for y in (270, 320, 370):
        _text_rows(draw, 48, 690, y, y + 34, line=34, gap=0)
    image.paste(Image.fromarray(_picture(width - 96, 1460, seed=8)), (48, 440))
    path = tmp_path / "IMG_0005.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 236 <= box.top <= 270


def _night_sky(height: int, width: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.repeat(rng.integers(0, 7, size=(height, width, 1)), 3, axis=2).astype(np.uint8)


def test_a_moon_in_a_flat_night_sky_is_cropped_as_a_bar(tmp_path: Path) -> None:
    """The strict pass still leaves a moon in a flat night sky alone (only
    screenshots get a speck allowance), but the aggressive pass reads the
    sky as a bar holding sparse ink and crops to the horizon. Nobody is in
    it, so the guard lets the suggestion stand for the reviewer to skip."""
    _require_guard()
    from PIL import ImageDraw

    picture = _picture(1200, 900, seed=1)
    picture[:400] = _night_sky(400, 1200, 11)
    image = Image.fromarray(picture)
    ImageDraw.Draw(image).ellipse([590, 200, 610, 220], fill=(245, 245, 230))
    path = tmp_path / "night.png"
    image.save(path)

    box = detect_black_borders(path)
    # The guard may claim some sky back on a detector hit in the noise
    # below; what matters is the sky is suggested at all (it was None).
    assert box is not None and box.top >= 100


def test_a_mast_rising_through_a_dark_sky_is_cropped_with_it(tmp_path: Path) -> None:
    """A letterboxed photo whose dark sky holds a narrow mast: the strict
    pass still refuses the top edge, but the aggressive pass takes bar and
    sky together down to the horizon. Both bars are cut."""
    _require_guard()
    frame = _framed(1280, 720, top=60, bottom=60)
    frame[60:210] = _night_sky(150, 1280, 5)
    frame[100:210, 634:646] = (200, 200, 190)  # the mast
    path = _save(frame, tmp_path / "clip.png")

    box = detect_black_borders(path)
    assert box is not None
    assert (box.left, box.right, box.bottom) == (0, 1280, 660)
    assert box.top >= 100  # the guard may keep some sky on a detector hit


def test_rows_with_nothing_under_the_mask_have_no_median() -> None:
    from dedupe.borders import _masked_medians

    pixels = np.zeros((2, 4, 3), dtype=np.int16)
    pixels[1] = [(10, 20, 30), (12, 22, 32), (200, 200, 200), (14, 24, 34)]
    mask = np.array([[False] * 4, [True, True, False, True]])

    medians = _masked_medians(pixels, mask)
    assert medians[0].tolist() == [0.0, 0.0, 0.0]
    assert medians[1].tolist() == [12.0, 22.0, 32.0]


# --- marks that look like interface but belong to the picture ---------------

_CURRENT_PAGE = (60, 100, 230)  # a carousel's dot for the page on show
_OTHER_PAGE = (200, 200, 200)


def _fog_scene(seed: int):
    """A white app page: the status bar, then a picture from row 400 whose
    last 220 rows are white fog, ending at row 1920."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1520, seed=seed)
    picture[1300:] = (255, 255, 255)
    image.paste(Image.fromarray(picture), (0, 400))
    return image, draw


def _under_fog(image, draw, path: Path) -> CropBox | None:
    """Add the action row and the navigation bar under the picture, save and
    detect."""
    _icon_row(draw, 1950, 2010, (40, 190, 340), (0, 0, 0), counts=True)
    _nav_buttons(draw, 1080, 2340 - 82, 2340 - 46, (60, 60, 60))
    image.save(path)
    return detect_black_borders(path)


def _sky_scene(seed: int):
    """A white app page: the status bar, then a picture from row 150 whose
    first 250 rows are a white sky."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1500, seed=seed)
    picture[:250] = (255, 255, 255)
    image.paste(Image.fromarray(picture), (0, 150))
    return image, draw


def test_a_watermark_on_the_pictures_flat_bottom_is_kept(tmp_path: Path) -> None:
    """Eight letters centred on the picture's white bottom are its
    watermark: they read as a line of words next to the picture, as a
    caption does, and the cut stays under them."""
    image, draw = _fog_scene(61)
    for x in range(479, 479 + 8 * 16, 16):
        draw.rectangle([x, 1862, x + 10, 1878], fill=(60, 60, 60))
    box = _under_fog(image, draw, tmp_path / "Screenshot_20240101-101010.png")

    assert box is not None and box.chrome == ("top", "bottom")
    assert 1879 <= box.bottom <= 1950


@pytest.mark.parametrize(
    "xs, size, fill",
    [((516, 556), 24, (30, 30, 30)), ((480, 526, 572), 28, (120, 90, 40))],
    ids=["two balls", "three coins"],
)
def test_round_things_of_one_colour_are_not_page_dots(tmp_path: Path, xs, size, fill) -> None:
    """Balls or coins at the centre of the picture's flat bottom are as round
    as page dots, but none of them stands out as the page on show."""
    image, draw = _fog_scene(45)
    for x in xs:
        draw.ellipse([x, 1884 - size, x + size, 1884], fill=fill)
    box = _under_fog(image, draw, tmp_path / "Screenshot_20240101-101010.png")

    assert box is not None and box.chrome == ("top", "bottom")
    assert 1885 <= box.bottom <= 1950


def test_page_dot_colours_above_the_picture_are_its_own(tmp_path: Path) -> None:
    """A carousel puts its dots under the picture. Two round marks of a
    carousel's colours in the picture's white sky, under the status bar, are
    the picture's (balloons): the cut stays above them."""
    image, draw = _sky_scene(43)
    draw.ellipse([520, 250, 530, 260], fill=_CURRENT_PAGE)
    draw.ellipse([540, 250, 550, 260], fill=_OTHER_PAGE)
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 70 < box.top <= 250


@pytest.mark.parametrize(
    "marks",
    [
        [([490, 1850, 500, 1860], _CURRENT_PAGE), ([580, 1850, 590, 1860], _OTHER_PAGE)],
        [([496, 1854, 530, 1884], _CURRENT_PAGE), ([550, 1854, 584, 1884], _OTHER_PAGE)],
        [([500, 1850, 510, 1860], _CURRENT_PAGE), ([520, 1850, 530, 1860], _OTHER_PAGE),
         ([555, 1850, 565, 1860], _OTHER_PAGE)],
        [([500, 1850, 510, 1860], _CURRENT_PAGE), ([520, 1850, 530, 1860], _OTHER_PAGE),
         ([546, 1850, 556, 1860], _OTHER_PAGE)],
    ],
    ids=["far apart", "wider than a dot", "unevenly spaced", "a little unevenly spaced"],
)
def test_marks_unlike_page_dots_are_the_pictures(tmp_path: Path, marks) -> None:
    """Page dots are small, close together and evenly spaced. Round marks in
    a carousel's colours on the picture's flat lake that are farther apart
    than a few dots, wider than any dot, or unevenly spaced — even 20 and 26
    pixels apart — are buoys: the cut stays under them."""
    image, draw = _fog_scene(45)
    for bounds, fill in marks:
        draw.ellipse(bounds, fill=fill)
    box = _under_fog(image, draw, tmp_path / "Screenshot_20240101-101010.png")

    assert box is not None and box.chrome == ("top", "bottom")
    assert max(bounds[3] for bounds, _ in marks) + 1 <= box.bottom <= 1950


def test_faint_specks_do_not_bring_far_marks_together(tmp_path: Path) -> None:
    """Two buoys in a carousel's colours, too far apart for page dots, with
    two faint specks of the fog between them: a mark too faint and narrow to
    be a dot does not close the gap, and the cut stays under the buoys."""
    image, draw = _fog_scene(45)
    draw.ellipse([490, 1850, 500, 1860], fill=_CURRENT_PAGE)
    draw.ellipse([580, 1850, 590, 1860], fill=_OTHER_PAGE)
    for x in (520, 552):
        draw.rectangle([x, 1854, x + 3, 1856], fill=(228, 228, 228))
    box = _under_fog(image, draw, tmp_path / "Screenshot_20240101-101010.png")

    assert box is not None and box.chrome == ("top", "bottom")
    assert 1861 <= box.bottom <= 1950


def _sign_with_a_stripe(draw) -> None:
    _text_rows(draw, 300, 760, 170, 300, line=22, gap=6)
    draw.rectangle([300, 226, 760, 246], fill=(150, 30, 30))


def _sign_beside_a_disc(draw) -> None:
    draw.ellipse([300, 180, 400, 280], fill=(30, 60, 160))
    _text_rows(draw, 420, 760, 170, 310, line=22, gap=6)


@pytest.mark.parametrize("paint", [_sign_with_a_stripe, _sign_beside_a_disc],
                         ids=["stripe", "disc"])
def test_a_sign_of_words_and_a_shape_is_the_picture(tmp_path: Path, paint) -> None:
    """A block of words a little taller than any interface row, with a
    stripe through it or a disc set in from the margin, is a sign on the
    picture's white wall, not an author row with its avatar: the cut stays
    above it."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1500, seed=41)
    picture[:350] = (255, 255, 255)
    image.paste(Image.fromarray(picture), (0, 120))
    paint(draw)
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 70 < box.top <= 170


def test_birds_spread_across_a_white_sky_are_the_picture(tmp_path: Path) -> None:
    """Five birds across the picture's white sky stand in a row like icons,
    but no icon or button is that thin: the cut stays above them."""
    image, draw = _sky_scene(43)
    _birds(draw, 250, (90, 300, 520, 760, 950), (30, 30, 30))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 70 < box.top <= 250


def test_boats_spread_across_white_fog_are_the_picture(tmp_path: Path) -> None:
    image, draw = _fog_scene(53)
    _birds(draw, 1850, (120, 330, 560, 800, 980), (30, 30, 30), size=(30, 12))
    box = _under_fog(image, draw, tmp_path / "Screenshot_20240101-101010.png")

    assert box is not None and box.chrome == ("top", "bottom")
    assert 1863 <= box.bottom <= 1950


def test_a_tight_flock_is_not_a_word(tmp_path: Path) -> None:
    """Four birds a letter's gap apart line up like a word's letters, but
    each is wider than the row is tall, as no letter is."""
    image, draw = _sky_scene(46)
    _birds(draw, 250, (470, 511, 552, 593), (30, 30, 30), size=(30, 20))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 70 < box.top <= 250


def test_an_author_row_with_a_large_avatar_is_cropped(tmp_path: Path) -> None:
    """An avatar taller than any other interface element, the name beside
    it: still an author row, which goes up to the picture."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    y, size = 140, 150
    _avatar(image, 48, y, size)
    _text_rows(draw, 228, 700, y + 20, y + 50, line=30, gap=0)
    _text_rows(draw, 228, 560, y + 70, y + 94, line=24, gap=0, fill=(120, 120, 120))
    for x in (width - 110, width - 90, width - 70):
        draw.ellipse([x, y + 60, x + 8, y + 68], fill=(0, 0, 0))
    image.paste(Image.fromarray(_picture(width, 1300, seed=9)), (0, y + size + 30))
    path = tmp_path / "IMG_0006.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert y + size <= box.top <= y + size + 30


def test_a_lone_short_line_under_the_status_bar_goes_with_it(tmp_path: Path) -> None:
    """One short line between the status bar and the picture reads as a
    header's title and is cut with the bar (so is a meme's one-line caption
    there: a known limit)."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _text_rows(draw, 330, 840, 180, 220, line=40, gap=0, fill=(0, 0, 0))
    image.paste(Image.fromarray(_picture(width, 980, seed=4)), (0, 280))
    path = tmp_path / "IMG_0007.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 220 < box.top <= 280


def test_an_action_row_is_not_kept_with_the_caption_under_the_picture(tmp_path: Path) -> None:
    """The caption under the picture is kept; the action row a line apart
    from it, its icons taller than the caption's letters, goes."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    image.paste(Image.fromarray(_picture(width, 1900, seed=67)), (0, 0))
    _text_rows(draw, 40, 620, 1920, 1954, line=34, gap=0)  # the caption
    _icon_row(draw, 1990, 2050, (40, 190, 340), (0, 0, 0), counts=True)
    _nav_buttons(draw, width, height - 82, height - 46, (60, 60, 60))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("bottom",)
    assert 1954 < box.bottom <= 1990


def test_a_header_title_is_not_kept_with_the_caption_above_the_picture(tmp_path: Path) -> None:
    """The caption above the picture is kept; the header's title, set off
    from it by more than a line, goes with the status bar."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    draw.polygon([(80, 134), (44, 150), (80, 166)], fill=(0, 0, 0))  # back arrow
    for x in range(480, 480 + 7 * 18, 18):
        draw.rectangle([x, 134, x + 12, 164], fill=(0, 0, 0))  # the title
    _text_rows(draw, 40, 620, 230, 264, line=34, gap=0)  # the caption
    image.paste(Image.fromarray(_picture(width, 1500, seed=65)), (0, 300))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 164 < box.top <= 230


@pytest.mark.parametrize(
    "y, size", [(320, (28, 10)), (170, (20, 30))], ids=["birds", "marks as tall as the title"]
)
def test_a_header_title_does_not_hold_the_cut_above_a_sky(tmp_path: Path, y, size) -> None:
    """A dark header with its title, then the switch into the picture's white
    sky, holding a row of birds: the switch is the picture's edge, and the
    title goes with the bar however like a line of text the birds look."""
    width, height = 1080, 2340
    image, draw = _shot(width, height, (0, 0, 0))
    _status_glyphs(draw, width, 31, 70, (255, 255, 255))
    _text_rows(draw, 420, 660, 100, 140, line=40, gap=0, fill=(255, 255, 255))
    picture = _picture(width, 1500, seed=5)
    picture[:300] = (255, 255, 255)
    image.paste(Image.fromarray(picture), (0, 160))
    _birds(draw, y, tuple(range(300, 700, 60)), (30, 30, 30), size=size)
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert abs(box.top - 160) <= 4


def test_a_short_counter_alone_is_cropped_with_the_band(tmp_path: Path) -> None:
    """A three-glyph counter ("1/3") alone in the band is sparse ink to the
    aggressive pass: band and counter are cropped together, right down to
    where the picture starts (the cut still never reaches into it)."""
    _require_guard()
    from PIL import ImageDraw

    width, height = 1080, 2340
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 32, 68, (255, 255, 255))
    for x in (900, 924, 948):
        draw.rectangle([x, 250, x + 14, 278], fill=(255, 255, 255))
    image.paste(Image.fromarray(_picture(width, 1470)), (0, 416))
    path = tmp_path / "Screenshot_20240101-101010.jpg"
    image.save(path, quality=85)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 400 <= box.top <= 416


def _letterboxed_moon() -> np.ndarray:
    frame = _framed(1300, 720, top=60, bottom=60)
    frame[60:300] = _night_sky(240, 1300, 11)
    frame[150:171, 640:661] = (245, 245, 230)
    return frame


def _phone_moon() -> np.ndarray:
    frame = _framed(1242, 2688, top=600, bottom=600)
    frame[600:1100] = _night_sky(500, 1242, 11)
    frame[800:821, 610:631] = (245, 245, 230)
    return frame


def _side_lamp() -> np.ndarray:
    frame = _framed(1242, 2688, left=120, right=120)
    frame[:, 120:400] = _night_sky(2688, 280, 3)
    frame[1300:1341, 250:291] = (240, 240, 220)
    return frame


def _dashed_rope() -> np.ndarray:
    frame = _framed(1242, 2688, top=30, bottom=60)
    frame[30:400] = _night_sky(370, 1242, 7)
    for y in range(40, 77, 4):
        frame[y : y + 3, 615:627] = (200, 200, 190)  # three rows on, one off
    return frame


@pytest.mark.parametrize(
    "build, name, expected",
    [
        (_letterboxed_moon, "Screenshot 2024-01-01 at 10.10.10.png", (0, 300, 1300, 660)),
        (_phone_moon, "night.png", (0, 1100, 1242, 2088)),
        (_side_lamp, "IMG_0008.PNG", (400, 0, 1122, 2688)),
        (_dashed_rope, "IMG_0009.PNG", (0, 400, 1242, 2628)),
    ],
    ids=["moon below the status bar's rows", "moon on a phone-shaped frame",
         "lamp by a side bar", "rope through the status bar's rows"],
)
def test_specks_deep_in_a_dark_sky_are_cropped_through(
    tmp_path: Path, build, name, expected
) -> None:
    """The strict speck allowance still ends a strict bar at a deep speck,
    but the final suggestion now follows the aggressive pass: a moon deeper
    in a flat night sky, a lamp by a side bar, or a rope through the top
    rows is sparse ink, cropped with the sky up to its horizon."""
    _require_guard()
    frame = build()
    path = _save(frame, tmp_path / name)

    box = detect_black_borders(path)
    height, width = frame.shape[:2]
    assert box == CropBox(*expected, width=width, height=height)


def test_a_privacy_dot_rides_along_when_only_the_bottom_is_interface(tmp_path: Path) -> None:
    """A full-screen video with the home indicator over its bottom bar: the
    walk takes that bar, and the privacy dot in the top bar still does not
    end the top bar."""
    from PIL import ImageDraw

    image = Image.fromarray(_framed(1242, 2688, top=70, bottom=68))
    draw = ImageDraw.Draw(image)
    draw.ellipse([960, 20, 972, 32], fill=(255, 150, 20))
    draw.rounded_rectangle([486, 2650, 756, 2662], radius=6, fill=(200, 200, 200))
    path = tmp_path / "IMG_0010.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("bottom",)
    assert (box.top, box.bottom) == (70, 2620)


def test_a_moon_below_the_status_bar_is_cropped_with_the_sky(tmp_path: Path) -> None:
    """Once the walk has taken the status bar, the strict pass keeps a moon
    below it as the picture's — but the aggressive pass takes the whole
    night sky the moon sits in, down to where the picture starts."""
    _require_guard()
    from PIL import ImageDraw

    width, height = 1170, 2532
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    _status_glyphs(draw, width, 52, 92, (255, 255, 255))
    picture = _picture(width, 1800, seed=5)
    picture[:400] = _night_sky(400, width, 11)
    image.paste(Image.fromarray(picture), (0, 150))
    draw.ellipse([575, 350, 595, 370], fill=(245, 245, 230))
    path = tmp_path / "IMG_0011.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert box.top >= 500


def test_a_few_stray_pixels_do_not_settle_the_peel() -> None:
    """When the median of two inks is a colour that is neither, the peel
    settles on the ink within reach of it only when that is a real share of
    what is left: ten stray pixels of compression do not make the row read
    plainer."""
    from dedupe.borders import _measure_rows, _peel_colours

    window = np.zeros((3, 540, 3), dtype=np.int16)
    row = window[1]
    row[100:145] = (247, 200, 247)  # one ink
    row[200:245] = (40, 176, 40)  # another, of like weight
    row[300:310] = (120, 255, 120)  # strays within reach of their median

    rows = _measure_rows(window)
    plain = _peel_colours(window, rows.medians, rows.far, rows.one_colour)
    assert plain[1] == pytest.approx(440 / 540)


# --- the post's words, author rows and specks: a fourth review ---------------


def _feed_page(seed: int = 0):
    """A white feed page: the status bar, then an author row at rows 100-196."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    _author_row(image, draw, 100, width=width)
    return image, draw


def test_paragraphs_a_blank_line_apart_are_kept(tmp_path: Path) -> None:
    """A post's two paragraphs above its photo, a blank line apart, on one
    margin: the blank line does not end the post's words."""
    image, draw = _feed_page()
    for y, right in ((240, 1040), (287, 700), (381, 560)):
        _text_rows(draw, 40, right, y, y + 34, line=34, gap=0)
    image.paste(Image.fromarray(_picture(1080, 1500, seed=12)), (0, 455))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 197 <= box.top <= 240


@pytest.mark.parametrize(
    "lines",
    [[(30, 735)], [(30, 700), (30, 820)], [(30, 400), (30, 110)]],
    ids=["two thirds of the width", "wrapped", "a short last word"],
)
def test_a_memes_caption_lines_of_any_width_are_kept(tmp_path: Path, lines) -> None:
    """A meme's caption in its own white band, which a white feed hides: a
    line two thirds of the width, too wide for a short line and too narrow
    for text across the width, or a last word of a few letters, is still
    the caption, and the cut stays above it."""
    image, draw = _feed_page()
    y = 230
    for left, right in lines:
        _text_rows(draw, left, right, y, y + 44, line=44, gap=0, fill=(0, 0, 0))
        y += 64
    image.paste(Image.fromarray(_picture(1080, 1500, seed=21)), (0, y + 20))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 197 <= box.top <= 230


@pytest.mark.parametrize("pad", [4, 8])
def test_a_caption_set_tight_above_its_photo_is_kept(tmp_path: Path, pad: int) -> None:
    """A caption a few pixels above its photo sits within the walk's gap of
    it, but is still the post's words."""
    image, draw = _feed_page()
    _text_rows(draw, 300, 780, 230, 278, line=48, gap=0, fill=(0, 0, 0))
    image.paste(Image.fromarray(_picture(1080, 1500, seed=14)), (0, 279 + pad))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 197 <= box.top <= 230


def _tall_author_row(image, draw, y: int, *, ring: bool, button: bool) -> None:
    """An author row taller than any other interface row: a 120-pixel
    avatar (in a story's ring, a white gap inside it, when asked), the name
    and handle, and a filled button at the right margin when asked."""
    if ring:
        draw.ellipse([36, y - 12, 180, y + 132], outline=(220, 40, 120), width=7)
    _avatar(image, 48, y, 120)
    _text_rows(draw, 200, 560, y + 10, y + 38, line=28, gap=0, fill=(0, 0, 0))
    _text_rows(draw, 200, 420, y + 56, y + 80, line=24, gap=0, fill=(110, 110, 110))
    if button:  # beside the handle: the name and the button together would fill half a row
        draw.rounded_rectangle([840, y + 44, 1122, y + 104], radius=30, fill=(15, 15, 15))


@pytest.mark.parametrize("ring, button", [(False, True), (True, False)],
                         ids=["a wide button", "a story's ring"])
def test_a_tall_author_row_is_cropped(tmp_path: Path, ring: bool, button: bool) -> None:
    """The avatar is the run of ink at a margin: a button wider than a pill
    at the other margin does not unmake it, and in a story's ring it is the
    picture inside the ring."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _tall_author_row(image, draw, 309, ring=ring, button=button)
    image.paste(Image.fromarray(_picture(width, 1500, seed=19)), (0, 480))
    path = tmp_path / "IMG_0007.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 442 <= box.top <= 480


@pytest.mark.parametrize("lines", [1, 2])
def test_a_timeline_posts_words_beside_the_avatar_are_kept(tmp_path: Path, lines: int) -> None:
    """On a timeline a post's first line sits beside the avatar, under the
    name, and its picture is set in the words' column: the author row stays
    with the words, as a cut between them would cut the avatar."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _avatar(image, 48, 177, 120)
    _text_rows(draw, 200, 620, 185, 215, line=30, gap=0, fill=(0, 0, 0))  # name, handle
    y = 249
    for right in (760, 1100)[:lines]:
        _text_rows(draw, 200, right, y, y + 40, line=40, gap=0)
        y += 60
    image.paste(Image.fromarray(_picture(922, 900, seed=31)), (200, y + 18))
    path = tmp_path / "IMG_0008.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 92 < box.top <= 177


def test_an_action_row_under_a_caption_across_the_width_goes(tmp_path: Path) -> None:
    """An action row, then a short line, under a caption spanning the width
    that sits under the picture: the caption is kept, the icons are not
    words and go with the interface."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    image.paste(Image.fromarray(_picture(width - 96, 1500, seed=8)), (48, 300))
    _text_rows(draw, 48, 1122, 1830, 1914, line=34, gap=16)
    _icon_row(draw, 1934, 1994, (60, 330, 600), (90, 90, 90), counts=True)
    _text_rows(draw, 48, 520, 2034, 2068, line=34, gap=0)
    draw.rounded_rectangle([395, 2496, 775, 2510], radius=7, fill=(0, 0, 0))  # home pill
    path = tmp_path / "IMG_0002.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert 1914 < box.bottom <= 1934


def test_a_title_above_words_across_the_width_goes(tmp_path: Path) -> None:
    """A header's short title set two lines above the post's words, which
    span the width: text across the width is centred on any title, so only
    a shared margin would join them, and the title goes with the bar."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    for x in range(475, 700, 16):  # the title's letters, centred on the words below
        draw.rectangle([x, 150, x + 9, 180], fill=(0, 0, 0))
    _text_rows(draw, 48, 1122, 240, 330, line=30, gap=12)
    image.paste(Image.fromarray(_picture(width, 1500, seed=8)), (0, 380))
    path = tmp_path / "IMG_0009.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 180 < box.top <= 240


def test_a_small_label_alone_under_the_status_bar_goes_with_it(tmp_path: Path) -> None:
    """A label of a few letters, shorter than an icon, is a word of
    interface where birds of that height are not: alone under the status
    bar, it goes with it."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    for x in range(48, 48 + 5 * 13, 13):
        draw.rectangle([x, 150, x + 9, 161], fill=(120, 120, 120))
    image.paste(Image.fromarray(_picture(width, 1500, seed=8)), (0, 200))
    path = tmp_path / "IMG_0010.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 162 <= box.top <= 200


@pytest.mark.parametrize("count", [8, 10])
def test_many_thin_birds_across_a_white_sky_are_the_picture(tmp_path: Path, count: int) -> None:
    """However many birds stand across the picture's white sky, none is as
    tall as an icon: the cut stays above them."""
    image, draw = _sky_scene(43)
    _birds(draw, 250, [60 + i * 930 // (count - 1) for i in range(count)], (30, 30, 30),
           size=(29, 11))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 70 < box.top <= 250


def test_three_marks_of_three_colours_are_not_page_dots(tmp_path: Path) -> None:
    """Of a carousel's dots only the page on show stands out; three round
    marks of three colours on the picture's white bottom are its own."""
    image, draw = _fog_scene(45)
    for x, fill in ((515, _CURRENT_PAGE), (535, _OTHER_PAGE), (555, (160, 160, 160))):
        draw.ellipse([x, 1850, x + 10, 1860], fill=fill)
    box = _under_fog(image, draw, tmp_path / "Screenshot_20240101-101010.png")

    assert box is not None and box.chrome == ("top", "bottom")
    assert 1861 <= box.bottom <= 1950


def _sign_crossed_by_bars(draw) -> None:
    _text_rows(draw, 60, 700, 170, 300, line=22, gap=6)
    draw.rectangle([60, 196, 700, 222], fill=(150, 30, 30))
    draw.rectangle([60, 250, 700, 276], fill=(150, 30, 30))


def _sign_beside_a_tab(draw) -> None:
    draw.rectangle([60, 226, 210, 240], fill=(150, 30, 30))
    _text_rows(draw, 230, 760, 170, 300, line=22, gap=6)


@pytest.mark.parametrize("paint", [_sign_crossed_by_bars, _sign_beside_a_tab],
                         ids=["crossed by bars", "beside a tab"])
def test_a_sign_at_the_margin_is_no_author_row(tmp_path: Path, paint) -> None:
    """A sign at the picture's left margin, a little taller than any
    interface row: bars wider than a pill from the margin, or a tab too
    short for an avatar, make no author row of it."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    picture = _picture(width, 1500, seed=41)
    picture[:350] = (255, 255, 255)
    image.paste(Image.fromarray(picture), (0, 120))
    paint(draw)
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 70 < box.top <= 170


def test_a_privacy_dot_by_the_dynamic_island_rides_in_the_bar(tmp_path: Path) -> None:
    """A Dynamic Island iPhone shows its privacy dot about thirty points
    down, deeper than a notched one: it still rides in the black bar over a
    full-screen video."""
    from PIL import ImageDraw

    image = Image.fromarray(_framed(1179, 2556, top=946, bottom=946))
    ImageDraw.Draw(image).ellipse([760, 80, 776, 96], fill=(255, 149, 0))
    path = tmp_path / "IMG_0011.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and (box.top, box.bottom) == (946, 1610)


@pytest.mark.parametrize(
    "width, height, y, quality",
    [
        (1125, 2436, 15, 80),
        (1125, 2436, 35, 75),
        (1284, 2778, 15, 80),
        (1125, 2436, 8, 75),
        (1125, 2436, 22, 75),
    ],
)
def test_a_privacy_dots_jpeg_ringing_does_not_end_a_black_bar(
    tmp_path: Path, width: int, height: int, y: int, quality: int
) -> None:
    """Saved as a JPEG, the privacy dot over a full-screen video rings:
    faint marks on the lines at the bar's level around it, within the 16
    lines a JPEG spreads colour over, and beside it on its own lines. They
    make the dot neither taller nor wider than a speck, and it still rides
    in the black bar."""
    from PIL import ImageDraw

    bar = int(height * 0.30)
    image = Image.fromarray(_framed(width, height, top=bar, bottom=bar))
    x = int(width * 0.82)
    ImageDraw.Draw(image).ellipse([x, y, x + 17, y + 17], fill=(255, 149, 0))
    path = tmp_path / "IMG_0011.JPG"
    image.save(path, quality=quality)

    box = detect_black_borders(path)
    assert box is not None and abs(box.top - bar) <= 2


def test_a_light_atop_a_thin_mast_is_cropped_with_the_sky(tmp_path: Path) -> None:
    """A red light on a mast too thin to lift a row off the sky's level is
    still no speck to the strict pass, but the aggressive pass reads the
    whole sky as a bar with sparse ink and crops to the picture below."""
    _require_guard()
    from PIL import ImageDraw

    picture = _picture(1080, 1920, seed=6)
    picture[:900] = _night_sky(900, 1080, 13)
    picture[30:900, 598:601] = (70, 70, 70)
    image = Image.fromarray(picture)
    ImageDraw.Draw(image).ellipse([592, 14, 607, 29], fill=(230, 40, 40))
    path = tmp_path / "night.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.top >= 850


def test_an_author_rows_two_lines_over_a_picture_across_the_width_go(tmp_path: Path) -> None:
    """An author row whose name and time stand a little apart beside the
    avatar, over a picture reaching under the avatar: the lines are the
    row's own, not a post's words, and the row goes."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    _avatar(image, 48, 110, 100)
    _text_rows(draw, 170, 480, 122, 148, line=26, gap=0, fill=(0, 0, 0))  # name
    _text_rows(draw, 170, 360, 168, 188, line=20, gap=0, fill=(120, 120, 120))  # time
    image.paste(Image.fromarray(_picture(width, 1500, seed=23)), (0, 250))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 210 < box.top <= 250


def test_a_headline_right_above_words_across_the_width_is_kept(tmp_path: Path) -> None:
    """A post's headline in letters nearly twice the height of its words
    across the width, set within its own height above them, is part of the
    post and stays with them."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _text_rows(draw, 48, 700, 160, 216, line=56, gap=0, fill=(0, 0, 0))
    _text_rows(draw, 48, 1122, 250, 340, line=30, gap=12)
    image.paste(Image.fromarray(_picture(width, 1500, seed=8)), (0, 380))
    path = tmp_path / "IMG_0012.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 92 < box.top <= 160


def test_a_title_and_its_icon_a_blank_line_above_the_words_go(tmp_path: Path) -> None:
    """A header's title with an icon a wide blank from it, a blank line above
    the post's words on the same margin: the blank inside it marks it as no
    line of the post, and it goes with the bar."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    for x in range(48, 400, 16):
        draw.rectangle([x, 150, x + 9, 186], fill=(0, 0, 0))
    draw.rectangle([490, 152, 526, 184], outline=(0, 0, 0), width=4)  # the icon
    _text_rows(draw, 48, 760, 250, 286, line=36, gap=0)
    _text_rows(draw, 48, 520, 298, 334, line=36, gap=0)
    image.paste(Image.fromarray(_picture(width, 1500, seed=8)), (0, 380))
    path = tmp_path / "IMG_0013.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 186 < box.top <= 250


def test_a_caption_line_with_dotted_letters_is_one_line_high(tmp_path: Path) -> None:
    """The dots over a caption's i's and j's stand a few rows above its
    letters: they do not make the line read shorter than the next one, and
    the two lines stay one caption."""
    image, draw = _feed_page()
    _text_rows(draw, 30, 760, 240, 284, line=44, gap=0, fill=(0, 0, 0))
    for x in range(30, 760, 64):  # the dots over the i's
        draw.rectangle([x, 230, x + 9, 236], fill=(0, 0, 0))
    _text_rows(draw, 30, 600, 304, 348, line=44, gap=0, fill=(0, 0, 0))
    image.paste(Image.fromarray(_picture(1080, 1500, seed=21)), (0, 380))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 197 <= box.top <= 230


def _emoji(draw, x: int, y: int, size: int) -> None:
    """A laughing face: a yellow disc with dark eyes and a smile."""
    draw.ellipse([x, y, x + size, y + size], fill=(255, 204, 51))
    for left in (0.25, 0.62):
        draw.ellipse([x + size * left, y + size * 0.3, x + size * (left + 0.13), y + size * 0.48],
                     fill=(90, 50, 20))
    draw.arc([x + size * 0.2, y + size * 0.35, x + size * 0.8, y + size * 0.8], 20, 160,
             fill=(90, 50, 20), width=4)


@pytest.mark.parametrize("toolbar", [True, False], ids=["toolbar", "navigation bar alone"])
def test_a_caption_band_under_its_photo_is_kept_whole(tmp_path: Path, toolbar: bool) -> None:
    """A dark viewer shows an image whose caption sits in a white band under
    its photo, set a few pixels from it. The band and its caption are the
    image's own: the cut stays under the band, not at the photo."""
    width, height = 1080, 2340
    image, draw = _shot(width, height, (0, 0, 0))
    _status_glyphs(draw, width, 31, 70, (255, 255, 255))
    bottom = 2000 if toolbar else 2140
    top = bottom - 250
    image.paste(Image.fromarray(_picture(width, 1200, seed=3)), (0, top - 1200))
    draw.rectangle([0, top, width - 1, bottom - 1], fill=(255, 255, 255))
    _text_rows(draw, 260, 760, top + 5, top + 51, line=46, gap=0, fill=(20, 20, 20))
    if toolbar:
        _emoji(draw, 770, top + 1, 54)
        _icon_row(draw, height - 200, height - 140, (90, 360, 630, 900), (255, 255, 255))
    _nav_buttons(draw, width, height - 82, height - 46, (220, 220, 220))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert abs(box.top - (top - 1200)) <= 4
    assert bottom <= box.bottom <= bottom + 100


def test_a_caption_band_above_its_photo_is_kept_whole(tmp_path: Path) -> None:
    """The same band above the photo, under the app's header: the caption set
    tight against the photo does not hold the cut there."""
    width, height = 1080, 2340
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 31, 70, (0, 0, 0))
    draw.polygon([(80, 108), (44, 130), (80, 152)], fill=(0, 0, 0))  # back arrow
    for x in range(150, 400, 16):  # title
        draw.rectangle([x, 115, x + 9, 145], fill=(0, 0, 0))
    picture = _picture(width, 1800, seed=12)
    picture[:240] = (250, 225, 60)
    image.paste(Image.fromarray(picture), (0, 200))
    _text_rows(draw, 260, 760, 389, 435, line=46, gap=0, fill=(20, 20, 20))
    _emoji(draw, 770, 385, 54)
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 152 < box.top <= 200


def test_a_short_row_of_boats_on_white_fog_is_the_picture(tmp_path: Path) -> None:
    """Six boats in a row on the picture's white fog, within the middle of
    the width, line up like a short line of letters, though they hold no
    word. As the row next to the picture, they are its own: the cut stays
    under them."""
    image, draw = _fog_scene(53)
    _birds(draw, 1850, [250 + 110 * i for i in range(6)], (30, 30, 30), size=(30, 12))
    box = _under_fog(image, draw, tmp_path / "Screenshot_20240101-101010.png")

    assert box is not None and box.chrome == ("top", "bottom")
    assert 1863 <= box.bottom <= 1950


def test_a_short_row_of_birds_under_the_author_row_is_the_picture(tmp_path: Path) -> None:
    """The same above the picture: six birds in a row in its white sky, under
    the author row, are the picture's, and the cut stays above them."""
    image, draw = _feed_page()
    picture = _picture(1080, 1500, seed=70)
    picture[:150] = (255, 255, 255)
    image.paste(Image.fromarray(picture), (0, 330))
    _birds(draw, 430, [280 + 85 * i for i in range(6)], (40, 40, 40))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 197 <= box.top <= 430


def _white_cornered(width: int, height: int, seed: int, *, reach: int, wall: bool) -> np.ndarray:
    """A picture whose top-left corner is blown-out white sky reaching
    ``reach`` pixels along its top edge, or with a white wall that wide down
    its left side."""
    picture = _picture(width, height, seed=seed).astype(np.float64)
    if wall:
        picture[:, :reach] = 255
    else:
        rows, columns = np.mgrid[0:height, 0:width]
        distance = (columns / reach) ** 2 + (rows / (0.6 * reach)) ** 2
        sky = np.clip((1.15 - distance) / 0.3, 0, 1)[..., None]
        picture = picture * (1 - sky) + 255 * sky
    return picture.astype(np.uint8)


@pytest.mark.parametrize(
    "reach, wall",
    [(290, False), (290, True), (190, False)],
    ids=["white sky in its corner", "white wall down its side", "white sky to the words"],
)
def test_an_author_row_over_a_picture_with_a_white_corner_goes(
    tmp_path: Path, reach: int, wall: bool
) -> None:
    """A Facebook post's author row over a photo reaching under the avatar,
    whose top-left corner is the page's white — a blown-out sky, a wall.
    The photo's side does not both line up with the name and run straight
    down, as a timeline post's picture set in the words' column does, and
    the row goes."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    _avatar(image, 36, 170, 120)
    _text_rows(draw, 180, 420, 182, 218, line=36, gap=0, fill=(0, 0, 0))  # name
    _text_rows(draw, 180, 380, 240, 268, line=28, gap=0, fill=(101, 103, 107))  # time
    for x in (990, 1006, 1022):  # the menu, level with the name
        draw.ellipse([x, 195, x + 10, 205], fill=(101, 103, 107))
    picture = _white_cornered(width, 1400, 1, reach=reach, wall=wall)
    image.paste(Image.fromarray(picture), (0, 330))
    path = tmp_path / "IMG_0103.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and "top" in box.chrome
    assert abs(box.top - 330) <= 4


def test_a_collages_labels_tight_over_its_photos_keep_the_posts_words(tmp_path: Path) -> None:
    """A before-and-after collage in a feed: its labels sit centred over its
    two photos, a few pixels above them, spread too wide for a short line
    and too sparse for a caption. They do not stop the post's words above
    from being kept with the picture."""
    width = 1080
    image, draw = _feed_page()
    _text_rows(draw, 40, width - 40, 240, 274, line=34, gap=0)  # the post's words
    _text_rows(draw, 160, 380, 330, 370, line=40, gap=0, fill=(0, 0, 0))  # "Before"
    _text_rows(draw, 700, 900, 330, 370, line=40, gap=0, fill=(0, 0, 0))  # "After"
    half = width // 2 - 4
    image.paste(Image.fromarray(_picture(half, 1200, seed=3)), (0, 376))
    image.paste(Image.fromarray(_picture(half, 1200, seed=4)), (half + 8, 376))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 197 <= box.top <= 240


def test_a_grey_time_a_blank_line_under_the_caption_goes(tmp_path: Path) -> None:
    """Under a photo, the post's caption and, a blank line below it, the
    time it was posted in smaller grey letters: the time is the app's, no
    paragraph of the caption, and the cut falls between them."""
    width, height = 1170, 2532
    image, draw = _shot(width, height)
    _status_glyphs(draw, width, 52, 92, (0, 0, 0))
    image.paste(Image.fromarray(_picture(width, 1642, seed=8)), (0, 200))  # rows 200-1842
    _text_rows(draw, 48, 1122, 1890, 1924, line=34, gap=0, fill=(0, 0, 0))  # caption
    _text_rows(draw, 48, 700, 1937, 1971, line=34, gap=0, fill=(0, 0, 0))
    _text_rows(draw, 48, 400, 2030, 2054, line=24, gap=0, fill=(140, 140, 140))  # time
    draw.rounded_rectangle([395, 2496, 775, 2510], radius=7, fill=(0, 0, 0))  # home indicator
    path = tmp_path / "IMG_0012.PNG"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert 1971 <= box.bottom <= 2030


def test_paragraphs_of_one_size_in_two_inks_are_kept(tmp_path: Path) -> None:
    """A post's two paragraphs of one size a blank line apart, the first in
    a lighter grey: lighter ink alone does not make it the app's, and both
    stay with the picture."""
    image, draw = _feed_page()
    for y, right in ((240, 1040), (287, 700)):
        _text_rows(draw, 40, right, y, y + 34, line=34, gap=0, fill=(140, 140, 140))
    _text_rows(draw, 40, 560, 381, 415, line=34, gap=0, fill=(0, 0, 0))
    image.paste(Image.fromarray(_picture(1080, 1500, seed=12)), (0, 455))
    path = tmp_path / "Screenshot_20240101-101010.png"
    image.save(path)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top",)
    assert 197 <= box.top <= 240


# --- galleries, media viewers and stories -------------------------------------


def _gallery(*, gap: bool = True, seed: int = 5) -> Image.Image:
    """An Android gallery: the photo, its rounded bottom on a navy band that
    darkens toward the edge, and a filmstrip of the photos around it under it,
    the current one taller. Without ``gap`` the photo runs into the strip."""
    from PIL import ImageDraw

    width, height = 1080, 2099
    canvas = np.zeros((height, width, 3), np.uint8)
    fade = np.linspace(0.0, 1.0, height - 1920)[:, None]
    canvas[1920:] = (np.array([14, 17, 23]) * (1 - fade) + np.array([2, 3, 5]) * fade)[:, None]
    end = 1920 if gap else 1940
    canvas[:end] = _picture(width, end, seed)
    image = Image.fromarray(canvas)
    for index, x in enumerate(range(-40, 700, 90)):
        current = index == 6
        y0, y1, wide = (1928, 2094, 92) if current else (1942, 2074, 65)
        mask = Image.new("L", (wide, y1 - y0), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, wide - 1, y1 - y0 - 1], radius=8, fill=255)
        image.paste(Image.fromarray(_picture(wide, y1 - y0, seed + index + 1)), (x, y0), mask)
    return image


def test_a_gallerys_filmstrip_is_cropped_to_the_photo(tmp_path: Path) -> None:
    """The thumbnails of the photos around the one shown, in a row under it,
    are the gallery's: the cut falls at the photo's bottom edge."""
    path = tmp_path / "2VkmCbgLL3o9lxGp.jpeg"
    _gallery().save(path, quality=85)
    with Image.open(path) as opened:
        assert borders.screenshot_evidence(path, opened) == "shape"

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("bottom",)
    assert abs(box.bottom - 1920) <= 4


def test_a_row_of_things_running_into_the_photo_is_no_filmstrip(tmp_path: Path) -> None:
    """Tiles with none of the band between them and the photo above — a
    fence, a shelf of books at the bottom of a picture — are its own: no
    chrome cut is claimed for them. (The aggressive pass may still shave
    the dark band under the tiles as a plain bar.)"""
    path = tmp_path / "2VkmCbgLL3o9lxGp.jpeg"
    _gallery(gap=False).save(path, quality=85)

    box = detect_black_borders(path)
    assert box is None or (box.chrome == () and box.top == 0)


def _media_viewer(*, seed: int = 7) -> Image.Image:
    """A media viewer on black: a counter ("13 / 20") high in the top band,
    the photo, and page dots low in the bottom band, deeper than a bar."""
    from PIL import ImageDraw

    width, height = 1080, 2107
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    for x in (958, 972, 996, 1008, 1020):
        draw.rectangle([x, 150, x + 9, 172], fill=(255, 255, 255))
    image.paste(Image.fromarray(_picture(width, 1441, seed)), (0, 328))  # rows 328-1769
    for index, x in enumerate(range(466, 590, 20)):
        radius = 3 if index in (0, 6) else 6
        fill = (255, 255, 255) if index == 3 else (110, 110, 110)
        draw.ellipse([x - radius, 1912 - radius, x + radius, 1912 + radius], fill=fill)
    return image


def test_a_media_viewers_counter_and_dots_are_cropped(tmp_path: Path) -> None:
    """A counter or page dots alone on a black band, which ends at the
    photo's straight edge, are the viewer's: both bands go."""
    path = tmp_path / "BGrjIeOp1a0yT59p.jpeg"
    _media_viewer().save(path, quality=85)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("top", "bottom")
    assert abs(box.top - 328) <= 4
    assert abs(box.bottom - 1769) <= 4


def test_lights_in_a_sky_that_brightens_into_the_scene_are_cropped(tmp_path: Path) -> None:
    """Small lights high in a night sky that brightens into the scene below
    with no hard edge: the strict pass keeps it all, the aggressive pass
    needs no hard edge and crops the dark headroom down into the gradient."""
    _require_guard()
    from PIL import ImageDraw

    width, height = 1080, 2107
    canvas = np.zeros((height, width, 3), np.uint8)
    ramp = np.clip(np.arange(height - 328) / 240.0, 0.0, 1.0)[:, None, None]
    canvas[328:] = (_picture(width, height - 328, seed=13) * ramp).astype(np.uint8)
    image = Image.fromarray(canvas)
    draw = ImageDraw.Draw(image)
    for x in (958, 972, 996, 1008, 1020):
        draw.rectangle([x, 150, x + 9, 172], fill=(255, 255, 255))
    path = tmp_path / "BGrjIeOp1a0yT59p.jpeg"
    image.save(path, quality=85)

    box = detect_black_borders(path)
    assert box is not None and box.top >= 328


@pytest.mark.parametrize("right", [925, 692])  # a send button; or more buttons, a narrower field
def test_a_reply_field_an_earlier_crop_cut_through_is_cropped(tmp_path: Path, right: int) -> None:
    """A story's reply field that an earlier crop cut at its bottom border:
    the border fills the edge rows, or much of them, so the band is the
    first plain row past it, and the field's words make it interface though
    most of its rows hold no more than its two sides."""
    from PIL import ImageDraw

    width, height = 1080, 2063
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    mask = Image.new("L", (width, 1920), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, -40, width - 1, 1919], radius=30, fill=255)
    image.paste(Image.fromarray(_picture(width, 1920, seed=9)), (0, 0), mask)
    # The field's flat bottom border is the last rows, grey across most of
    # the width; its words; a send button beside it.
    draw.rounded_rectangle([22, 1932, right, height - 1], radius=63, outline=(101, 101, 101), width=3)
    _text_rows(draw, 69, 400, 1985, 2015, line=30, gap=0, fill=(255, 255, 255))
    for x in range(right + 50, width - 50, 120):
        draw.polygon([(x, 1980), (x + 57, 1980), (x + 25, 2030)], outline=(255, 255, 255), width=4)
    path = tmp_path / "ThueoHCziZcVzeht.jpg"
    image.save(path, quality=85)

    box = detect_black_borders(path)
    assert box is not None and box.chrome == ("bottom",)
    assert abs(box.bottom - 1920) <= 4


def test_ringing_beside_rounded_corners_on_a_light_band_is_no_word(tmp_path: Path) -> None:
    """A story card's rounded corners on a light band, with a JPEG's faint
    ringing beside them: the specks sit close like letters, but only a black
    surround holds a reply field. The caption under them is kept."""

    width, height = 1170, 2074
    band = (207, 202, 197)
    image, draw = _shot(width, height, band)
    radius = 30
    draw.rectangle([0, 0, radius, radius], fill=(20, 20, 20))
    draw.rectangle([width - radius, 0, width, radius], fill=(20, 20, 20))
    draw.ellipse([0, 0, 2 * radius, 2 * radius], fill=band)
    draw.ellipse([width - 2 * radius, 0, width, 2 * radius], fill=band)
    for y in (2, 6, 10):
        for x in range(width - 54, width - 30, 6):
            draw.rectangle([x, y, x + 1, y], fill=(190, 185, 180))
    _text_rows(draw, 60, 820, 264, 304, line=40, gap=0)
    image.paste(Image.fromarray(_picture(width, height - 409, seed=11)), (0, 409))
    path = tmp_path / "34sBUynJv0XHUFW5.png"
    image.save(path)

    assert detect_black_borders(path) is None


def test_tiles_that_do_not_stand_in_columns_are_no_filmstrip(tmp_path: Path) -> None:
    """A brick wall's courses, each set half a brick along from the last,
    hold as many tile-sized runs as a filmstrip, but its gaps do not run
    down the strip: the wall is the picture's."""
    width, height = 1080, 2099
    canvas = np.zeros((height, width, 3), np.uint8)
    canvas[:] = (14, 17, 23)
    canvas[:1920] = _picture(width, 1920, seed=21)
    for course, y in enumerate(range(1950, 2080, 21)):  # to 2095, thin mortar between
        for x in range(45 * (course % 2), width - 65, 90):
            canvas[y : y + 19, x : x + 65] = _picture(65, 19, seed=course * 20 + x)
    path = tmp_path / "2VkmCbgLL3o9lxGp.jpeg"
    _save(canvas, path, quality=85)

    assert detect_black_borders(path) is None


# --- videos -----------------------------------------------------------------
#
# A pillarboxed clip: a vertical picture baked into a landscape frame (and
# the letterboxed mirror image). Frames are real testsrc video so detection
# and re-encoding exercise ffmpeg exactly as in a scan.

_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
requires_ffmpeg = pytest.mark.skipif(not _FFMPEG, reason="ffmpeg/ffprobe not available")


def _require_guard() -> None:
    """Skip when the human guard cannot run: without it only strict bars
    are suggested, so aggressive expectations do not apply (CI's PR job
    installs no OpenCV)."""
    if not borders._human_guard_available():
        pytest.skip("human guard unavailable (OpenCV not installed)")


def _make_video(path: Path, graph: str, *, duration: float = 3.0, audio: bool = False) -> Path:
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", graph,
    ]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                    "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-shortest"]
    command += ["-pix_fmt", "yuv420p", str(path)]
    built = subprocess.run(command, capture_output=True, check=False)
    if built.returncode != 0 or not path.exists():
        pytest.skip("ffmpeg cannot synthesize a test video here")
    return path


def _loop_video(path: Path, array: np.ndarray, *, duration: float = 3.0) -> Path:
    """A still noisy picture looped as video: full-frame content with no
    dark edges of its own (testsrc's pattern has real dark margins, which
    the aggressive pass quite reasonably reads as bars)."""
    still = _save(array, path.with_suffix(".png"))
    built = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(still),
         "-t", str(duration), "-r", "15", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, check=False,
    )
    if built.returncode != 0 or not path.exists():
        pytest.skip("ffmpeg cannot synthesize a test video here")
    return path


def _pillarboxed(path: Path, **kwargs) -> Path:
    """640x360 landscape frame holding a 202x360 vertical picture centred:
    219px black bars left and right (odd on purpose: the crop must land on
    even offsets an encoder accepts)."""
    return _make_video(
        path,
        "color=c=black:size=640x360:rate=15:duration=3[bg];"
        "testsrc=size=202x360:rate=15:duration=3[fg];"
        "[bg][fg]overlay=219:0",
        **kwargs,
    )


def _video_record(path: Path) -> FileRecord:
    from dedupe.similar_video import probe_video

    duration, width, height = probe_video(path)
    stat = path.stat()
    return FileRecord(
        path=str(path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        media_type=MediaType.VIDEO,
        extension=path.suffix.lower(),
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
        width=width,
        height=height,
        duration=duration,
    )


@requires_ffmpeg
def test_pillarboxed_video_is_found(tmp_path: Path) -> None:
    path = _pillarboxed(tmp_path / "vertical.mp4")
    box = borders.detect_video_borders(path)
    assert box is not None
    assert (box.width, box.height) == (640, 360)
    # H.264 ringing shifts the measured edge a few pixels, and the box is
    # pulled in to even offsets/sizes the encoder accepts.
    assert 214 <= box.left <= 224 and box.left % 2 == 0
    assert 416 <= box.right <= 426 and box.right % 2 == 0
    assert box.top == 0 and box.bottom == 360
    assert box.crop_width % 2 == 0 and box.crop_height % 2 == 0


@requires_ffmpeg
def test_letterboxed_video_is_found(tmp_path: Path) -> None:
    canvas = np.zeros((360, 640, 3), dtype=np.uint8)
    canvas[60:300] = _picture(640, 240)
    path = _loop_video(tmp_path / "letterbox.mp4", canvas)
    box = borders.detect_video_borders(path)
    assert box is not None
    assert 56 <= box.top <= 64 and 296 <= box.bottom <= 304
    assert box.left == 0 and box.right == 640


@requires_ffmpeg
def test_full_frame_video_has_no_suggestion(tmp_path: Path) -> None:
    path = _loop_video(tmp_path / "clean.mp4", _picture(640, 360))
    assert borders.detect_video_borders(path) is None


@requires_ffmpeg
def test_video_that_fills_the_frame_later_is_not_cropped(tmp_path: Path) -> None:
    """Bars in a minority of a clip (a pillarboxed insert in full-frame
    content) do not win a crop: the majority rules. The full-frame segment
    is flat gray rather than testsrc, whose pattern has genuinely dark
    edges the aggressive pass would read as thin bars."""
    path = _make_video(
        tmp_path / "mixed.mp4",
        "color=c=black:size=640x360:rate=15:duration=1.2[bg];"
        "testsrc=size=202x360:rate=15:duration=1.2[fg];"
        "[bg][fg]overlay=219:0[boxed];"
        "color=c=0x7f7f7f:size=640x360:rate=15:duration=1.8[full];"
        "[boxed][full]concat=n=2:v=1:a=0",
    )
    assert borders.detect_video_borders(path) is None


@requires_ffmpeg
def test_all_black_video_has_no_suggestion(tmp_path: Path) -> None:
    path = _make_video(tmp_path / "black.mp4", "color=c=black:size=640x360:rate=15:duration=3")
    assert borders.detect_video_borders(path) is None


@requires_ffmpeg
def test_video_crop_replaces_file_and_trashes_original(tmp_path: Path) -> None:
    from dedupe.similar_video import probe_video

    path = _pillarboxed(tmp_path / "vertical.mp4", audio=True)
    record = _video_record(path)
    box = borders.detect_video_borders(path)
    assert box is not None
    trash = tmp_path / "trash"
    outcome = crop_file_in_place(record, box, send_to_trash=_fake_trash(trash))
    assert (outcome.width, outcome.height) == (box.crop_width, box.crop_height)
    duration, width, height = probe_video(path)
    assert (width, height) == (box.crop_width, box.crop_height)
    assert duration is not None and abs(duration - 3.0) < 0.6
    # The soundtrack survives the re-encode (stream-copied).
    streams = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=False,
    ).stdout
    assert "audio" in streams
    # The untouched original is in the Trash at its full size, so Undo works.
    original = trash / "vertical.mp4"
    assert original.is_file()
    _, orig_width, orig_height = probe_video(original)
    assert (orig_width, orig_height) == (640, 360)
    # The cropped file has no bars left to suggest.
    assert borders.detect_video_borders(path) is None


@requires_ffmpeg
def test_scan_publishes_video_in_crop_review(tmp_path: Path) -> None:
    _pillarboxed(tmp_path / "vertical.mp4")
    _save(_framed(640, 480, top=60, bottom=60), tmp_path / "letterbox.png")
    result = run_scan([str(tmp_path)], find_black_borders=True)
    crop_groups = [group for group in result.groups if group.kind == GroupKind.CROP]
    assert len(crop_groups) == 1
    by_name = {Path(member.path).name: member for member in crop_groups[0].members}
    assert set(by_name) == {"vertical.mp4", "letterbox.png"}
    video_box = suggested_crop(by_name["vertical.mp4"])
    assert video_box is not None and video_box.left > 200


@requires_ffmpeg
def test_rotated_video_is_cropped_in_display_orientation(tmp_path: Path) -> None:
    """Phone clips are often stored landscape with a rotation tag. The bars
    are measured and cut as displayed, and the result is saved upright —
    the video counterpart of an image's EXIF transpose."""
    from dedupe.similar_video import probe_video

    coded = _pillarboxed(tmp_path / "coded.mp4")  # 640x360, bars on the sides
    rotated = tmp_path / "rotated.mp4"
    built = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-display_rotation:v", "-90",
         "-i", str(coded), "-c", "copy", str(rotated)],
        capture_output=True, check=False,
    )
    probe = borders._probe_video_stream(rotated) if built.returncode == 0 else None
    if not probe or (probe["width"], probe["height"]) != (360, 640):
        pytest.skip("this ffmpeg cannot write a display-rotation fixture")

    box = borders.detect_video_borders(rotated)
    assert box is not None
    # Displayed portrait: the coded side bars are now top and bottom.
    assert (box.width, box.height) == (360, 640)
    assert 214 <= box.top <= 224 and 416 <= box.bottom <= 426
    assert box.left == 0 and box.right == 360

    record = _video_record(rotated)
    outcome = crop_file_in_place(record, box, send_to_trash=_fake_trash(tmp_path / "trash"))
    assert (outcome.width, outcome.height) == (360, box.crop_height)
    _, width, height = probe_video(rotated)  # coded == display: saved upright
    assert (width, height) == (360, box.crop_height)
    assert borders.detect_video_borders(rotated) is None


@requires_ffmpeg
def test_hevc_video_crop_keeps_hevc(tmp_path: Path) -> None:
    built = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         ("color=c=black:size=320x180:rate=15:duration=2[bg];"
          "testsrc=size=160x180:rate=15:duration=2[fg];[bg][fg]overlay=80:0"),
         "-c:v", "libx265", "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
         str(tmp_path / "hevc.mp4")],
        capture_output=True, check=False,
    )
    if built.returncode != 0:
        pytest.skip("libx265 unavailable for the HEVC fixture")
    path = tmp_path / "hevc.mp4"
    box = borders.detect_video_borders(path)
    assert box is not None and box.left >= 76
    outcome = crop_file_in_place(
        _video_record(path), box, send_to_trash=_fake_trash(tmp_path / "trash")
    )
    probe = borders._probe_video_stream(path)
    assert probe is not None and probe["codec_name"] == "hevc"
    assert (outcome.width, outcome.height) == (box.crop_width, box.crop_height)


@requires_ffmpeg
def test_web_video_crop_apply_and_undo(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "media"
    media.mkdir()
    target = str(_pillarboxed(media / "vertical.mp4"))
    original = Path(target).read_bytes()
    _FakeBatch.trash_dir = tmp_path / "Trash"
    monkeypatch.setattr(web_app, "TrashBatch", _FakeBatch)
    cache_path = tmp_path / "cache.sqlite3"
    result = run_scan([str(media)], find_black_borders=True, cache_path=cache_path)
    app = create_app(result, review_session_path=tmp_path / "session.json",
                     cache_path=cache_path)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]
    group = next(g for g in result.groups if g.kind == GroupKind.CROP)
    payload = {"group_id": group.id, "path": target, "scan_id": scan_id}

    fetched = client.get(f"/api/groups/{group.id}").get_json()
    member = next(m for m in fetched["members"] if m["path"] == target)
    assert member["media_type"] == "video" and member["crop_state"] == "pending"
    assert member["crop"]["left"] > 200

    applied = client.post("/api/crop/apply", json=payload, headers=headers)
    assert applied.status_code == 200, applied.get_json()
    member = next(m for m in applied.get_json()["members"] if m["path"] == target)
    assert member["crop_state"] == "cropped"
    assert member["width"] == member["crop"]["crop_width"]

    undone = client.post("/api/crop/undo", json=payload, headers=headers)
    assert undone.status_code == 200, undone.get_json()
    assert Path(target).read_bytes() == original  # the untouched re-encode source


@requires_ffmpeg
def test_video_boxed_in_most_segments_is_cropped(tmp_path: Path) -> None:
    """Majority rule: boxed segments outvote a full-frame tail. (The
    minority case above stays uncropped.)"""
    path = _make_video(
        tmp_path / "mostly_boxed.mp4",
        "color=c=black:size=640x360:rate=15:duration=2.4[bg];"
        "testsrc=size=202x360:rate=15:duration=2.4[fg];"
        "[bg][fg]overlay=219:0[boxed];"
        "color=c=0x7f7f7f:size=640x360:rate=15:duration=1.6[full];"
        "[boxed][full]concat=n=2:v=1:a=0",
    )
    box = borders.detect_video_borders(path)
    assert box is not None
    assert box.left >= 214 and box.right <= 426


# --- aggressive bars ----------------------------------------------------------
#
# The strict detector stays as the first word; the aggressive pass adds the
# bars it is too timid for — caption ink in the bar, encoder noise, dark-gray
# bars — and the human guard, not timidity, keeps people whole.


def test_aggressive_finds_bar_holding_caption_text() -> None:
    luma = np.zeros((1280, 720), dtype=np.uint8)
    luma[420:] = _picture(720, 860)[:, :, 0]
    for x in range(300, 420, 24):  # a line of caption glyphs inside the bar
        luma[384:404, x : x + 12] = 230
    strict = detect_borders_in_array(luma)
    assert strict is None or strict[0] == 0  # the caption defeats the hard edge
    bars = borders.detect_borders_aggressive(luma)
    assert bars is not None
    assert bars[0] >= 420  # bar and caption are cropped together


def test_aggressive_finds_dark_gray_bar() -> None:
    luma = np.full((600, 800), 40, dtype=np.uint8)  # bar level above strict's 28
    luma[100:-100] = _picture(800, 400)[:, :, 0]
    assert detect_borders_in_array(luma) is None
    bars = borders.detect_borders_aggressive(luma)
    assert bars is not None
    assert bars[0] >= 96 and bars[1] >= 96


def test_aggressive_finds_noisy_bar() -> None:
    rng = np.random.default_rng(3)
    luma = rng.integers(0, 36, size=(600, 800), dtype=np.uint8)  # encoder noise
    luma[120:-120] = _picture(800, 360)[:, :, 0]
    bars = borders.detect_borders_aggressive(luma)
    assert bars is not None
    assert bars[0] >= 110 and bars[1] >= 110


def test_aggressive_ignores_clean_picture() -> None:
    assert borders.detect_borders_aggressive(_picture(800, 600)[:, :, 0]) is None


def test_human_guard_pulls_edges_back_from_people() -> None:
    # A face (fractions x0,y0,x1,y1) reaching into the top and left strips.
    boxes = [(0.02, 0.05, 0.30, 0.25)]
    guarded = borders._guarded_bars((400, 100, 200, 50), boxes, 720, 1280)
    assert guarded[0] <= int(0.05 * 1280)  # top keeps the whole head
    assert guarded[2] <= int(0.02 * 720)
    assert guarded[1] == 100 and guarded[3] == 50  # untouched edges stay
    assert borders._guarded_bars((400, 100, 200, 50), [], 720, 1280) == (400, 100, 200, 50)


def test_captioned_letterbox_image_is_suggested(tmp_path: Path) -> None:
    _require_guard()
    canvas = np.zeros((1280, 720, 3), dtype=np.uint8)
    canvas[420:860] = _picture(720, 440)
    for x in range(280, 440, 24):
        canvas[380:400, x : x + 12] = 235
    path = _save(canvas, tmp_path / "meme.png")
    box = detect_black_borders(path)
    assert box is not None
    assert box.top >= 420 and box.bottom <= 860 + 4


def test_only_mp4_family_videos_are_croppable(tmp_path: Path) -> None:
    def record(name: str, media_type: MediaType) -> FileRecord:
        return FileRecord(
            path=str(tmp_path / name), size=1, mtime=0.0,
            media_type=media_type, extension=Path(name).suffix.lower(),
        )

    assert borders.is_croppable(record("clip.mp4", MediaType.VIDEO))
    assert borders.is_croppable(record("clip.mov", MediaType.VIDEO))
    assert borders.is_croppable(record("clip.m4v", MediaType.VIDEO))
    assert not borders.is_croppable(record("clip.mkv", MediaType.VIDEO))
    assert not borders.is_croppable(record("clip.webm", MediaType.VIDEO))
    assert borders.is_croppable(record("photo.jpg", MediaType.IMAGE))
    assert not borders.is_croppable(record("clip.mp4", MediaType.IMAGE))
