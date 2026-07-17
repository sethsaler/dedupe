"""Isolate-for-review action tests."""

from __future__ import annotations

from pathlib import Path

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
    assert (review / "_review_index.json").exists()


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
    assert result.review_root == str((src / "_Dedupe Review").resolve())
    assert (src / "_Dedupe Review").is_dir()
    # originals stay in source root
    assert (src / "a.jpg").exists() and (src / "b.jpg").exists()
