"""Ordered video fingerprint comparisons."""

import json
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
    probe_video,
    video_fingerprint_distances,
)


def test_video_fingerprint_preserves_frame_order() -> None:
    first = "0000000000000000"
    second = "ffffffffffffffff"
    ordered = f"v2:{first},{second}"
    reversed_order = f"v2:{second},{first}"

    assert video_fingerprint_distances(ordered, ordered) == [0, 0]
    assert video_fingerprint_distances(ordered, reversed_order) == [64, 64]


def test_legacy_longer_fingerprint_still_compares_with_current_samples() -> None:
    sample = "0123456789abcdef"
    legacy = "v2:" + ",".join([sample] * 16)
    current = "v3:" + ",".join([sample] * 8)

    assert video_fingerprint_distances(legacy, current) == [0] * 8
    assert video_fingerprint_distances(current, legacy) == [0] * 8


def _piped_batch_runner(calls: list[list[str]], *, count: int = 8):
    """Fake subprocess.run that returns concatenated piped rawvideo frames."""

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        payload = b"".join(
            bytes([position + 1]) * (HASH_FRAME_SIZE * HASH_FRAME_SIZE)
            for position in range(count)
        )
        return SimpleNamespace(returncode=0, stdout=payload)

    return fake_run


def test_video_fingerprint_extracts_all_frames_in_one_ffmpeg_call(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)
    monkeypatch.setattr("dedupe.similar_video.probe_video", lambda _path: (60.0, 1920, 1080))
    monkeypatch.setattr("dedupe.similar_video.subprocess.run", _piped_batch_runner(calls))

    fingerprint, width, height, duration = compute_video_fingerprint(tmp_path / "video.mp4")

    assert fingerprint is not None and fingerprint.startswith("v3:")
    assert len(fingerprint[3:].split(",")) == 8
    assert (width, height, duration) == (1920, 1080, 60.0)
    # Single-pass sequential pipe for D <= 180s: one process, zero temp files.
    assert len(calls) == 1
    command = calls[0]
    assert "pipe:1" in command
    assert command.count("-i") == 1
    assert "-y" not in command


def test_video_fingerprint_falls_back_to_per_frame_extraction(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        # Sequential and multi-seek batched pipes fail; per-frame seeks succeed.
        is_per_frame = (
            "-ss" in cmd
            and "-frames:v" in cmd
            and cmd[cmd.index("-frames:v") + 1] == "1"
            and "pipe:1" in cmd
        )
        if not is_per_frame:
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
    per_frame_calls = [
        cmd
        for cmd in calls
        if "-ss" in cmd
        and "-frames:v" in cmd
        and cmd[cmd.index("-frames:v") + 1] == "1"
        and "pipe:1" in cmd
    ]
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
    """The piped multi-seek extractor retries in software when hardware decode fails."""
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if "-hwaccel" in cmd:
            return SimpleNamespace(returncode=1, stdout=b"")
        payload = b"".join(
            bytes([position + 1]) * (HASH_FRAME_SIZE * HASH_FRAME_SIZE)
            for position in range(2)
        )
        return SimpleNamespace(returncode=0, stdout=payload)

    monkeypatch.setattr(
        "dedupe.similar_video._hwaccel_args",
        lambda: ("-hwaccel", "videotoolbox"),
    )
    monkeypatch.setattr("dedupe.similar_video.subprocess.run", fake_run)

    frames = _extract_hash_frames(tmp_path / "video.mp4", [0.0, 1.0])

    assert len(frames) == 2
    assert any("-hwaccel" in cmd for cmd in calls)
    assert "-hwaccel" not in calls[-1]
    assert "pipe:1" in calls[-1] and "-y" not in calls[-1]


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


@pytest.mark.parametrize(
    ("stream_duration", "container_duration", "expected"),
    [("3.0", "10.0", 3.0), ("N/A", "10.0", 10.0),
     (None, "10.0", 10.0), ("nan", "inf", None), ("0", "-1", None)],
)
def test_probe_uses_valid_video_duration_before_container_duration(
    monkeypatch, stream_duration, container_duration, expected
) -> None:
    monkeypatch.setattr("dedupe.similar_video.shutil.which", lambda _name: "ffprobe")
    monkeypatch.setattr(
        "dedupe.similar_video.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "streams": [{"duration": stream_duration, "width": 640, "height": 480}],
                "format": {"duration": container_duration},
            }),
        ),
    )

    assert probe_video("video.mp4") == (expected, 640, 480)


