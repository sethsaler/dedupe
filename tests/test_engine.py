"""End-to-end engine scan with image fixtures."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from dedupe.engine import run_scan, run_scans_parallel
from dedupe.human_detection import human_detection_signature
from dedupe.keep_decisions import update_keep_decisions
from dedupe.models import GroupKind, ScanResult
from dedupe.similar_video import _extract_frames


def _save(path: Path, color: tuple[int, int, int], quality: int = 90) -> None:
    img = Image.new("RGB", (48, 48), color)
    for x in range(5, 25):
        for y in range(5, 25):
            img.putpixel((x, y), (255, color[1], 0))
    img.save(path, format="JPEG", quality=quality)


def test_run_scan_finds_exact_and_similar(tmp_path: Path) -> None:
    # Exact pair
    data = b"identical-binary-payload-for-exact-match!!!"
    (tmp_path / "exact1.jpg").write_bytes(data)
    (tmp_path / "exact2.jpg").write_bytes(data)

    # Similar pair (same visual, different quality)
    _save(tmp_path / "sim1.jpg", (30, 60, 90), quality=95)
    _save(tmp_path / "sim2.jpg", (30, 60, 90), quality=50)

    # Unique
    _save(tmp_path / "unique.jpg", (200, 10, 200), quality=90)

    result = run_scan(
        [tmp_path],
        exact=True,
        similar=True,
        include_videos=False,
        use_cache=False,
        image_threshold=12,
    )

    assert result.exact_groups >= 1
    assert len(result.files) == 5
    # At least one group overall
    assert len(result.groups) >= 1


def test_run_scan_cancel_midway_raises_promptly(tmp_path: Path) -> None:
    """Cancelling while stages overlap must not deadlock the stage pool.

    Regression: the dimensions stage set its done-event in a ``finally`` that
    did not cover its own cancel check, so a cancel raised there left the
    review stage waiting on the event forever.
    """
    for index in range(40):
        _save(tmp_path / f"img{index:02}.jpg", (index * 6 % 255, 60, 90), quality=90)
    cancel_after = {"n": 0}

    def cancelled() -> bool:
        cancel_after["n"] += 1
        return cancel_after["n"] > 3

    started = time.monotonic()
    with pytest.raises(InterruptedError, match="scan cancelled"):
        run_scan(
            [tmp_path],
            exact=True,
            similar=True,
            include_videos=False,
            use_cache=False,
            cancelled=cancelled,
        )
    assert time.monotonic() - started < 30


def test_run_scan_streams_groups_via_on_group(tmp_path: Path) -> None:
    """Groups should be published progressively (exact before similar finishes)."""
    data = b"identical-binary-payload-for-exact-match!!!"
    (tmp_path / "exact1.jpg").write_bytes(data)
    (tmp_path / "exact2.jpg").write_bytes(data)
    _save(tmp_path / "sim1.jpg", (30, 60, 90), quality=95)
    _save(tmp_path / "sim2.jpg", (30, 60, 90), quality=50)

    streamed: list[str] = []
    kinds: list[str] = []

    def on_group(g) -> None:
        streamed.append(g.id)
        kinds.append(g.kind.value)

    result = run_scan(
        [tmp_path],
        exact=True,
        similar=True,
        include_videos=False,
        use_cache=False,
        image_threshold=12,
        on_group=on_group,
    )

    assert len(streamed) == len(result.groups)
    assert set(streamed) == {g.id for g in result.groups}
    # Exact groups are published before similar groups
    if GroupKind.EXACT.value in kinds and GroupKind.SIMILAR.value in kinds:
        first_exact = kinds.index(GroupKind.EXACT.value)
        first_similar = kinds.index(GroupKind.SIMILAR.value)
        assert first_exact < first_similar


def test_similar_image_and_video_stages_run_concurrently(
    tmp_path: Path, monkeypatch
) -> None:
    """Image/GIF and video similarity are disjoint, so they run at the same time."""
    import threading

    import dedupe.engine as engine_mod

    _save(tmp_path / "a.jpg", (30, 60, 90))
    (tmp_path / "b.mp4").write_bytes(b"not-a-real-video")

    image_started = threading.Event()
    video_started = threading.Event()

    def fake_images(records, **_kwargs):
        image_started.set()
        assert video_started.wait(timeout=10), "video stage did not run concurrently"
        return []

    def fake_videos(records, **_kwargs):
        video_started.set()
        assert image_started.wait(timeout=10), "image stage did not run concurrently"
        return []

    monkeypatch.setattr(engine_mod, "find_similar_image_groups", fake_images)
    monkeypatch.setattr(engine_mod, "find_similar_video_groups", fake_videos)

    run_scan([tmp_path], exact=False, similar=True, use_cache=False)

    assert image_started.is_set()
    assert video_started.is_set()


def test_exact_stage_overlaps_similarity_hashing(tmp_path: Path, monkeypatch) -> None:
    """Exact hashing runs concurrently with similarity hashing."""
    import threading

    import dedupe.engine as engine_mod

    _save(tmp_path / "a.jpg", (30, 60, 90))

    exact_started = threading.Event()
    image_started = threading.Event()

    def fake_exact(records, **_kwargs):
        exact_started.set()
        assert image_started.wait(timeout=10), "image stage did not overlap exact"
        return []

    def fake_images(records, **_kwargs):
        image_started.set()
        assert exact_started.wait(timeout=10), "exact stage did not overlap image"
        return []

    monkeypatch.setattr(engine_mod, "find_exact_groups", fake_exact)
    monkeypatch.setattr(engine_mod, "find_similar_image_groups", fake_images)
    monkeypatch.setattr(
        engine_mod, "find_similar_video_groups", lambda records, **_kwargs: []
    )

    run_scan([tmp_path], exact=True, similar=True, use_cache=False)

    assert exact_started.is_set()
    assert image_started.is_set()


def test_person_detection_overlaps_similarity_hashing(
    tmp_path: Path, monkeypatch
) -> None:
    """Person detection runs concurrently with similarity hashing."""
    import threading

    import dedupe.engine as engine_mod

    _save(tmp_path / "a.jpg", (30, 60, 90))

    human_started = threading.Event()
    image_started = threading.Event()

    def fake_find(records, **_kwargs):
        human_started.set()
        assert image_started.wait(timeout=10), "image stage did not overlap human"
        return []

    def fake_images(records, **_kwargs):
        image_started.set()
        assert human_started.wait(timeout=10), "human stage did not overlap image"
        return []

    monkeypatch.setattr(engine_mod, "find_no_human_files", fake_find)
    monkeypatch.setattr(engine_mod, "find_similar_image_groups", fake_images)
    monkeypatch.setattr(
        engine_mod, "find_similar_video_groups", lambda records, **_kwargs: []
    )

    run_scan(
        [tmp_path],
        exact=False,
        similar=True,
        find_no_humans=True,
        use_cache=False,
    )

    assert human_started.is_set()
    assert image_started.is_set()


def test_face_counting_overlaps_person_detection(
    tmp_path: Path, monkeypatch
) -> None:
    """Face counting and person detection analyze files at the same time."""
    import threading

    import dedupe.engine as engine_mod

    _save(tmp_path / "a.jpg", (30, 60, 90))

    human_started = threading.Event()
    face_started = threading.Event()

    def fake_find(records, **_kwargs):
        human_started.set()
        assert face_started.wait(timeout=10), "face stage did not overlap human"
        return []

    def fake_count(records, **_kwargs):
        face_started.set()
        assert human_started.wait(timeout=10), "human stage did not overlap face"
        return list(records)

    monkeypatch.setattr(engine_mod, "find_no_human_files", fake_find)
    monkeypatch.setattr(engine_mod, "count_faces_in_files", fake_count)

    run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_no_humans=True,
        count_faces=True,
        use_cache=False,
    )

    assert human_started.is_set()
    assert face_started.is_set()


def test_face_stage_supersedes_in_stage_face_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    """With faces counted in the same scan, the human stage skips its own
    YuNet confirmation pass; without them it keeps it."""
    _save(tmp_path / "a.jpg", (30, 60, 90))
    captured = {}

    def fake_find(records, *, progress=None, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("dedupe.engine.find_no_human_files", fake_find)
    monkeypatch.setattr(
        "dedupe.engine.count_faces_in_files",
        lambda records, **_kwargs: list(records),
    )

    run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_no_humans=True,
        count_faces=True,
        use_cache=False,
    )
    assert captured["confirm_with_faces"] is False

    run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_no_humans=True,
        count_faces=False,
        use_cache=False,
    )
    assert captured["confirm_with_faces"] is True


def test_similar_groups_publish_after_slow_exact(tmp_path: Path, monkeypatch) -> None:
    """Even when exact is slow, similar groups are published after exact groups."""
    import time as time_mod

    import dedupe.engine as engine_mod

    data = b"identical-binary-payload-for-exact-match!!!"
    (tmp_path / "exact1.jpg").write_bytes(data)
    (tmp_path / "exact2.jpg").write_bytes(data)
    _save(tmp_path / "sim1.jpg", (30, 60, 90), quality=95)
    _save(tmp_path / "sim2.jpg", (30, 60, 90), quality=50)

    real_exact = engine_mod.find_exact_groups

    def slow_exact(records, **kwargs):
        time_mod.sleep(0.3)
        return real_exact(records, **kwargs)

    monkeypatch.setattr(engine_mod, "find_exact_groups", slow_exact)

    kinds: list[str] = []
    result = run_scan(
        [tmp_path],
        exact=True,
        similar=True,
        include_videos=False,
        use_cache=False,
        image_threshold=12,
        on_group=lambda g: kinds.append(g.kind.value),
    )

    assert GroupKind.EXACT.value in kinds
    assert GroupKind.SIMILAR.value in kinds
    assert kinds.index(GroupKind.EXACT.value) < kinds.index(GroupKind.SIMILAR.value)
    assert result.exact_groups >= 1


def test_parallel_streams_scan_folders_independently(tmp_path: Path) -> None:
    """Each folder is its own stream: identical content across folders stays separate."""
    data = b"identical-binary-payload-for-exact-match!!!"
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    # Same bytes in both folders. A combined scan would merge all four into one
    # exact group; parallel streams must keep them per-folder.
    (folder_a / "a1.jpg").write_bytes(data)
    (folder_a / "a2.jpg").write_bytes(data)
    (folder_b / "b1.jpg").write_bytes(data)
    (folder_b / "b2.jpg").write_bytes(data)

    combined = run_scan(
        [folder_a, folder_b], similar=False, include_videos=False, use_cache=False
    )
    assert len([g for g in combined.groups if g.kind == GroupKind.EXACT]) == 1

    result = run_scans_parallel(
        [folder_a, folder_b], similar=False, include_videos=False, use_cache=False
    )
    exact_groups = [g for g in result.groups if g.kind == GroupKind.EXACT]
    assert len(exact_groups) == 2
    roots = {g.root for g in exact_groups}
    assert roots == {str(folder_a.resolve()), str(folder_b.resolve())}
    # Every group's members live under its tagged root — no cross-folder mixing.
    for group in exact_groups:
        assert all(member.path.startswith(group.root) for member in group.members)
    assert len(result.files) == 4


def test_parallel_streams_report_per_stream_and_aggregate_progress(
    tmp_path: Path,
) -> None:
    for i, name in enumerate(("a", "b")):
        folder = tmp_path / name
        folder.mkdir()
        _save(folder / f"{name}.jpg", (30 + i * 40, 60, 90))

    stream_indices: set[int] = set()
    stream_roots: set[str] = set()
    aggregate_done = []

    def on_stream_progress(prog) -> None:
        assert prog.stream_index is not None
        stream_indices.add(prog.stream_index)
        stream_roots.add(prog.root)

    def on_progress(prog) -> None:
        if prog.done:
            aggregate_done.append(prog)

    run_scans_parallel(
        [tmp_path / "a", tmp_path / "b"],
        include_videos=False,
        use_cache=False,
        on_stream_progress=on_stream_progress,
        progress=on_progress,
    )

    assert stream_indices == {0, 1}
    assert len(stream_roots) == 2
    assert aggregate_done and aggregate_done[-1].done


def test_parallel_streams_skip_missing_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    _save(real / "x.jpg", (10, 20, 30))
    result = run_scans_parallel(
        [real, tmp_path / "missing"],
        include_videos=False,
        use_cache=False,
    )
    assert result.roots == [str(real.resolve())]
    assert any("does not exist" in err for err in result.errors)


def test_run_scan_surfaces_no_human_candidates(tmp_path: Path, monkeypatch) -> None:
    _save(tmp_path / "landscape.jpg", (30, 120, 60))
    captured = {}

    def fake_find(records, *, progress=None, **_kwargs):
        captured.update(_kwargs)
        for record in records:
            record.human_detection_status = "no_person_detected"
            record.human_detection_signature = human_detection_signature(
                _kwargs.get("backend", "opencv"),
                photon_model=_kwargs.get("photon_model", "test-model"),
            )
        if progress:
            progress("human-detection", len(records), len(records))
        return records

    monkeypatch.setattr("dedupe.engine.find_no_human_files", fake_find)
    result = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_no_humans=True,
        human_backend="photon",
        photon_model="test-model",
        use_cache=False,
    )

    assert result.no_human_files == 1
    no_human_group = next(group for group in result.groups if group.kind == GroupKind.NO_HUMANS)
    assert no_human_group.selected_for_removal == []
    assert captured["backend"] == "photon"
    assert captured["photon_model"] == "test-model"
    assert captured["workers"] >= 1


def test_run_scan_keeps_female_faces_out_of_non_human(tmp_path: Path, monkeypatch) -> None:
    _save(tmp_path / "landscape.jpg", (30, 120, 60))
    _save(tmp_path / "portrait.jpg", (200, 40, 80))

    def fake_find(records, *, progress=None, **_kwargs):
        signature = human_detection_signature("opencv")
        for record in records:
            record.human_detection_status = "no_person_detected"
            record.human_detection_signature = signature
        if progress:
            progress("human-detection", len(records), len(records))
        return list(records)

    def fake_count(records, *, progress=None, **_kwargs):
        for record in records:
            if record.path.endswith("portrait.jpg"):
                record.face_count = 1
                record.male_face_count = 0
                record.female_face_count = 1
                record.face_detection_signature = "face-test"
            else:
                record.face_count = 0
                record.male_face_count = 0
                record.female_face_count = 0
                record.face_detection_signature = "face-test"
        if progress:
            progress("face-detection", len(records), len(records))
        return list(records)

    monkeypatch.setattr("dedupe.engine.find_no_human_files", fake_find)
    monkeypatch.setattr("dedupe.engine.count_faces_in_files", fake_count)

    result = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_no_humans=True,
        count_faces=True,
        random_review_count=0,
        use_cache=False,
    )

    no_human = next(group for group in result.groups if group.kind == GroupKind.NO_HUMANS)
    faces = next(group for group in result.groups if group.kind == GroupKind.FACES)
    assert [Path(member.path).name for member in no_human.members] == ["landscape.jpg"]
    assert [Path(member.path).name for member in faces.members] == ["portrait.jpg"]
    assert result.no_human_files == 1
    assert result.faces_files == 1


def test_run_scan_builds_low_resolution_and_random_review_branches_without_similarity(
    tmp_path: Path,
) -> None:
    for index in range(55):
        _save(tmp_path / f"image-{index:02d}.jpg", (index, 80, 120))

    result = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_low_resolution=True,
        random_review_count=50,
        use_cache=False,
    )

    low_resolution = next(
        group for group in result.groups if group.kind == GroupKind.LOW_RESOLUTION
    )
    random_review = next(
        group for group in result.groups if group.kind == GroupKind.RANDOM_REVIEW
    )
    assert len(low_resolution.members) == 55
    assert len(random_review.members) == 50
    assert all(member.width == 48 and member.height == 48 for member in low_resolution.members)
    assert result.low_resolution_files == 55
    assert result.random_review_files == 50


def test_rescan_respects_stored_low_resolution_keep_decisions(tmp_path: Path) -> None:
    for index in range(3):
        _save(tmp_path / f"image-{index}.jpg", (index * 20, 80, 120))
    options = {
        "exact": False,
        "similar": False,
        "find_low_resolution": True,
        "random_review_count": 0,
        "use_cache": False,
    }

    first = run_scan([tmp_path], **options)
    low = next(group for group in first.groups if group.kind == GroupKind.LOW_RESOLUTION)
    kept = low.members[0]
    update_keep_decisions(keep=[kept])

    second = run_scan([tmp_path], **options)
    rescanned = next(
        group for group in second.groups if group.kind == GroupKind.LOW_RESOLUTION
    )
    assert kept.path not in [member.path for member in rescanned.members]
    assert len(rescanned.members) == len(low.members) - 1

    # Editing the file invalidates the stored decision, so it resurfaces.
    stat = os.stat(kept.path)
    os.utime(kept.path, ns=(stat.st_mtime_ns + 1_000_000_000, stat.st_mtime_ns + 1_000_000_000))
    third = run_scan([tmp_path], **options)
    resurfaced = next(
        group for group in third.groups if group.kind == GroupKind.LOW_RESOLUTION
    )
    assert kept.path in [member.path for member in resurfaced.members]


def test_run_scan_probes_video_dimensions_for_low_resolution_review(
    tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "small.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr("dedupe.engine.probe_video", lambda _path: (5.0, 640, 360))

    result = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_low_resolution=True,
        random_review_count=0,
        use_cache=False,
    )

    low_resolution = next(
        group for group in result.groups if group.kind == GroupKind.LOW_RESOLUTION
    )
    assert [(member.width, member.height) for member in low_resolution.members] == [
        (640, 360)
    ]


def test_run_scan_skips_disabled_low_resolution_media_types(
    tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "small.mp4"
    video.write_bytes(b"video")
    probe_calls = []

    def probe_video(path):
        probe_calls.append(path)
        return 5.0, 640, 360

    monkeypatch.setattr("dedupe.engine.probe_video", probe_video)

    result = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_low_resolution=True,
        low_resolution_images=True,
        low_resolution_gifs=False,
        low_resolution_videos=False,
        random_review_count=0,
        use_cache=False,
    )

    assert probe_calls == []
    assert all(group.kind != GroupKind.LOW_RESOLUTION for group in result.groups)


def test_run_scan_uses_custom_low_resolution_bound(tmp_path: Path) -> None:
    _save(tmp_path / "small.jpg", (20, 80, 120))

    below_bound = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        low_resolution_image_max_pixels=3_000,
        random_review_count=0,
        use_cache=False,
    )
    above_bound = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        low_resolution_image_max_pixels=2_000,
        random_review_count=0,
        use_cache=False,
    )

    assert below_bound.low_resolution_files == 1
    assert above_bound.low_resolution_files == 0


def test_repeated_human_scan_only_analyzes_new_files(tmp_path: Path, monkeypatch) -> None:
    _save(tmp_path / "first.jpg", (30, 120, 60))
    cache_path = tmp_path / "hashes.sqlite3"
    calls = 0

    class FakeDetector:
        backend = "opencv-test"

        def score(self, _frame):
            nonlocal calls
            calls += 1
            return 0.0

        def close(self):
            return None

    monkeypatch.setattr(
        "dedupe.human_detection.create_person_detector",
        lambda *_args, **_kwargs: FakeDetector(),
    )

    first = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_no_humans=True,
        cache_path=cache_path,
    )
    second = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_no_humans=True,
        cache_path=cache_path,
    )
    _save(tmp_path / "second.jpg", (80, 30, 120))
    third = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_no_humans=True,
        cache_path=cache_path,
    )

    assert calls == 2
    assert first.no_human_files == 1
    assert second.no_human_files == 1
    assert third.no_human_files == 2


def test_human_scan_rejects_frames_from_partial_video_decode(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("dedupe.similar_video.probe_video", lambda _path: (10.0, 640, 480))

    def failed_ffmpeg(_cmd, **_kwargs):
        (tmp_path / "frame_001.jpg").write_bytes(b"partial")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr("dedupe.similar_video.subprocess.run", failed_ffmpeg)
    frames = _extract_frames(
        tmp_path / "broken.mp4", tmp_path
    )
    assert frames == []


def test_scan_diagnostics_account_for_success_and_round_trip(tmp_path: Path) -> None:
    data = b"same-sized exact candidate"
    (tmp_path / "one.jpg").write_bytes(data)
    (tmp_path / "two.jpg").write_bytes(data)

    result = run_scan([tmp_path], similar=False, use_cache=False)

    assert result.diagnostics.total_duration_seconds > 0
    assert result.diagnostics.stages["inventory"].succeeded == 1
    exact = result.diagnostics.stages["exact"]
    assert (exact.attempted, exact.succeeded, exact.failed) == (2, 2, 0)
    restored = ScanResult.from_dict(result.to_dict())
    assert restored.diagnostics.to_dict() == result.diagnostics.to_dict()
    # Older result JSON remains loadable and receives empty diagnostics.
    legacy = result.to_dict()
    legacy.pop("diagnostics")
    assert ScanResult.from_dict(legacy).diagnostics.stages == {}


def test_scan_diagnostics_report_ffmpeg_unavailable(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "one.mp4").write_bytes(b"video one")
    (tmp_path / "two.mp4").write_bytes(b"video two")
    monkeypatch.setattr("dedupe.engine.ffmpeg_available", lambda: False)
    monkeypatch.setattr("dedupe.similar_video.ffmpeg_available", lambda: False)

    result = run_scan([tmp_path], exact=False, similar=True, use_cache=False)

    video = result.diagnostics.stages["similar_video"]
    assert video.attempted == 0
    assert video.skipped == 2
    assert any("ffmpeg" in warning for warning in video.warnings)


def test_cache_store_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    """A failed cache write must surface, not be swallowed."""
    _save(tmp_path / "one.jpg", (30, 60, 90))

    def boom(self, _records):
        raise RuntimeError("disk full")

    monkeypatch.setattr("dedupe.cache.HashCache.store_all", boom)

    result = run_scan(
        [tmp_path],
        similar=False,
        include_videos=False,
        cache_path=tmp_path / "hashes.sqlite3",
    )

    assert any("cache store failed: disk full" in error for error in result.errors)
    cache_stage = result.diagnostics.stages["cache"]
    assert cache_stage.failed == 1
    assert any("disk full" in warning for warning in cache_stage.warnings)


def test_run_scan_rejects_photos_library_root(tmp_path: Path) -> None:
    library = tmp_path / "Photos Library.photoslibrary"
    originals = library / "originals"
    originals.mkdir(parents=True)
    managed = originals / "managed.jpg"
    managed.write_bytes(b"managed-by-photos")

    result = run_scan([library], use_cache=False)
    descendant_result = run_scan([originals], use_cache=False)
    file_result = run_scan([managed], use_cache=False)

    assert result.files == []
    assert result.groups == []
    assert len(result.errors) == 1
    assert "export media from Photos.app" in result.errors[0]
    assert descendant_result.files == []
    assert "export media from Photos.app" in descendant_result.errors[0]
    assert file_result.files == []
    assert "export media from Photos.app" in file_result.errors[0]


def test_run_scan_counts_faces_and_reports_stage(tmp_path: Path) -> None:
    import pytest

    pytest.importorskip("cv2")
    _save(tmp_path / "photo.jpg", (30, 60, 90))
    (tmp_path / "clip.mp4").write_bytes(b"fake video bytes")

    result = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        count_faces=True,
        include_videos=True,
        use_cache=False,
    )

    photo = next(f for f in result.files if f.path.endswith("photo.jpg"))
    video = next(f for f in result.files if f.path.endswith("clip.mp4"))
    assert photo.face_count == 0
    assert photo.face_detection_signature
    # The fake video cannot decode, so it is attempted and fails cleanly.
    assert video.face_count is None

    stage = result.diagnostics.stages["face_detection"]
    assert stage.attempted == 2
    assert stage.succeeded == 1
    assert stage.failed == 1

    serialized = result.to_dict()
    faces = {f["path"]: f["face_count"] for f in serialized["files"]}
    assert faces[photo.path] == 0
    assert faces[video.path] is None


def test_run_scan_publishes_faces_review_group(tmp_path: Path, monkeypatch) -> None:
    _save(tmp_path / "solo.jpg", (30, 60, 90))
    _save(tmp_path / "group-shot.jpg", (60, 90, 30))
    (tmp_path / "clip.mp4").write_bytes(b"fake video bytes")

    def fake_count(records, *, progress=None, **_kwargs):
        for record in records:
            record.face_count = 2 if record.path.endswith("group-shot.jpg") else 1
            record.face_detection_signature = "test-signature"
        return records

    monkeypatch.setattr("dedupe.engine.count_faces_in_files", fake_count)
    result = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        count_faces=True,
        include_videos=True,
        use_cache=False,
    )

    faces_groups = [g for g in result.groups if g.kind == GroupKind.FACES]
    assert len(faces_groups) == 1
    # Busiest shot first; videos with trusted counts join the review flow.
    assert [m.face_count for m in faces_groups[0].members] == [2, 1, 1]
    assert faces_groups[0].members[0].path.endswith("group-shot.jpg")
    assert faces_groups[0].selected_for_removal == []
    assert result.faces_files == 3
    assert result.to_dict()["faces_files"] == 3


def test_run_scan_without_count_faces_publishes_no_faces_group(
    tmp_path: Path,
) -> None:
    import pytest

    pytest.importorskip("cv2")
    _save(tmp_path / "photo.jpg", (30, 60, 90))

    result = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        count_faces=False,
        use_cache=False,
    )

    assert result.faces_files == 0
    assert all(g.kind != GroupKind.FACES for g in result.groups)


def test_parallel_streams_floor_per_stream_workers(tmp_path: Path, monkeypatch) -> None:
    """Multi-folder scans must not double-divide CPU stages down to 1 worker."""
    import dedupe.engine as engine_module

    seen_workers: list[int] = []

    def fake_run_scan(paths, **kwargs):
        seen_workers.append(kwargs["workers"])
        return ScanResult(
            roots=[str(path) for path in paths], files=[], groups=[]
        )

    monkeypatch.setattr(engine_module, "run_scan", fake_run_scan)
    roots = []
    for name in ("a", "b", "c"):
        root = tmp_path / name
        root.mkdir()
        roots.append(root)

    run_scans_parallel(roots, workers=8, use_cache=False)
    assert seen_workers == [3, 3, 3]  # ceil(8/3), not floor-to-2 then split again

    seen_workers.clear()
    run_scans_parallel(roots, workers=1, use_cache=False)
    assert seen_workers == [2, 2, 2]  # floor of 2 keeps CPU stages concurrent
