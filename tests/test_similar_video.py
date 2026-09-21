"""Ordered video fingerprint comparisons."""

import random
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dedupe.cache import DistinctReviews
from dedupe.models import FileRecord, MediaType
from dedupe.similar_video import (
    HASH_FRAME_SIZE,
    _extract_hash_frame,
    _extract_hash_frames,
    _sample_timestamps,
    compute_video_fingerprint,
    ffmpeg_available,
    find_similar_video_groups,
    probe_duration,
    video_fingerprint_distances,
)


def test_video_fingerprint_preserves_frame_order() -> None:
    first = "0000000000000000"
    second = "ffffffffffffffff"
    ordered = f"v2:{first},{second}"
    reversed_order = f"v2:{second},{first}"

    assert video_fingerprint_distances(ordered, ordered) == [0, 0]
    assert video_fingerprint_distances(ordered, reversed_order) == [64, 64]


def _single_pass_runner(calls: list[list[str]]):
    """Fake subprocess.run that writes the batched extractor's frame outputs."""

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        targets = [cmd[index + 1] for index, arg in enumerate(cmd) if arg == "-y"]
        for position, target in enumerate(targets):
            Path(target).write_bytes(
                bytes([position + 1]) * (HASH_FRAME_SIZE * HASH_FRAME_SIZE)
            )
        return SimpleNamespace(returncode=0, stdout=b"")

    return fake_run


def test_video_fingerprint_extracts_all_frames_in_one_ffmpeg_call(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)
    monkeypatch.setattr("dedupe.similar_video.probe_video", lambda _path: (60.0, 1920, 1080))
    monkeypatch.setattr("dedupe.similar_video.subprocess.run", _single_pass_runner(calls))

    fingerprint, width, height, duration = compute_video_fingerprint(tmp_path / "video.mp4")

    assert fingerprint is not None and fingerprint.startswith("v3:")
    assert len(fingerprint[3:].split(",")) == 8
    assert (width, height, duration) == (1920, 1080, 60.0)
    # One process for the whole video instead of one per sampled frame.
    assert len(calls) == 1
    command = calls[0]
    seeks = [
        float(command[index + 1])
        for index, arg in enumerate(command)
        if arg == "-ss"
    ]
    assert seeks == _sample_timestamps(60.0)
    assert command.count("-map") == 8
    assert command.count("-i") == 8


def test_video_fingerprint_falls_back_to_per_frame_extraction(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        # The batched pass produces nothing; per-frame seeks still succeed.
        if "pipe:1" not in cmd:
            return SimpleNamespace(returncode=1, stdout=b"")
        return SimpleNamespace(
            returncode=0,
            stdout=bytes([len(calls)]) * (HASH_FRAME_SIZE * HASH_FRAME_SIZE),
        )

    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)
    monkeypatch.setattr("dedupe.similar_video.probe_video", lambda _path: (60.0, 1920, 1080))
    monkeypatch.setattr("dedupe.similar_video.subprocess.run", fake_run)

    fingerprint, _width, _height, _duration = compute_video_fingerprint(tmp_path / "video.mp4")

    assert fingerprint is not None and fingerprint.startswith("v3:")
    assert len(fingerprint[3:].split(",")) == 8
    per_frame_calls = [cmd for cmd in calls if "pipe:1" in cmd]
    assert len(per_frame_calls) == 8
    assert [
        float(cmd[cmd.index("-ss") + 1]) for cmd in per_frame_calls
    ] == _sample_timestamps(60.0)


