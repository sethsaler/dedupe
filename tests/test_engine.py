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


@pytest.mark.parametrize("across_folders", [False, True])
@pytest.mark.parametrize("workers", [1, 2])
def test_exact_and_similar_membership_is_stable_across_cached_scans(
    tmp_path, across_folders, workers
):
    media = tmp_path / "media"
    media.mkdir()
    exports = tmp_path / "exports" if across_folders else media
    exports.mkdir(exist_ok=True)
    original = media / "original.jpg"
    copy, download = (exports / name for name in ("copy.jpg", "small.jpg"))
    # Explicit file roots and overlapping directories must not duplicate records.
    roots = [media, exports, original] if across_folders else [media]
    with Image.open(Path(__file__).parent / "fixtures" / "astronaut.png") as image:
        image.save(original, quality=95)
        image.resize((128, 128)).save(download, quality=75)
    copy.write_bytes(original.read_bytes())
    # An unrelated pair with identical grayscale hashes must not leak into results.
    Image.new("RGB", (256, 256), (200, 40, 40)).save(media / "red.png")
    Image.new("RGB", (256, 256), (40, 121, 40)).save(media / "green.png")

    for cached in (False, True):
        streamed = []
        result = run_scan(
            roots, cache_path=tmp_path / "cache.sqlite3", workers=workers,
            find_low_resolution=False, random_review_count=0, on_group=streamed.append,
        )
        assert len(result.groups) == 2
        assert len(result.files) == len({record.path for record in result.files}) == 5
        assert {g.kind: {m.path for m in g.members} for g in result.groups} == {
            GroupKind.EXACT: {str(original), str(copy)},
            GroupKind.SIMILAR: {str(original), str(copy), str(download)},
        }
        assert [g.kind for g in streamed] == [GroupKind.EXACT, GroupKind.SIMILAR]
        assert all(g.suggested_keep != str(download) for g in result.groups)
        assert result.diagnostics.cache_hits == (5 if cached else 0)


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


def test_parallel_streams_cancel_raises_interrupted_not_unbound(
    tmp_path: Path,
) -> None:
    """Regression: a cancelled stream future left ``sub`` unbound, crashing the
    merge loop with UnboundLocalError instead of a clean InterruptedError."""
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    for index in range(20):
        _save(folder_a / f"a{index:02}.jpg", (index * 11 % 255, 40, 90), quality=90)
        _save(folder_b / f"b{index:02}.jpg", (index * 7 % 255, 90, 40), quality=90)
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 5

    with pytest.raises(InterruptedError, match="scan cancelled"):
        run_scans_parallel(
            [folder_a, folder_b],
            include_videos=False,
            use_cache=False,
            cancelled=cancelled,
        )


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


@pytest.mark.parametrize("nested_first", [False, True])
def test_parallel_overlapping_roots_do_not_repeat_files_or_groups(tmp_path, nested_first):
    nested = tmp_path / "nested"
    nested.mkdir()
    _save(nested / "original.jpg", (30, 60, 90))
    (nested / "copy.jpg").write_bytes((nested / "original.jpg").read_bytes())
    roots = [nested, tmp_path] if nested_first else [tmp_path, nested]
    streamed = []

    result = run_scans_parallel(
        roots, use_cache=False, similar=False, find_low_resolution=True,
        random_review_count=50, workers=2, on_group=streamed.append,
    )

    assert len(result.files) == 2
    assert result.exact_groups == 1
    assert result.low_resolution_files == result.random_review_files == 2
    assert len({group.id for group in streamed}) == len(streamed)
    assert len({group.id for group in result.groups}) == len(result.groups)
    assert {g.id: g.root for g in streamed} == {g.id: g.root for g in result.groups}


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


