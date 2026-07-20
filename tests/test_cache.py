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
