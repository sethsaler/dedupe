"""Ordered video fingerprint comparisons."""

from pathlib import Path
from types import SimpleNamespace

from dedupe.models import FileRecord, MediaType
from dedupe.similar_video import (
    HASH_FRAME_SIZE,
    _sample_timestamps,
    compute_video_fingerprint,
    find_similar_video_groups,
    video_fingerprint_distances,
)


def test_video_fingerprint_preserves_frame_order() -> None:
    first = "0000000000000000"
    second = "ffffffffffffffff"
    ordered = f"v2:{first},{second}"
    reversed_order = f"v2:{second},{first}"

    assert video_fingerprint_distances(ordered, ordered) == [0, 0]
    assert video_fingerprint_distances(ordered, reversed_order) == [64, 64]


def test_video_fingerprint_fast_seeks_to_sampled_frames(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout=bytes([len(calls)]) * (HASH_FRAME_SIZE * HASH_FRAME_SIZE),
        )

    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)
    monkeypatch.setattr("dedupe.similar_video.probe_video", lambda _path: (60.0, 1920, 1080))
    monkeypatch.setattr("dedupe.similar_video.subprocess.run", fake_run)

    fingerprint, width, height, duration = compute_video_fingerprint(tmp_path / "video.mp4")

    assert fingerprint is not None and fingerprint.startswith("v3:")
    assert len(fingerprint[3:].split(",")) == 8
    assert (width, height, duration) == (1920, 1080, 60.0)
    assert len(calls) == 8
    assert [float(cmd[cmd.index("-ss") + 1]) for cmd in calls] == _sample_timestamps(60.0)
    assert all(cmd[-1] == "pipe:1" for cmd in calls)


def test_video_fingerprint_rejects_incomplete_seek_frame(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)
    monkeypatch.setattr("dedupe.similar_video.probe_video", lambda _path: (10.0, 640, 480))
    monkeypatch.setattr(
        "dedupe.similar_video.subprocess.run",
        lambda _cmd, **_kwargs: SimpleNamespace(returncode=0, stdout=b"partial"),
    )

    fingerprint, width, height, duration = compute_video_fingerprint(tmp_path / "broken.mp4")

    assert fingerprint is None
    assert (width, height, duration) == (640, 480, 10.0)


def test_video_clustering_uses_duration_without_changing_matches(monkeypatch) -> None:
    fingerprint = "v3:" + ",".join(["0123456789abcdef"] * 8)

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
        "v3:" + ",".join(["fedcba9876543210"] * 8),
    )
    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)

    groups = find_similar_video_groups([original, close, too_long, different])

    assert len(groups) == 1
    assert {record.path for record in groups[0]} == {"original.mp4", "close.mp4"}

    reviewed_pair = tuple(sorted((original.path, close.path)))
    assert find_similar_video_groups(
        [original, close, too_long, different], distinct_pairs={reviewed_pair}
    ) == []