@pytest.mark.parametrize("chosen", [True, False])
def test_image_stage_cap_applies_to_the_automatic_budget_only(
    tmp_path: Path, monkeypatch, chosen: bool
) -> None:
    """A chosen worker count reaches image matching whole; auto stays capped."""
    import dedupe.engine as engine_module

    _save(tmp_path / "a.jpg", (30, 120, 60))
    _save(tmp_path / "b.jpg", (200, 40, 80))
    seen: list[int] = []

    def fake_images(records, *, workers=None, **_kwargs):
        seen.append(workers)
        return []

    monkeypatch.setattr(engine_module, "find_similar_image_groups", fake_images)
    monkeypatch.setattr(engine_module, "resolve_workers", lambda workers=None, **_k: 12)

    run_scan(
        [tmp_path], exact=False, similar=True, use_cache=False,
        workers=12 if chosen else None,
    )

    assert seen == [12 if chosen else engine_module.DEFAULT_IMAGE_WORKERS_CAP]


def test_parallel_streams_say_whether_the_worker_count_was_chosen(
    tmp_path: Path, monkeypatch
) -> None:
    """A stream gets its share as a number, so the origin travels beside it."""
    import dedupe.engine as engine_module

    seen: list[bool] = []

    def fake_run_scan(paths, **kwargs):
        seen.append(kwargs["_workers_chosen"])
        return ScanResult(roots=[str(path) for path in paths], files=[], groups=[])

    monkeypatch.setattr(engine_module, "run_scan", fake_run_scan)
    roots = []
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        roots.append(root)

    run_scans_parallel(roots, workers=8, use_cache=False)
    run_scans_parallel(roots, workers=None, use_cache=False)

    assert seen == [True, True, False, False]


def test_combined_progress_reaches_its_total_and_never_goes_back(tmp_path: Path) -> None:
    """Every stage counts as complete when it ends, and none starts over.

    Exact hashing only reports the files that share a size, and image
    comparison follows image hashing, so the combined count once stopped at
    half of its total for the rest of the scan.
    """
    for index in range(6):
        _save(tmp_path / f"{index}.jpg", (40 * index, 255 - 40 * index, 20 * index))

    def scan() -> list[tuple[int, int]]:
        reports: list[tuple[int, int]] = []

        def progress(prog) -> None:
            if prog.phase == "processing":
                reports.append((prog.files_processed, prog.files_found))

        run_scan(
            [tmp_path],
            exact=True,
            similar=True,
            find_black_borders=True,
            random_review_count=0,
            cache_path=tmp_path / "cache.sqlite3",
            progress=progress,
        )
        return reports

    first = scan()
    _save(tmp_path / "new.jpg", (10, 200, 120))
    # The second scan works on one new file; the cache serves the rest.
    for reports in (first, scan()):
        assert reports
        counts = [processed for processed, _total in reports]
        assert counts == sorted(counts)
        assert reports[-1][0] == reports[-1][1]