def test_video_with_longer_audio_still_matches_its_video_copy(tmp_path: Path) -> None:
    """A longer audio stream must not produce seeks past the final video frame."""
    from dedupe.scanner import inventory

    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    with_audio = tmp_path / "long-audio.mp4"
    video_only = tmp_path / "video-only.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc2=size=160x120:rate=12:duration=3", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=10", "-threads", "1", str(with_audio),
        ],
        capture_output=True, check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(with_audio),
            "-an", "-c:v", "copy", str(video_only),
        ],
        capture_output=True, check=True,
    )
    records = inventory([with_audio, video_only])

    groups = find_similar_video_groups(records, workers=1)

    assert [{record.path for record in group} for group in groups] == [
        {str(with_audio), str(video_only)}
    ]
    assert all(record.duration == pytest.approx(3.0) for record in records)
    assert all(record.error is None for record in records)


def test_unknown_duration_video_rejects_undecodable_sample(tmp_path: Path, monkeypatch) -> None:
    from PIL import Image

    good, bad = tmp_path / "good.jpg", tmp_path / "bad.jpg"
    Image.new("RGB", (32, 32), "red").save(good)
    bad.write_bytes(b"incomplete image")
    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)
    monkeypatch.setattr("dedupe.similar_video.probe_video", lambda _path: (None, 640, 480))
    monkeypatch.setattr(
        "dedupe.similar_video._extract_frames", lambda *_args, **_kwargs: [good, bad]
    )

    assert compute_video_fingerprint(tmp_path / "video.mp4") == (None, 640, 480, None)


@pytest.mark.parametrize("invalid", ["v3:", "v3:xyz", "v3:0", "v3:" + "0" * 17,
                                    "v3:" + "0" * 16 + ","])
def test_malformed_cached_fingerprint_is_refreshed_without_losing_other_matches(
    monkeypatch, invalid
) -> None:
    valid = "v3:" + ",".join(["0123456789abcdef"] * 8)
    records = [
        FileRecord(
            path=f"{name}.mp4", size=1, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint=fingerprint, duration=10.0,
        )
        for name, fingerprint in [("a", valid), ("b", valid), ("broken", invalid)]
    ]
    refreshed = []

    def failed_refresh(path, **_kwargs):
        refreshed.append(path)
        return None, None, None, None

    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)
    monkeypatch.setattr("dedupe.similar_video.compute_video_fingerprint", failed_refresh)

    groups = find_similar_video_groups(records, workers=1)

    assert refreshed == ["broken.mp4"]
    assert [{record.path for record in group} for group in groups] == [{"a.mp4", "b.mp4"}]
    assert records[2].video_fingerprint is None
    assert records[2].error.startswith("video fingerprint failed:")
    assert video_fingerprint_distances(invalid, valid) is None


def test_current_fingerprint_with_too_many_samples_is_refreshed(monkeypatch) -> None:
    current = "v3:" + ",".join(["0123456789abcdef"] * 8)
    oversized = "v3:" + ",".join(["0123456789abcdef"] * 16)
    records = [
        FileRecord(
            path=f"{name}.mp4", size=1, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint=fingerprint, duration=10.0,
        )
        for name, fingerprint in [("normal", current), ("oversized", oversized)]
    ]
    refreshed = []

    def refresh(path, **_kwargs):
        refreshed.append(path)
        return current, 640, 480, 10.0

    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)
    monkeypatch.setattr("dedupe.similar_video.compute_video_fingerprint", refresh)

    groups = find_similar_video_groups(records, workers=1)

    assert refreshed == ["oversized.mp4"]
    assert records[1].video_fingerprint == current
    assert [{record.path for record in group} for group in groups] == [
        {"normal.mp4", "oversized.mp4"}
    ]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args, "-threads", "1"],
                   capture_output=True, check=True)


