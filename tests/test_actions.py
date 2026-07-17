"""Action dry-run / quarantine tests."""

from __future__ import annotations

from pathlib import Path

from dedupe.actions import apply_actions
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


def test_quarantine_dry_run(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same")
    b = _rec(tmp_path / "b.jpg", b"same")
    groups = build_groups([[a, b]], [])
    q = tmp_path / "quarantine"
    result = apply_actions(groups, action="quarantine", quarantine_dir=q, dry_run=True)
    assert result.success_count == 1
    assert not q.exists() or not any(q.iterdir()) if q.exists() else True
    # originals untouched
    assert Path(a.path).exists() and Path(b.path).exists()


def test_quarantine_execute(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    q = tmp_path / "quarantine"
    result = apply_actions(groups, action="quarantine", quarantine_dir=q, dry_run=False)
    assert result.success_count == 1
    assert q.exists()
    remaining = [p for p in (Path(a.path), Path(b.path)) if p.exists()]
    assert len(remaining) == 1
    assert len(list(q.iterdir())) == 1