def test_hash_frame_extraction_falls_back_to_software_decode(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed hardware-decode attempt is retried with software decode."""
    calls: list[list[str]] = []
    expected = bytes(HASH_FRAME_SIZE * HASH_FRAME_SIZE)

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if "-hwaccel" in cmd:
            return SimpleNamespace(returncode=1, stdout=b"")
        return SimpleNamespace(returncode=0, stdout=expected)

    monkeypatch.setattr(
        "dedupe.similar_video._hwaccel_args",
        lambda: ("-hwaccel", "videotoolbox"),
    )
    monkeypatch.setattr("dedupe.similar_video.subprocess.run", fake_run)

    raw = _extract_hash_frame(tmp_path / "video.mp4", 1.0)

    assert raw == expected
    assert any("-hwaccel" in cmd for cmd in calls)
    assert "-hwaccel" not in calls[-1]


def test_batched_extraction_falls_back_to_software_decode(
    tmp_path: Path, monkeypatch
) -> None:
    """The single-pass extractor retries in software when hardware decode fails."""
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if "-hwaccel" in cmd:
            return SimpleNamespace(returncode=1, stdout=b"")
        targets = [cmd[index + 1] for index, arg in enumerate(cmd) if arg == "-y"]
        for position, target in enumerate(targets):
            Path(target).write_bytes(
                bytes([position + 1]) * (HASH_FRAME_SIZE * HASH_FRAME_SIZE)
            )
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(
        "dedupe.similar_video._hwaccel_args",
        lambda: ("-hwaccel", "videotoolbox"),
    )
    monkeypatch.setattr("dedupe.similar_video.subprocess.run", fake_run)

    frames = _extract_hash_frames(tmp_path / "video.mp4", [0.0, 1.0])

    assert len(frames) == 2
    assert any("-hwaccel" in cmd for cmd in calls)
    assert "-hwaccel" not in calls[-1]


def test_hwaccel_disabled_by_env(monkeypatch) -> None:
    from dedupe.similar_video import _hwaccel_args

    _hwaccel_args.cache_clear()
    monkeypatch.setenv("DEDUPE_DISABLE_HWACCEL", "1")
    try:
        assert _hwaccel_args() == ()
    finally:
        _hwaccel_args.cache_clear()


def test_batched_extraction_matches_per_frame_frames(tmp_path: Path) -> None:
    """Real ffmpeg: the single pass must return the same bytes as N seeks."""
    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    video = tmp_path / "sample.mp4"
    build = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=15:duration=6",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        capture_output=True,
        check=False,
    )
    if build.returncode != 0 or not video.exists():
        pytest.skip("ffmpeg cannot synthesize a test video here")

    duration = probe_duration(video)
    assert duration is not None
    timestamps = _sample_timestamps(duration)
    batched = _extract_hash_frames(video, timestamps)
    per_frame = [_extract_hash_frame(video, timestamp) for timestamp in timestamps]

    assert batched == per_frame


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


@pytest.mark.parametrize("failure", ["exception", "incomplete"])
def test_failed_video_rehash_cannot_reuse_legacy_fingerprint(monkeypatch, failure):
    import dedupe.similar_video as module

    records = [
        FileRecord(
            path=f"/{name}.mp4", size=1, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint="v2:" + ",".join(["0123456789abcdef"] * 8),
        )
        for name in ("a", "b")
    ]

    def failed_fingerprint(_path, **_kwargs):
        if failure == "exception":
            raise OSError("unreadable video")
        return None, 640, 480, 10.0

    monkeypatch.setattr(module, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(module, "compute_video_fingerprint", failed_fingerprint)

    assert find_similar_video_groups(records, workers=1) == []
    assert all(record.video_fingerprint is None for record in records)
    assert all(record.error.startswith("video fingerprint failed:") for record in records)


def test_video_similarity_reports_corruption_and_keeps_reencoded_match(tmp_path):
    """Exercise extraction and diagnostics on actual files, not supplied hashes."""
    from dedupe.scanner import inventory

    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    original, reexport, corrupt = (tmp_path / name for name in ("a.mp4", "b.mp4", "bad.mp4"))
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc2=size=160x120:rate=12:duration=3", "-threads", "1", str(original),
        ],
        capture_output=True, check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(original),
            "-vf", "scale=320:240", "-crf", "28", "-threads", "1", str(reexport),
        ],
        capture_output=True, check=True,
    )
    corrupt.write_bytes(b"not a video")
    records = inventory([original, reexport, corrupt])

    groups = find_similar_video_groups(records, workers=1)

    assert [{record.path for record in group} for group in groups] == [
        {str(original), str(reexport)}
    ]
    failed = next(record for record in records if record.path == str(corrupt))
    assert failed.video_fingerprint is None
    assert failed.error.startswith("video fingerprint failed:")


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
        [original, close, too_long, different],
        distinct=DistinctReviews.from_pairs({reviewed_pair}),
    ) == []


def test_reviewed_distinct_video_pair_survives_metadata_drift(
    tmp_path: Path, monkeypatch
) -> None:
    """Same fingerprint under fresh stat metadata keeps the pair suppressed."""
    from dedupe.cache import HashCache

    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)
    fingerprint_a = "v3:" + ",".join(["0123456789abcdef"] * 8)
    fingerprint_b = "v3:" + ",".join(["0123456789abcdee"] * 8)

    def record(path: Path, inode: int, mtime: float, fingerprint: str) -> FileRecord:
        return FileRecord(
            path=str(path),
            size=1,
            mtime=mtime,
            media_type=MediaType.VIDEO,
            extension=".mp4",
            device=2,
            inode=inode,
            mtime_ns=int(mtime * 1_000_000_000),
            video_fingerprint=fingerprint,
            duration=10.0,
        )

    cache = HashCache(tmp_path / "hashes.sqlite3")
    cache.mark_distinct(
        [
            record(tmp_path / "a.mp4", 10, 1.0, fingerprint_a),
            record(tmp_path / "b.mp4", 11, 1.0, fingerprint_b),
        ]
    )

    drifted = [
        record(tmp_path / "a.mp4", 10, 1.0, fingerprint_a),
        record(tmp_path / "b.mp4", 99, 2.0, fingerprint_b),
    ]
    reviews = cache.distinct_reviews(drifted)
    assert cache.distinct_pairs(drifted) == set()  # stat alone cannot confirm
    assert find_similar_video_groups(drifted, distinct=reviews) == []
    cache.close()


def _synthetic_video_records(seed: int = 7, count: int = 40) -> list[FileRecord]:
    """Clusters of perturbed fingerprints plus unrelated noise."""
    rng = random.Random(seed)
    records: list[FileRecord] = []
    for cluster in range(8):
        base = [rng.getrandbits(64) for _ in range(8)]
        duration = 20.0 + cluster * 3
        for member in range(count // 8):
            frames = []
            for value in base:
                noise = 0
                for _ in range(rng.choice([0, 0, 1, 3, 12])):
                    noise |= 1 << rng.randrange(64)
                frames.append(value ^ noise)
            records.append(
                FileRecord(
                    path=f"/tmp/c{cluster}-{member}.mp4",
                    size=100 + member,
                    mtime=1,
                    media_type=MediaType.VIDEO,
                    extension=".mp4",
                    video_fingerprint="v3:" + ",".join(f"{f:016x}" for f in frames),
                    duration=duration * rng.choice([1.0, 0.95, 1.05]),
                )
            )
    return records


def _reference_video_groups(records, threshold: int = 8):
    """Independent O(n^2) restatement of the documented matching contract."""
    from dedupe.grouping import cluster_around_best
    from dedupe.similar_video import _fingerprint_hashes

    max_frame_distance = max(threshold * 2, 4)
    adjacency = {record.path: set() for record in records}
    fingerprints = [_fingerprint_hashes(r.video_fingerprint or "") for r in records]
    for i, a in enumerate(records):
        for j in range(i + 1, len(records)):
            b = records[j]
            left, right = fingerprints[i], fingerprints[j]
            count = min(len(left), len(right))
            if count == 1:
                left_indexes = right_indexes = [0]
            else:
                left_indexes = [round(k * (len(left) - 1) / (count - 1)) for k in range(count)]
                right_indexes = [round(k * (len(right) - 1) / (count - 1)) for k in range(count)]
            distances = [
                (left[li] ^ right[ri]).bit_count()
                for li, ri in zip(left_indexes, right_indexes, strict=True)
            ]
            if not distances:
                continue
            if sum(distances) / len(distances) > threshold:
                continue
            if max(distances) > max_frame_distance:
                continue
            ratio = min(a.duration, b.duration) / max(a.duration, b.duration)
            if ratio < 0.9:
                continue
            adjacency[a.path].add(b.path)
            adjacency[b.path].add(a.path)
    return cluster_around_best(records, adjacency)


def test_duration_bucketing_matches_exhaustive_comparison(monkeypatch) -> None:
    """Candidate bucketing is an optimization: groups equal the exhaustive scan."""
    import dedupe.similar_video as module

    monkeypatch.setattr(module, "ffmpeg_available", lambda: True)

    def paths(groups):
        return sorted(tuple(sorted(r.path for r in group)) for group in groups)

    records = _synthetic_video_records()
    optimized = paths(module.find_similar_video_groups(records))
    reference = paths(_reference_video_groups(records))

    assert optimized == reference
    assert any(len(group) > 1 for group in reference)
