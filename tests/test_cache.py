"""Hash cache identity and algorithm-version tests."""

from pathlib import Path

from dedupe.cache import HashCache
from dedupe.models import FileRecord, MediaType


def _record(path: Path, *, inode: int) -> FileRecord:
    return FileRecord(
        path=str(path),
        size=10,
        mtime=1.0,
        media_type=MediaType.IMAGE,
        extension=".jpg",
        device=2,
        inode=inode,
        mtime_ns=1_000_000_000,
        phash="0" * 16,
    )


def test_cache_rejects_replaced_inode(tmp_path: Path) -> None:
    cache = HashCache(tmp_path / "hashes.sqlite3")
    original = _record(tmp_path / "photo.jpg", inode=10)
    cache.store_all([original])

    same = _record(tmp_path / "photo.jpg", inode=10)
    same.phash = None
    replaced = _record(tmp_path / "photo.jpg", inode=11)
    replaced.phash = None

    assert cache.hydrate([same]) == 1
    assert same.phash == "0" * 16
    assert cache.hydrate([replaced]) == 0
    cache.close()
