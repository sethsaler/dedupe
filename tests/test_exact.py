"""Exact duplicate detection tests."""

from __future__ import annotations

from pathlib import Path

from dedupe.exact import find_exact_groups, file_sha256
from dedupe.models import FileRecord, MediaType, classify_media
from dedupe.scanner import inventory


def _write(p: Path, data: bytes) -> FileRecord:
    p.write_bytes(data)
    st = p.stat()
    return FileRecord(
        path=str(p.resolve()),
        size=st.st_size,
        mtime=st.st_mtime,
        media_type=MediaType.IMAGE,
        extension=p.suffix.lower(),
    )


def test_exact_finds_identical_bytes(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.jpg", b"hello-image-content-12345")
    b = _write(tmp_path / "b.jpg", b"hello-image-content-12345")
    c = _write(tmp_path / "c.jpg", b"different-content-zzzz")

    groups = find_exact_groups([a, b, c])
    assert len(groups) == 1
    paths = {m.path for m in groups[0]}
    assert a.path in paths and b.path in paths
    assert c.path not in paths
    assert groups[0][0].sha256 == groups[0][1].sha256


def test_exact_different_size_never_grouped(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.jpg", b"short")
    b = _write(tmp_path / "b.jpg", b"a much longer payload that differs in size")
    groups = find_exact_groups([a, b])
    assert groups == []


def test_inventory_classifies(tmp_path: Path) -> None:
    (tmp_path / "x.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "y.gif").write_bytes(b"GIF89a")
    (tmp_path / "z.mp4").write_bytes(b"\x00\x00ftyp")
    (tmp_path / "notes.txt").write_text("skip me")

    recs = inventory([tmp_path])
    exts = {r.extension for r in recs}
    assert ".png" in exts
    assert ".gif" in exts
    assert ".mp4" in exts
    assert ".txt" not in exts
    assert classify_media(Path("foo.GIF")) == MediaType.GIF


def test_file_sha256_stable(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc" * 1000)
    assert file_sha256(p) == file_sha256(p)