def test_near_miss_copy_is_verified_at_its_trimmed_offset(tmp_path: Path) -> None:
    """A slightly trimmed, cropped re-export lands just past the fingerprint threshold."""
    from dedupe.scanner import inventory

    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    source, copy, other = (tmp_path / name for name in ("source.mp4", "copy.mp4", "other.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24", "-t", "8", str(source))
    _ffmpeg("-ss", "0.16", "-i", str(source), "-vf", "crop=iw*0.97:ih,scale=310:240",
            "-crf", "28", str(copy))
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24", "-ss", "3", "-t", "8",
            str(other))
    records = inventory([source, copy, other])

    groups = find_similar_video_groups(records, workers=1)

    by_name = {Path(record.path).name: record for record in records}
    distances = video_fingerprint_distances(
        by_name["source.mp4"].video_fingerprint, by_name["copy.mp4"].video_fingerprint
    )
    assert 8 < sum(distances) / len(distances) <= 16  # fingerprint alone rejects it
    assert [{Path(record.path).name for record in group} for group in groups] == [
        {"source.mp4", "copy.mp4"}
    ]


def test_near_miss_pair_that_fails_offset_check_stays_separate(tmp_path: Path) -> None:
    from dedupe.scanner import inventory

    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    source, reframed = tmp_path / "source.mp4", tmp_path / "reframed.mp4"
    _ffmpeg("-f", "lavfi", "-i", "mandelbrot=size=320x240:rate=12", "-t", "6", str(source))
    # A deeper crop of a constantly zooming image changes every sampled frame.
    _ffmpeg("-i", str(source), "-vf", "crop=iw*0.88:ih*0.88,scale=300:226", "-crf", "28",
            str(reframed))
    records = inventory([source, reframed])

    groups = find_similar_video_groups(records, workers=1)

    distances = video_fingerprint_distances(*(record.video_fingerprint for record in records))
    assert 8 < sum(distances) / len(distances) <= 16
    assert groups == []


@pytest.mark.parametrize(
    ("cores", "workers", "hardware", "expected"),
    [
        (16, 4, True, 6),
        (8, 4, True, 2),
        (64, 4, True, 6),  # capped
        (4, 4, True, 0),  # no cores to spare
        (16, 2, True, 0),  # a lower worker setting asks for a lighter scan
        (16, 4, False, 0),  # software is already the only decoder
    ],
)
def test_software_decode_jobs_are_added_only_beside_a_full_hardware_lane(
    monkeypatch, cores: int, workers: int, hardware: bool, expected: int
) -> None:
    import dedupe.similar_video as module

    monkeypatch.setattr(module.os, "cpu_count", lambda: cores)
    monkeypatch.setattr(
        module, "_hwaccel_args", lambda: ("-hwaccel", "videotoolbox") if hardware else ()
    )

    assert module._software_decode_workers(workers) == expected


