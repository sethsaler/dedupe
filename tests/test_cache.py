"""Hash cache identity and algorithm-version tests."""

import json
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
    original.tile_phashes = "0,1,2,3,4"
    cache.store_all([original])

    same = _record(tmp_path / "photo.jpg", inode=10)
    same.phash = None
    replaced = _record(tmp_path / "photo.jpg", inode=11)
    replaced.phash = None

    assert cache.hydrate([same]) == 1
    assert same.phash == "0" * 16
    assert same.tile_phashes == "0,1,2,3,4"
    assert cache.hydrate([replaced]) == 0
    cache.close()


def test_cache_reuses_hashes_after_same_filesystem_move(tmp_path: Path) -> None:
    cache = HashCache(tmp_path / "hashes.sqlite3")
    original = _record(tmp_path / "before" / "photo.jpg", inode=10)
    original.tile_phashes = "0,1,2,3,4"
    cache.store_all([original])

    moved = _record(tmp_path / "after" / "renamed.jpg", inode=10)
    moved.phash = None

    assert cache.hydrate([moved]) == 1
    assert moved.phash == "0" * 16
    assert moved.tile_phashes == "0,1,2,3,4"
    cache.close()


def test_cache_round_trips_person_decision_without_hashes(tmp_path: Path) -> None:
    cache = HashCache(tmp_path / "hashes.sqlite3")
    original = _record(tmp_path / "photo.jpg", inode=10)
    original.phash = None
    original.human_detection_status = "person_detected"
    original.human_detector = "opencv_face_hog"
    original.human_detection_signature = "human-presence-v1|opencv|confidence=0.25"
    original.human_frames_analyzed = 1
    original.human_max_confidence = 1.0
    cache.store_all([original])

    same = _record(tmp_path / "photo.jpg", inode=10)
    same.phash = None

    assert cache.hydrate([same]) == 1
    assert same.human_detection_status == "person_detected"
    assert same.human_detector == "opencv_face_hog"
    assert same.human_detection_signature == original.human_detection_signature
    assert same.human_frames_analyzed == 1
    assert same.human_max_confidence == 1.0
    cache.close()


def test_cache_round_trips_face_count_without_hashes(tmp_path: Path) -> None:
    cache = HashCache(tmp_path / "hashes.sqlite3")
    original = _record(tmp_path / "photo.jpg", inode=10)
    original.phash = None
    original.face_count = 3
    original.face_detector = "opencv_yunet"
    original.face_detection_signature = "face-count-v1|opencv_yunet|yunet=abc"
    cache.store_all([original])

    same = _record(tmp_path / "photo.jpg", inode=10)
    same.phash = None

    assert cache.hydrate([same]) == 1
    assert same.face_count == 3
    assert same.face_detector == "opencv_yunet"
    assert same.face_detection_signature == original.face_detection_signature
    cache.close()


def test_cache_round_trips_distinct_pair_until_a_file_changes(tmp_path: Path) -> None:
    cache = HashCache(tmp_path / "hashes.sqlite3")
    left = _record(tmp_path / "left.jpg", inode=10)
    right = _record(tmp_path / "right.jpg", inode=11)

    assert cache.mark_distinct([right, left]) == 1
    assert cache.distinct_pairs([left, right]) == {(left.path, right.path)}

    changed = _record(tmp_path / "right.jpg", inode=11)
    changed.size += 1
    assert cache.distinct_pairs([left, changed]) == set()
    cache.close()


def test_identity_matches_legacy_json_encoding(tmp_path: Path) -> None:
    """Reviewed-distinct rows written by earlier versions must still match."""
    record = _record(tmp_path / "photo.jpg", inode=10)
    record.mtime = 1.5

    legacy = json.dumps(
        [record.size, record.mtime_ns, record.mtime, record.device, record.inode],
        separators=(",", ":"),
    )

    assert HashCache._identity(record) == legacy

    record.mtime_ns = None
    record.device = None
    legacy_missing = json.dumps(
        [record.size, record.mtime_ns, record.mtime, record.device, record.inode],
        separators=(",", ":"),
    )
    assert HashCache._identity(record) == legacy_missing


