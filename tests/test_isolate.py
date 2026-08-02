"""Isolate-for-review action tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import dedupe.actions as actions_module
from dedupe.actions import isolate_groups
from dedupe.grouping import build_groups
from dedupe.models import FileRecord, MediaType


def _rec(path: Path, data: bytes) -> FileRecord:
    path.write_bytes(data)
    st = path.stat()
    return FileRecord(
        path=str(path.resolve()),
        size=st.st_size,
        mtime=st.st_mtime,
        media_type=MediaType.IMAGE,
        extension=path.suffix.lower(),
    )


def test_isolate_copy_creates_group_folders(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    a = _rec(src / "a.jpg", b"same-bytes-aaa")
    b = _rec(src / "b.jpg", b"same-bytes-aaa")
    groups = build_groups([[a, b]], [])

    review = tmp_path / "review"
    result = isolate_groups(groups, review, mode="copy", dry_run=False)

    assert result.success_count == 2
    assert result.fail_count == 0
    assert Path(result.review_root).exists()
    assert len(result.group_dirs) == 1

    group_dir = Path(result.group_dirs[0])
    assert group_dir.is_dir()
    files = [p.name for p in group_dir.iterdir() if p.is_file()]
    assert "_group.json" in files
    assert "README.txt" in files
    keep_files = [n for n in files if n.startswith("KEEP__")]
    assert len(keep_files) == 1
    # originals untouched
    assert Path(a.path).exists() and Path(b.path).exists()
    assert Path(result.review_root).parent == review
    assert (Path(result.review_root) / "_review_index.json").exists()


def test_isolate_dry_run_no_writes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    a = _rec(src / "a.jpg", b"x" * 20)
    b = _rec(src / "b.jpg", b"x" * 20)
    groups = build_groups([[a, b]], [])
    review = tmp_path / "review"
    result = isolate_groups(groups, review, mode="copy", dry_run=True)
    assert result.success_count == 2
    assert not review.exists()


def test_isolate_kinds_filter(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    a = _rec(src / "a.jpg", b"z" * 10)
    b = _rec(src / "b.jpg", b"z" * 10)
    groups = build_groups([[a, b]], [])
    review = tmp_path / "review"
    result = isolate_groups(groups, review, mode="copy", kinds={"similar"}, dry_run=False)
    # only exact group exists → filtered out
    assert result.success_count == 0
    assert result.group_dirs == []


def test_default_review_dir_is_inside_source(tmp_path: Path) -> None:
    from dedupe.actions import default_review_dir

    src = tmp_path / "MyPhotos"
    src.mkdir()
    review = default_review_dir([str(src)])
    assert review == src / "_Dedupe Review"
    assert src in review.parents or review.parent == src


def test_isolate_defaults_into_scan_root(tmp_path: Path) -> None:
    src = tmp_path / "album"
    src.mkdir()
    a = _rec(src / "a.jpg", b"same-payload-xyz")
    b = _rec(src / "b.jpg", b"same-payload-xyz")
    groups = build_groups([[a, b]], [])

    # No review_dir → under source
    result = isolate_groups(groups, review_dir=None, mode="copy", dry_run=False, roots=[str(src)])
    assert result.success_count == 2
    review_base = (src / "_Dedupe Review").resolve()
    assert Path(result.review_root).parent == review_base
    assert Path(result.review_root).is_dir()
    # originals stay in source root
    assert (src / "a.jpg").exists() and (src / "b.jpg").exists()


def _pair(tmp_path: Path, payload: bytes = b"shared-payload") -> tuple[Path, list]:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    a = _rec(src / "a.jpg", payload)
    b = _rec(src / "b.jpg", payload)
    return src, build_groups([[a, b]], [])


def test_isolate_hardlink_shares_inodes(tmp_path: Path) -> None:
    _src, groups = _pair(tmp_path)
    result = isolate_groups(groups, tmp_path / "review", mode="hardlink", dry_run=False)

    assert result.success_count == 2 and result.fail_count == 0
    for item in result.items:
        source_stat = Path(item.path).stat()
        dest_stat = Path(item.destination).stat()
        assert dest_stat.st_ino == source_stat.st_ino
        assert dest_stat.st_nlink >= 2


def test_isolate_symlink_points_at_source(tmp_path: Path) -> None:
    _src, groups = _pair(tmp_path)
    result = isolate_groups(groups, tmp_path / "review", mode="symlink", dry_run=False)

    assert result.success_count == 2
    for item in result.items:
        dest = Path(item.destination)
        assert dest.is_symlink()
        assert dest.resolve() == Path(item.path).resolve()
        assert Path(item.path).exists()


def test_isolate_move_relocates_originals(tmp_path: Path) -> None:
    src, groups = _pair(tmp_path)
    result = isolate_groups(groups, tmp_path / "review", mode="move", dry_run=False)

    assert result.success_count == 2 and result.fail_count == 0
    for item in result.items:
        assert not Path(item.path).exists()
        assert Path(item.destination).is_file()
    assert not any(src.iterdir())


def test_isolate_hardlink_falls_back_to_copy_across_volumes(
    tmp_path: Path, monkeypatch
) -> None:
    _src, groups = _pair(tmp_path)

    def _cross_volume_link(source, target):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(actions_module.os, "link", _cross_volume_link)
    result = isolate_groups(groups, tmp_path / "review", mode="hardlink", dry_run=False)

    assert result.success_count == 2 and result.fail_count == 0
    for item in result.items:
        dest = Path(item.destination)
        assert dest.is_file() and not dest.is_symlink()
        assert dest.stat().st_ino != Path(item.path).stat().st_ino
        assert dest.read_bytes() == Path(item.path).read_bytes()


def test_isolate_refuses_symlinked_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    real = _rec(src / "real.jpg", b"payload-bytes")
    link_path = src / "link.jpg"
    link_path.symlink_to(Path(real.path))
    st = link_path.stat()
    link_record = FileRecord(
        path=str(link_path),
        size=st.st_size,
        mtime=st.st_mtime,
        media_type=MediaType.IMAGE,
        extension=".jpg",
    )
    groups = build_groups([[real, link_record]], [])

    result = isolate_groups(groups, tmp_path / "review", mode="copy", dry_run=False)

    assert result.success_count == 0
    assert any("symbolic link" in (item.error or "") for item in result.items)
    assert not (tmp_path / "review").exists()


def test_link_or_copy_rejects_symlink_directly(tmp_path: Path) -> None:
    target = tmp_path / "target.jpg"
    target.write_bytes(b"payload")
    link = tmp_path / "link.jpg"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        actions_module._link_or_copy(link, tmp_path / "out.jpg", "copy")
    assert not (tmp_path / "out.jpg").exists()


def test_isolate_move_refuses_cross_device(tmp_path: Path, monkeypatch) -> None:
    src, groups = _pair(tmp_path)
    review = tmp_path / "review"

    def _fake_device(path: Path):
        return 2 if "review" in str(path) else 1

    monkeypatch.setattr(actions_module, "_device_of", _fake_device)
    result = isolate_groups(groups, review, mode="move", dry_run=False)

    assert result.success_count == 0
    assert all("different volume" in (item.error or "") for item in result.items)
    assert (src / "a.jpg").exists() and (src / "b.jpg").exists()
    assert not review.exists()


def test_isolate_move_cross_device_allowed_copies_and_verifies(
    tmp_path: Path, monkeypatch
) -> None:
    _src, groups = _pair(tmp_path)
    review = tmp_path / "review"
    real_replace = os.replace

    def _fake_replace(source, target):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(actions_module, "_device_of", lambda p: 2 if "review" in str(p) else 1)
    monkeypatch.setattr(actions_module.os, "replace", _fake_replace)
    result = isolate_groups(
        groups, review, mode="move", dry_run=False, allow_cross_device=True
    )
    monkeypatch.setattr(actions_module.os, "replace", real_replace)

    assert result.success_count == 2 and result.fail_count == 0
    for item in result.items:
        assert not Path(item.path).exists()
        assert Path(item.destination).read_bytes() == b"shared-payload"


def test_isolate_records_sizes_and_orders_items(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    records = [_rec(src / f"f{i:03d}.jpg", b"x" * (10 + i)) for i in range(12)]
    groups = build_groups([[records[i], records[i + 1]] for i in range(0, 12, 2)], [])

    result = isolate_groups(groups, tmp_path / "review", mode="copy", dry_run=False)

    expected = [member.path for group in groups for member in group.members]
    assert [item.path for item in result.items] == expected
    assert all(item.size for item in result.items)
