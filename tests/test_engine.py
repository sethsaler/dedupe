"""End-to-end engine scan with image fixtures."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from dedupe.engine import run_scan, run_scans_parallel
from dedupe.human_detection import human_detection_signature
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
    assert result.groups[0].kind == GroupKind.NO_HUMANS
    assert result.groups[0].selected_for_removal == []
    assert captured["backend"] == "photon"
    assert captured["photon_model"] == "test-model"
    assert captured["workers"] >= 1


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