def test_missing_protected_folder_reports_root_error_gracefully(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    _save(media / "a.jpg", (10, 40, 90))
    missing = tmp_path / "missing-protected"

    result = run_scan(
        [media],
        protected_folders=[str(missing)],
        exact=False,
        similar=False,
        use_cache=False,
    )
    assert result.protected_folders == []
    assert any("protected folder does not exist" in err for err in result.errors)


def test_protected_folder_walked_first_and_hardlinks_inherit_protection(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    protected = tmp_path / "library"
    inbox.mkdir()
    protected.mkdir()
    master = protected / "orig.jpg"
    _save(master, (20, 120, 200))
    link = inbox / "hardlink.jpg"
    os.link(master, link)

    result = run_scan(
        [inbox],
        protected_folders=[str(protected)],
        exact=True,
        similar=False,
        find_low_resolution=False,
        random_review_count=0,
        use_cache=False,
    )
    # Every hardlink sharing the protected file's inode stays marked protected,
    # so exact-group deduplication over the pair sees only protected links.
    assert all(rec.protected for rec in result.files)
    assert result.groups == []


def test_crop_and_dedupe_matches_uncropped_screenshot_on_first_scan(
    tmp_path: Path, monkeypatch
) -> None:
    import dedupe.engine as engine_mod

    _save(tmp_path / "orig.jpg", (50, 90, 140))
    _save(tmp_path / "shot.png", (210, 20, 20))
    order: list[str] = []

    def fake_borders(records, **_kwargs):
        order.append("borders")
        for rec in records:
            if rec.path.endswith("shot.png"):
                rec.border_crop = "c:0,10,48,28"
        return []

    seen_crop_in_image_stage: dict[str, str | None] = {}

    def fake_images(records, **_kwargs):
        order.append("image")
        for rec in records:
            seen_crop_in_image_stage[Path(rec.path).name] = rec.border_crop
        return []

    monkeypatch.setattr(engine_mod, "analyze_borders", fake_borders)
    monkeypatch.setattr(engine_mod, "find_similar_image_groups", fake_images)

    run_scan(
        [tmp_path],
        exact=False,
        similar=True,
        find_black_borders=True,
        find_low_resolution=False,
        random_review_count=0,
        use_cache=False,
    )
    assert order == ["borders", "image"]
    assert seen_crop_in_image_stage["shot.png"] == "c:0,10,48,28"


def test_semantic_excluded_pairs_surfaces_original_resized_and_cropped(
    tmp_path: Path, monkeypatch
) -> None:
    import dedupe.engine as engine_mod

    for name in ("orig.jpg", "resized.jpg", "cropped.jpg", "prot_a.jpg", "prot_b.jpg"):
        _save(tmp_path / name, (20, 100, 180))

    p = {name: str((tmp_path / name).resolve()) for name in (
        "orig.jpg", "resized.jpg", "cropped.jpg", "prot_a.jpg", "prot_b.jpg"
    )}

    monkeypatch.setattr(engine_mod, "semantic_available", lambda: True)
    monkeypatch.setattr(
        engine_mod,
        "embed_records",
        lambda recs, **_k: ({r.path: b"\x00\x00\x80?" for r in recs}, []),
    )
    monkeypatch.setattr(
        engine_mod,
        "find_similar_image_groups",
        lambda recs, **_k: [
            [
                next(r for r in recs if r.path == p["orig.jpg"]),
                next(r for r in recs if r.path == p["resized.jpg"]),
            ]
        ],
    )

    captured_kwargs: dict[str, object] = {}

    def fake_group_semantic(records, vectors, **kwargs):
        captured_kwargs.update(kwargs)
        by_path = {r.path: r for r in records}
        dups = [
            (
                [by_path[p["orig.jpg"]], by_path[p["cropped.jpg"]]],
                {p["cropped.jpg"]: 0.95},
            ),
            (
                [by_path[p["prot_a.jpg"]], by_path[p["prot_b.jpg"]]],
                {p["prot_b.jpg"]: 0.95},
            ),
        ]
        return [], dups

    monkeypatch.setattr(engine_mod, "group_semantic_vectors", fake_group_semantic)

    prot_dir = tmp_path / "prot"
    prot_dir.mkdir()
    (tmp_path / "prot_a.jpg").rename(prot_dir / "prot_a.jpg")
    (tmp_path / "prot_b.jpg").rename(prot_dir / "prot_b.jpg")
    p["prot_a.jpg"] = str((prot_dir / "prot_a.jpg").resolve())
    p["prot_b.jpg"] = str((prot_dir / "prot_b.jpg").resolve())

    result = run_scan(
        [tmp_path],
        protected_folders=[str(prot_dir)],
        exact=False,
        similar=True,
        semantic=True,
        find_low_resolution=False,
        random_review_count=0,
        use_cache=False,
    )
    assert "exclude_from_duplicates" not in captured_kwargs
    assert captured_kwargs["excluded_pairs"] == {
        tuple(sorted((p["orig.jpg"], p["resized.jpg"])))
    }
    sim_groups = [g for g in result.groups if g.kind == GroupKind.SIMILAR]
    assert len(sim_groups) == 2
    assert {g.match_basis for g in sim_groups} == {None, "semantic"}
    assert all(not all(m.protected for m in g.members) for g in sim_groups)


def test_video_versions_without_similar_does_not_run_image_or_std_video_stages(
    tmp_path: Path, monkeypatch
) -> None:
    import dedupe.engine as engine_mod

    _save(tmp_path / "photo.jpg", (40, 80, 120))
    (tmp_path / "clip.mp4").write_bytes(b"video")

    calls: list[str] = []
    monkeypatch.setattr(
        engine_mod, "find_similar_image_groups", lambda *_a, **_k: calls.append("image") or []
    )
    monkeypatch.setattr(
        engine_mod, "find_similar_video_groups", lambda *_a, **_k: calls.append("video") or []
    )
    monkeypatch.setattr(
        engine_mod, "find_video_version_matches", lambda *_a, **_k: calls.append("versions") or []
    )

    run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        video_versions=True,
        find_low_resolution=False,
        random_review_count=0,
        use_cache=False,
    )
    assert calls == ["versions"]


def test_incremental_new_only_short_circuits_and_isolates_arrivals(tmp_path: Path) -> None:
    from dedupe.jobs import file_identity

    lib = tmp_path / "protected"
    inbox = tmp_path / "inbox"
    lib.mkdir()
    inbox.mkdir()
    _save(inbox / "old_small.jpg", (20, 40, 60))
    _save(lib / "old_master.jpg", (10, 90, 170))
    cache_path = tmp_path / "cache.sqlite3"

    first = run_scan(
        [inbox],
        protected_folders=[str(lib)],
        cache_path=cache_path,
        find_low_resolution=True,
        random_review_count=50,
    )
    baseline = {r.path: file_identity(r) for r in first.files}

    # Zero arrivals must short-circuit completely with no cache hydration or groups.
    empty = run_scan(
        [inbox],
        protected_folders=[str(lib)],
        cache_path=cache_path,
        baseline=baseline,
        new_only=True,
    )
    assert empty.groups == []
    assert empty.diagnostics.cache_hits == 0

    # Add 1 arrival matching old_master exact bytes + 1 protected arrival.
    arrival = inbox / "arrival_dup.jpg"
    arrival.write_bytes((lib / "old_master.jpg").read_bytes())
    _save(lib / "prot_arrival.jpg", (200, 150, 100))

    inc = run_scan(
        [inbox],
        protected_folders=[str(lib)],
        cache_path=cache_path,
        baseline=baseline,
        new_only=True,
        find_low_resolution=True,
        random_review_count=50,
    )
    exact_groups = [g for g in inc.groups if g.kind == GroupKind.EXACT]
    assert len(exact_groups) == 1
    assert {m.path for m in exact_groups[0].members} == {
        str((lib / "old_master.jpg").resolve()),
        str(arrival.resolve()),
    }
    low_res = next(g for g in inc.groups if g.kind == GroupKind.LOW_RESOLUTION)
    rand_rev = next(g for g in inc.groups if g.kind == GroupKind.RANDOM_REVIEW)
    assert [m.path for m in low_res.members] == [str(arrival.resolve())]
    assert [m.path for m in rand_rev.members] == [str(arrival.resolve())]


def test_raw_thumbnail_excluded_from_low_resolution_and_pixel_exact_tagged(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "photo.cr3"
    raw.write_bytes(b"CR3-RAW-CONTAINER")

    img = Image.new("RGB", (120, 120), (30, 140, 210))
    for x in range(10, 80, 2):
        for y in range(10, 80, 3):
            img.putpixel((x, y), (220, 40, 40))
    p_a = tmp_path / "a.jpg"
    p_b = tmp_path / "b.jpg"
    exif_a = img.getexif()
    exif_a[0x010E] = "First caption"
    img.save(p_a, format="JPEG", quality=95, exif=exif_a.tobytes())
    raw_bytes = p_a.read_bytes()
    dqt_pos = raw_bytes.find(b"\xff\xdb")
    p_b.write_bytes(b"\xff\xd8\xff\xfe\x00\x06INFO" + raw_bytes[dqt_pos:])

    result = run_scan(
        [tmp_path],
        exact=True,
        similar=True,
        find_low_resolution=True,
        low_resolution_max_pixels=500_000,
        random_review_count=0,
        use_cache=False,
    )
    low_res_paths = {
        m.path for g in result.groups if g.kind == GroupKind.LOW_RESOLUTION for m in g.members
    }
    assert str(raw.resolve()) not in low_res_paths
    assert result.diagnostics.stages["low_resolution"].failed == 0

    sim_groups = [g for g in result.groups if g.kind == GroupKind.SIMILAR]
    assert len(sim_groups) == 1
    assert sim_groups[0].match_basis == "pixel_exact"
    assert sim_groups[0].match_explanations


def test_live_photo_pair_annotated_in_scan_diagnostics_and_groups(
    tmp_path: Path, monkeypatch
) -> None:
    _save(tmp_path / "IMG_1234.jpg", (50, 100, 150))
    (tmp_path / "IMG_1234.mov").write_bytes(b"short-live-photo-mov")
    monkeypatch.setattr("dedupe.engine.probe_video", lambda _p: (2.2, 1080, 1920))

    result = run_scan(
        [tmp_path],
        exact=False,
        similar=False,
        find_low_resolution=True,
        low_resolution_max_pixels=50_000,
        random_review_count=0,
        use_cache=False,
    )
    inv_warnings = result.diagnostics.stages["inventory"].warnings
    assert any("Live Photo pair detected" in w for w in inv_warnings)
    low_res = next(g for g in result.groups if g.kind == GroupKind.LOW_RESOLUTION)
    assert str((tmp_path / "IMG_1234.jpg").resolve()) in low_res.match_explanations


def test_parallel_streams_reject_photos_library_aggregates_diags_dedups_hardlinks_and_cancel(
    tmp_path: Path,
) -> None:
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    photos_lib = tmp_path / "Photos Library.photoslibrary"
    a_dir.mkdir()
    b_dir.mkdir()
    photos_lib.mkdir()
    master = a_dir / "master.jpg"
    _save(master, (30, 100, 190))
    os.link(master, b_dir / "linked.jpg")
    _save(b_dir / "other.jpg", (200, 40, 40))

    result = run_scans_parallel(
        [a_dir, b_dir, photos_lib],
        exact=False,
        similar=False,
        find_low_resolution=False,
        random_review_count=0,
        use_cache=False,
    )
    assert any("Photos libraries cannot be scanned directly" in err for err in result.errors)
    # Cross-root hardlink (master vs linked) is deduplicated in merged result.files.
    assert len(result.files) == 2
    assert result.diagnostics is not None
    assert result.diagnostics.stages["inventory"].failed == 1

    # Cancel in run_scans_parallel must NOT emit a final done=True progress message.
    events = []
    with pytest.raises(InterruptedError, match="scan cancelled"):
        run_scans_parallel(
            [a_dir, b_dir],
            include_videos=False,
            use_cache=False,
            cancelled=lambda: True,
            progress=events.append,
        )
    assert all(not ev.done for ev in events)


def test_cancel_waits_for_stage_pool_to_drain_before_cache_store(
    tmp_path: Path, monkeypatch
) -> None:
    import threading

    import dedupe.engine as engine_mod
    from dedupe.cache import HashCache

    _save(tmp_path / "a.jpg", (20, 40, 60))
    _save(tmp_path / "b.jpg", (80, 100, 120))
    worker_active = threading.Event()
    worker_finished = threading.Event()
    saw_active_during_store: list[bool] = []

    def slow_images(*_a, **_k):
        worker_active.set()
        threading.Event().wait(0.08)
        worker_finished.set()
        return []

    def cancel_exact(*_a, **_k):
        assert worker_active.wait(2)
        raise InterruptedError("scan cancelled")

    real_store_all = HashCache.store_all

    def guarded_store_all(self, records):
        saw_active_during_store.append(not worker_finished.is_set())
        return real_store_all(self, records)

    monkeypatch.setattr(engine_mod, "find_similar_image_groups", slow_images)
    monkeypatch.setattr(engine_mod, "find_exact_groups", cancel_exact)
    monkeypatch.setattr(HashCache, "store_all", guarded_store_all)

    with pytest.raises(InterruptedError, match="scan cancelled"):
        run_scan(
            [tmp_path],
            cache_path=tmp_path / "hashes.sqlite3",
            find_low_resolution=False,
            random_review_count=0,
        )
    assert saw_active_during_store == [False]