def test_store_all_batches_writes(tmp_path: Path, monkeypatch) -> None:
    """A scan persists in batched statements, not one round trip per record."""
    cache = HashCache(tmp_path / "hashes.sqlite3")
    batches: list[int] = []
    executes: list[str] = []
    connection = cache._conn

    class CountingConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def executemany(self, sql, rows):
            rows = list(rows)
            batches.append(len(rows))
            return connection.executemany(sql, rows)

        def execute(self, sql, *args):
            executes.append(sql)
            return connection.execute(sql, *args)

    monkeypatch.setattr(cache, "_conn", CountingConnection())

    records = []
    for index in range(2500):
        record = _record(tmp_path / f"photo{index}.jpg", inode=index)
        record.tile_phashes = "t2:0,1,2,3,4"
        records.append(record)
    # A record with nothing worth caching stays out of the batch.
    skipped = _record(tmp_path / "empty.jpg", inode=99999)
    skipped.phash = None
    records.append(skipped)

    cache.store_all(records)

    assert batches == [1000, 1000, 500]
    assert not any("INSERT INTO hashes" in sql for sql in executes)
    stored = connection.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]
    assert stored == 2500

    reloaded = _record(tmp_path / "photo7.jpg", inode=7)
    reloaded.phash = None
    assert cache.hydrate([reloaded]) == 1
    assert reloaded.phash == "0" * 16
    assert reloaded.tile_phashes == "t2:0,1,2,3,4"
    cache.close()


def test_hydrate_batches_reads(tmp_path: Path, monkeypatch) -> None:
    """Hydration uses chunked IN queries, not one round trip per record."""
    cache = HashCache(tmp_path / "hashes.sqlite3")
    records = []
    for index in range(1000):
        record = _record(tmp_path / f"photo{index}.jpg", inode=index)
        record.tile_phashes = "t2:0,1,2,3,4"
        records.append(record)
    cache.store_all(records)

    selects: list[str] = []
    connection = cache._conn

    class CountingConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("SELECT"):
                selects.append(sql)
            return connection.execute(sql, *args)

    monkeypatch.setattr(cache, "_conn", CountingConnection())

    fresh = []
    for index in range(1000):
        record = _record(tmp_path / f"photo{index}.jpg", inode=index)
        record.phash = None
        record.tile_phashes = None
        fresh.append(record)

    assert cache.hydrate(fresh) == 1000
    assert all(record.tile_phashes == "t2:0,1,2,3,4" for record in fresh)
    # 1000 paths / 400 per batch = 3 path queries; no misses, so no fallback.
    assert len(selects) == 3
    cache.close()


def test_hydrate_batches_identity_fallback(tmp_path: Path, monkeypatch) -> None:
    """Renamed files reuse hashes via one batched device/inode query pass."""
    cache = HashCache(tmp_path / "hashes.sqlite3")
    originals = []
    for index in range(20):
        original = _record(tmp_path / "before" / f"photo{index}.jpg", inode=index)
        original.tile_phashes = "t2:0,1,2,3,4"
        originals.append(original)
    cache.store_all(originals)

    selects: list[str] = []
    connection = cache._conn

    class CountingConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("SELECT"):
                selects.append(sql)
            return connection.execute(sql, *args)

    monkeypatch.setattr(cache, "_conn", CountingConnection())

    moved = []
    for index in range(20):
        record = _record(tmp_path / "after" / f"renamed{index}.jpg", inode=index)
        record.phash = None
        record.tile_phashes = None
        moved.append(record)

    assert cache.hydrate(moved) == 20
    assert all(record.phash == "0" * 16 for record in moved)
    # 1 path batch (misses) + 1 identity fallback batch.
    assert len(selects) == 2
    cache.close()
