"""Ordered video fingerprint comparisons."""

from dedupe.models import FileRecord, MediaType
from dedupe.similar_video import find_similar_video_groups, video_fingerprint_distances


def test_video_fingerprint_preserves_frame_order() -> None:
    first = "0000000000000000"
    second = "ffffffffffffffff"
    ordered = f"v2:{first},{second}"
    reversed_order = f"v2:{second},{first}"

    assert video_fingerprint_distances(ordered, ordered) == [0, 0]
    assert video_fingerprint_distances(ordered, reversed_order) == [64, 64]


def test_video_clustering_uses_duration_without_changing_matches(monkeypatch) -> None:
    fingerprint = "v2:" + ",".join(["0123456789abcdef"] * 8)

    def record(path: str, duration: float, value: str = fingerprint) -> FileRecord:
        return FileRecord(
            path=path,
            size=1,
            mtime=1,
            media_type=MediaType.VIDEO,
            extension=".mp4",
            video_fingerprint=value,
            duration=duration,
        )

    close = record("close.mp4", 109)
    original = record("original.mp4", 100)
    too_long = record("too-long.mp4", 125)
    different = record(
        "different.mp4",
        100,
        "v2:" + ",".join(["fedcba9876543210"] * 8),
    )
    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)

    groups = find_similar_video_groups([original, close, too_long, different])

    assert len(groups) == 1
    assert {record.path for record in groups[0]} == {"original.mp4", "close.mp4"}

    reviewed_pair = tuple(sorted((original.path, close.path)))
    assert find_similar_video_groups(
        [original, close, too_long, different], distinct_pairs={reviewed_pair}
    ) == []
