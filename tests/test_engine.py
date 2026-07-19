"""End-to-end engine scan with image fixtures."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from dedupe.engine import run_scan
from dedupe.human_detection import human_detection_signature
from dedupe.models import GroupKind
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
