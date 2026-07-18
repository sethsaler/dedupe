"""Action dry-run / quarantine tests."""

from __future__ import annotations

from pathlib import Path

from dedupe.actions import apply_actions, undo_quarantine
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


def test_execute_refuses_when_selected_file_changed_after_scan(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    selected = Path(groups[0].selected_for_removal[0])
    selected.write_bytes(b"new unrelated content")

    result = apply_actions(
        groups,
        action="quarantine",
        quarantine_dir=tmp_path / "q",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )

    assert result.success_count == 0
    assert result.fail_count == 1
    assert selected.exists()
    assert "changed since scan" in (result.items[0].error or "")
    assert result.log_path and Path(result.log_path).exists()


def test_quarantine_receipt_can_restore_file(tmp_path: Path) -> None:
    a = _rec(tmp_path / "a.jpg", b"same-bytes")
    b = _rec(tmp_path / "b.jpg", b"same-bytes")
    groups = build_groups([[a, b]], [])
    action = apply_actions(
        groups,
        action="quarantine",
        quarantine_dir=tmp_path / "q",
        dry_run=False,
        roots=[str(tmp_path)],
        log_dir=tmp_path / "logs",
    )
    moved_source = Path(action.items[0].path)
    moved_destination = Path(action.items[0].destination or "")
    assert not moved_source.exists() and moved_destination.exists()

    restored = undo_quarantine(action.log_path or "", dry_run=False)

    assert restored.success_count == 1
    assert moved_source.exists() and not moved_destination.exists()