def test_explicit_worker_budget_expands_software_decode_lane(monkeypatch) -> None:
    import dedupe.similar_video as module

    monkeypatch.setattr(module.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(module, "_hwaccel_args", lambda: ("-hwaccel", "videotoolbox"))

    assert module._software_decode_workers(4, requested_workers=16) == 12


def test_software_decode_job_does_not_try_the_hardware_decoder(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(
            returncode=0,
            stdout=bytes(2 * HASH_FRAME_SIZE * HASH_FRAME_SIZE),
        )

    monkeypatch.setattr(
        "dedupe.similar_video._hwaccel_args", lambda: ("-hwaccel", "videotoolbox")
    )
    monkeypatch.setattr("dedupe.similar_video.subprocess.run", fake_run)

    frames = _extract_hash_frames(tmp_path / "video.mp4", [0.0, 1.0], software=True)

    assert len(frames) == 2
    assert len(calls) == 1 and "-hwaccel" not in calls[0]


def test_hardware_lane_stays_at_its_cap_while_software_jobs_run_beside_it(monkeypatch) -> None:
    import threading
    import time

    import dedupe.similar_video as module

    lock = threading.Lock()
    running = {"hardware": 0, "software": 0}
    peak = {"hardware": 0, "software": 0}

    def fake_fingerprint(path, *, on_frame=None, software=False):
        lane = "software" if software else "hardware"
        with lock:
            running[lane] += 1
            peak[lane] = max(peak[lane], running[lane])
        time.sleep(0.02)
        with lock:
            running[lane] -= 1
        return "v3:" + ",".join([f"{int(Path(path).stem):016x}"] * 8), 640, 480, 10.0

    monkeypatch.setattr(module, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(module, "_hwaccel_args", lambda: ("-hwaccel", "videotoolbox"))
    monkeypatch.setattr(module.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(module, "compute_video_fingerprint", fake_fingerprint)
    records = [
        FileRecord(
            path=f"/{index}.mp4", size=1, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", duration=10.0,
        )
        for index in range(1, 41)
    ]

    find_similar_video_groups(records, workers=8)

    assert all(record.video_fingerprint for record in records)
    assert peak["hardware"] == module.DEFAULT_VIDEO_WORKERS_CAP
    assert 1 <= peak["software"] <= module.MAX_SOFTWARE_DECODE_WORKERS


def test_exact_duplicate_videos_decode_once_and_reuse_cached_donor(
    tmp_path: Path, monkeypatch
) -> None:
    import dedupe.similar_video as module
    from dedupe.cache import HashCache
    from dedupe.scanner import inventory

    payload = (b"video-prefix-0123" * 6000) + b"tail-bytes"
    copies = [tmp_path / f"copy_{i}.mp4" for i in range(3)]
    for path in copies:
        path.write_bytes(payload)

    fp = "v3:" + ",".join(["0123456789abcdef"] * 8)
    decoded: list[str] = []

    def fake_fingerprint(path, *, on_frame=None, software=False):
        decoded.append(str(path))
        return fp, 1920, 1080, 12.5

    monkeypatch.setattr(module, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(module, "compute_video_fingerprint", fake_fingerprint)

    records = inventory(copies)
    groups = find_similar_video_groups(records, workers=4)

    assert len(decoded) == 1
    assert len(groups) == 1 and len(groups[0]) == 3
    assert all(r.video_fingerprint == fp and r.width == 1920 and r.duration == 12.5 for r in records)

    cache = HashCache(tmp_path / "cache.sqlite3")
    try:
        cache.store_all(records)
        fourth = tmp_path / "copy_3.mp4"
        fourth.write_bytes(payload)
        warm_records = inventory([*copies, fourth])
        assert cache.hydrate(warm_records) == 3

        decoded.clear()
        warm_groups = find_similar_video_groups(warm_records, workers=4)
    finally:
        cache.close()

    assert decoded == []
    assert len(warm_groups) == 1 and len(warm_groups[0]) == 4
    assert all(r.video_fingerprint == fp and r.height == 1080 for r in warm_records)


def test_near_miss_verification_runs_in_parallel_and_deduplicates_frame_batches(
    tmp_path: Path, monkeypatch
) -> None:
    import threading

    import dedupe.similar_video as module

    base = [0x0F0F0F0F0F0F0F0F ^ (i * 0x0111) for i in range(8)]
    # 2 bits differing per frame => 16 bits? Wait: 9 bits diff per frame => mean_distance = 9 in (8, 16].
    near = [h ^ 0x1FF for h in base]
    fp_base = "v3:" + ",".join(f"{h:016x}" for h in base)
    fp_near = "v3:" + ",".join(f"{h:016x}" for h in near)

    def _diverse_frames():
        out = []
        for idx in range(7):
            buf = bytearray(HASH_FRAME_SIZE * HASH_FRAME_SIZE)
            for y in range(HASH_FRAME_SIZE):
                for x in range(HASH_FRAME_SIZE):
                    val = ((x * (idx + 1) + y * (idx + 3)) % 256)
                    buf[y * HASH_FRAME_SIZE + x] = val
            out.append(bytes(buf))
        return out

    diverse = _diverse_frames()
    call_counts: dict[tuple[str, tuple[float, ...]], int] = {}
    lock = threading.Lock()

    def fake_batches(path: str, timestamps: list[float], cancelled) -> list[bytes]:
        key = (path, tuple(round(t, 4) for t in timestamps))
        with lock:
            call_counts[key] = call_counts.get(key, 0) + 1
        return list(diverse)

    monkeypatch.setattr(module, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(module, "_version_frame_batches", fake_batches)

    def make_records():
        recs = [
            FileRecord(
                path=str(tmp_path / "src.mp4"),
                size=1000,
                mtime=1.0,
                media_type=MediaType.VIDEO,
                extension=".mp4",
                video_fingerprint=fp_base,
                duration=10.0,
            )
        ]
        for idx in range(1, 5):
            recs.append(
                FileRecord(
                    path=str(tmp_path / f"near_{idx}.mp4"),
                    size=1000 + idx,
                    mtime=1.0,
                    media_type=MediaType.VIDEO,
                    extension=".mp4",
                    video_fingerprint=fp_near,
                    duration=10.0,
                )
            )
        return recs

    g_serial = find_similar_video_groups(make_records(), workers=1)
    call_counts.clear()
    g_parallel = find_similar_video_groups(make_records(), workers=4)

    assert len(g_serial) == 1 and len(g_serial[0]) == 5
    assert len(g_parallel) == 1 and len(g_parallel[0]) == 5
    src_calls = [cnt for (p, _), cnt in call_counts.items() if p.endswith("src.mp4")]
    assert src_calls == [1]


def test_vectorized_clustering_matches_mixed_and_equal_length_fingerprints(monkeypatch) -> None:
    import dedupe.similar_video as module
    from dedupe.cache import DistinctReviews

    monkeypatch.setattr(module, "ffmpeg_available", lambda: True)

    short_fp = "v3:" + ",".join(["0000000000000000"] * 4)
    long_fp = "v3:" + ",".join(["0000000000000000"] * 8)
    other_fp = "v3:" + ",".join(["ffffffffffffffff"] * 8)
    records = [
        FileRecord(
            path="/short_unknown.mp4", size=100, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint=short_fp, duration=None,
        ),
        FileRecord(
            path="/long_20s.mp4", size=20, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint=long_fp, duration=20.0,
        ),
        FileRecord(
            path="/long_18s.mp4", size=21, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint=long_fp, duration=18.0,
        ),
        FileRecord(
            path="/too_short_17s.mp4", size=22, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint=long_fp, duration=17.9,
        ),
        FileRecord(
            path="/unrelated_20s.mp4", size=23, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint=other_fp, duration=20.0,
        ),
    ]

    groups = find_similar_video_groups(records, distinct=DistinctReviews.empty(), workers=1)
    # Unknown duration record must not bridge 20.0s and 17.9s (>10% apart) into one group.
    assert len(groups) == 1
    group_paths = {r.path for r in groups[0]}
    assert "/short_unknown.mp4" in group_paths
    assert not ({"/long_20s.mp4", "/too_short_17s.mp4"} <= group_paths)
    # Check exact 0.9 (18.0 / 20.0) vs 0.895 (17.9 / 20.0) duration cutoff directly.
    at_cutoff = find_similar_video_groups([records[1], records[2], records[4]], workers=1)
    assert [{r.path for r in g} for g in at_cutoff] == [{"/long_20s.mp4", "/long_18s.mp4"}]
    below_cutoff = find_similar_video_groups([records[1], records[3], records[4]], workers=1)
    assert below_cutoff == []


def test_sample_timestamps_avoids_zero_and_eliminates_short_clip_cliff() -> None:
    for duration in (2.0, 2.5, 3.0, 5.5, 7.9, 60.0):
        ts = _sample_timestamps(duration)
        assert len(ts) == 8
        assert ts[0] > 0.0
        assert ts[-1] < duration
        assert ts == [round((i + 0.5) * duration / 8, 6) for i in range(8)]
    sub_second = _sample_timestamps(0.6)
    assert len(sub_second) == 3
    assert sub_second[0] > 0.0 and sub_second[-1] < 0.6
    # Explicit non-default frame budget keeps legacy behavior for detectors.
    assert _sample_timestamps(60.0, 16) == [index * 3.75 for index in range(16)]


@pytest.mark.parametrize(
    ("stream_extra", "expected_dims"),
    [
        ({"side_data_list": [{"rotation": -90}]}, (480, 640)),
        ({"side_data_list": [{"rotation": 90}]}, (480, 640)),
        ({"side_data_list": [{"rotation": 270}]}, (480, 640)),
        ({"tags": {"rotate": "90"}}, (480, 640)),
        ({"tags": {"rotate": "180"}}, (640, 480)),
        ({"side_data_list": [{"rotation": 0}]}, (640, 480)),
    ],
)
def test_probe_video_swaps_coded_dimensions_for_quarter_turn_rotation(
    monkeypatch, stream_extra: dict, expected_dims: tuple[int, int]
) -> None:
    monkeypatch.setattr("dedupe.similar_video.shutil.which", lambda _name: "ffprobe")
    stream = {"duration": "12.0", "width": 640, "height": 480, **stream_extra}
    monkeypatch.setattr(
        "dedupe.similar_video.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [stream], "format": {"duration": "12.0"}}),
        ),
    )

    duration, width, height = probe_video("rotated.mp4")

    assert duration == 12.0
    assert (width, height) == expected_dims


def test_long_video_uses_piped_concat_seeks_and_forwards_software_and_cancel(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(
            returncode=0,
            stdout=b"".join(
                bytes([idx + 1]) * (HASH_FRAME_SIZE * HASH_FRAME_SIZE) for idx in range(8)
            ),
        )

    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: True)
    monkeypatch.setattr("dedupe.similar_video.probe_video", lambda _path, **_kw: (300.0, 1920, 1080))
    monkeypatch.setattr(
        "dedupe.similar_video._hwaccel_args", lambda: ("-hwaccel", "videotoolbox")
    )
    monkeypatch.setattr("dedupe.similar_video.subprocess.run", fake_run)

    fp, width, height, duration = compute_video_fingerprint(tmp_path / "long.mp4", software=True)

    assert fp is not None and fp.startswith("v3:")
    assert (width, height, duration) == (1920, 1080, 300.0)
    assert len(calls) == 1
    cmd = calls[0]
    assert "pipe:1" in cmd and "-y" not in cmd
    assert "-hwaccel" not in cmd
    assert cmd.count("-ss") == 8

    # Software mode is also forwarded to per-frame fallback when batched pipe fails.
    fallback_calls: list[list[str]] = []

    def fail_batch_run(cmd, **_kwargs):
        fallback_calls.append(list(cmd))
        if "-frames:v" in cmd and cmd[cmd.index("-frames:v") + 1] == "1":
            return SimpleNamespace(
                returncode=0, stdout=bytes([9]) * (HASH_FRAME_SIZE * HASH_FRAME_SIZE)
            )
        return SimpleNamespace(returncode=1, stdout=b"")

    monkeypatch.setattr("dedupe.similar_video.subprocess.run", fail_batch_run)
    fp2, *_ = compute_video_fingerprint(tmp_path / "long.mp4", software=True)
    assert fp2 is not None
    assert all("-hwaccel" not in c for c in fallback_calls)

    with pytest.raises(InterruptedError):
        compute_video_fingerprint(tmp_path / "long.mp4", cancelled=lambda: True)


def test_unknown_duration_hub_cannot_bridge_incompatible_durations(monkeypatch) -> None:
    import dedupe.similar_video as module
    from dedupe.cache import DistinctReviews

    monkeypatch.setattr(module, "ffmpeg_available", lambda: True)
    fp = "v3:" + ",".join(["0123456789abcdef"] * 8)
    records = [
        FileRecord(
            path="/unknown_hub.mp4", size=500, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint=fp, duration=None,
        ),
        FileRecord(
            path="/clip_10s.mp4", size=100, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint=fp, duration=10.0,
        ),
        FileRecord(
            path="/clip_30s.mp4", size=200, mtime=1, media_type=MediaType.VIDEO,
            extension=".mp4", video_fingerprint=fp, duration=30.0,
        ),
    ]

    groups = find_similar_video_groups(records, distinct=DistinctReviews.empty(), workers=1)

    for group in groups:
        paths = {r.path for r in group}
        assert not ({"/clip_10s.mp4", "/clip_30s.mp4"} <= paths)


def test_first_last_frame_mismatch_reaches_verification_and_dual_end_trims_match(
    tmp_path: Path,
) -> None:
    """An added end card or dual-end slight trim must still match via offset verification."""
    from dedupe.scanner import inventory

    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    base = tmp_path / "base.mp4"
    with_end_card = tmp_path / "end_card.mp4"
    dual_trimmed = tmp_path / "dual_trimmed.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=240x180:rate=24", "-t", "8", str(base))
    # Replace the last 1.2s (including final sample frame) with solid blue end card.
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=240x180:rate=24:duration=6.8",
        "-f", "lavfi", "-i", "color=c=blue:size=240x180:rate=24:duration=1.2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", str(with_end_card),
    )
    # Trim both ends slightly: 0.25s from start and 0.35s from end (7.4s total).
    _ffmpeg("-ss", "0.25", "-i", str(base), "-t", "7.4", "-crf", "26", str(dual_trimmed))

    records_end = inventory([base, with_end_card])
    groups_end = find_similar_video_groups(records_end, workers=1)
    assert [{Path(r.path).name for r in g} for g in groups_end] == [{"base.mp4", "end_card.mp4"}]

    records_dual = inventory([base, dual_trimmed])
    groups_dual = find_similar_video_groups(records_dual, workers=1)
    assert [{Path(r.path).name for r in g} for g in groups_dual] == [
        {"base.mp4", "dual_trimmed.mp4"}
    ]


def test_blank_static_and_hue_shifted_clips_stay_separate_while_bw_matches(
    tmp_path: Path,
) -> None:
    """Variance, temporal diversity, and chroma guards reject dark/static/recolored clips."""
    from dedupe.scanner import inventory

    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    black_a, black_b = tmp_path / "black_a.mp4", tmp_path / "black_b.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:size=160x120:rate=12:duration=4", str(black_a))
    _ffmpeg("-f", "lavfi", "-i", "color=c=#141414:size=160x120:rate=12:duration=4", str(black_b))
    assert find_similar_video_groups(inventory([black_a, black_b]), workers=1) == []

    # Static frozen clips built from a still frame.
    still = tmp_path / "still.png"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=1", "-frames:v", "1", str(still))
    static_a, static_b = tmp_path / "static_a.mp4", tmp_path / "static_b.mp4"
    _ffmpeg("-loop", "1", "-i", str(still), "-t", "4", "-r", "12", "-crf", "20", str(static_a))
    _ffmpeg("-loop", "1", "-i", str(still), "-t", "4", "-r", "12", "-crf", "28", str(static_b))
    assert find_similar_video_groups(inventory([static_a, static_b]), workers=1) == []

    # Color-shifted (hue=120) vs original must be rejected by chroma guard,
    # whereas grayscale re-grade still matches original.
    orig, recolored, bw = tmp_path / "orig.mp4", tmp_path / "recolored.mp4", tmp_path / "bw.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=12:duration=4", str(orig))
    _ffmpeg("-i", str(orig), "-vf", "hue=h=120", str(recolored))
    _ffmpeg("-i", str(orig), "-vf", "hue=s=0", str(bw))

    assert find_similar_video_groups(inventory([orig, recolored]), workers=1) == []
    # Re-run on pre-cached fingerprints to verify guards enforce on warm scans too.
    warm_src = inventory([orig, recolored])
    compute_orig = compute_video_fingerprint(orig)
    compute_rec = compute_video_fingerprint(recolored)
    warm_src[0].video_fingerprint, warm_src[0].duration = compute_orig[0], compute_orig[3]
    warm_src[1].video_fingerprint, warm_src[1].duration = compute_rec[0], compute_rec[3]
    assert find_similar_video_groups(warm_src, workers=1) == []

    bw_groups = find_similar_video_groups(inventory([orig, bw]), workers=1)
    assert [{Path(r.path).name for r in g} for g in bw_groups] == [{"orig.mp4", "bw.mp4"}]


def test_cropped_border_roi_and_horizontally_flipped_videos_match(tmp_path: Path) -> None:
    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    from dedupe.scanner import inventory

    orig = tmp_path / "orig.mp4"
    letterboxed = tmp_path / "letterboxed.mp4"
    flipped = tmp_path / "flipped.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=4", str(orig))
    # Pad 70px black bars top and bottom -> 320x320 letterboxed frame; bars distort uncropped pHash.
    _ffmpeg(
        "-i", str(orig), "-vf", "pad=320:320:0:70:black", "-crf", "24", str(letterboxed)
    )
    _ffmpeg("-i", str(orig), "-vf", "hflip", "-crf", "24", str(flipped))

    box_records = inventory([orig, letterboxed])
    # Attach detected border_crop ("left,top,right,bottom,width,height") to the letterboxed video.
    box_records[1].border_crop = "0,70,320,250,320,320"
    crop_groups = find_similar_video_groups(box_records, workers=1)
    assert [{Path(r.path).name for r in g} for g in crop_groups] == [
        {"orig.mp4", "letterboxed.mp4"}
    ]

    flip_records = inventory([orig, flipped])
    flip_groups = find_similar_video_groups(flip_records, workers=1)
    assert [{Path(r.path).name for r in g} for g in flip_groups] == [{"orig.mp4", "flipped.mp4"}]
