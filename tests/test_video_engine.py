"""Video-version scan integration and manual-review boundaries."""

from pathlib import Path

import pytest

from dedupe.engine import run_scan
from dedupe.grouping import apply_smart_select_all
from dedupe.models import GroupKind, ReviewGroup, SmartRule


def _scan(tmp_path, **kwargs):
    return run_scan(
        [tmp_path / "media"], cache_path=tmp_path / "hashes.sqlite3",
        find_low_resolution=False, random_review_count=0, **kwargs,
    )


@pytest.fixture
def video_pair(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    (media / "full.mp4").write_bytes(b"full recording")
    (media / "clip.mp4").write_bytes(b"short clip")

    def records_ready(records):
        for record in records:
            record.duration = 20 if Path(record.path).name == "full.mp4" else 5
            # Larger short export must not displace the complete recording.
            record.width = record.height = 100 if record.duration == 20 else 200
            record.video_fingerprint = "v3:" + ",".join(["0123456789abcdef"] * 8)
        return records

    def standard(records, **kwargs):
        return [records_ready(records)]

    def versions(records, **kwargs):
        records_ready(records)
        return [(records, {
            "relation": "excerpt", "audio": "same", "manual_review": True,
            "ranges": {r.path: [0, 5] for r in records},
        })]

    monkeypatch.setattr("dedupe.engine.find_similar_video_groups", standard)
    monkeypatch.setattr("dedupe.engine.find_video_version_matches", versions)
    return media


def test_deep_video_groups_remain_manual_and_prefer_complete_recording(tmp_path, video_pair):
    result = _scan(tmp_path, video_versions=True, exact=False)
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.match_basis == "video_version"
    assert group.suggested_keep == str(video_pair / "full.mp4")
    assert group.selected_for_removal == []
    apply_smart_select_all(result.groups, SmartRule.AUTOMATIC)
    assert group.selected_for_removal == []
    restored = ReviewGroup.from_dict(group.to_dict())
    assert restored.match_details == group.match_details


def test_protected_copy_overrides_complete_recording_preference(tmp_path, video_pair):
    protected = str(video_pair / "clip.mp4")
    result = _scan(tmp_path, video_versions=True, exact=False, protected_folders=[protected])
    group = result.groups[0]
    assert group.suggested_keep == protected
    assert next(m for m in group.members if m.path == protected).protected
    assert group.selected_for_removal == []


def test_entirely_protected_version_pair_is_not_a_cleanup_group(tmp_path, video_pair):
    result = _scan(tmp_path, video_versions=True, exact=False, protected_folders=[str(video_pair)])
    assert result.groups == []


def test_exact_copies_do_not_duplicate_video_version_groups(tmp_path, video_pair):
    (video_pair / "clip.mp4").write_bytes((video_pair / "full.mp4").read_bytes())
    result = _scan(tmp_path, video_versions=True)
    assert len(result.groups) == 1
    assert result.groups[0].kind == GroupKind.EXACT


def test_standard_pass_keeps_existing_selection_without_deep_option(tmp_path, video_pair):
    result = _scan(tmp_path, exact=False)
    assert len(result.groups) == 1
    assert result.groups[0].match_basis is None
    assert len(result.groups[0].selected_for_removal) == 1


def test_audio_different_standard_match_is_manual_in_deep_scan(tmp_path, video_pair, monkeypatch):
    monkeypatch.setattr("dedupe.engine.find_video_version_matches", lambda *a, **k: [])
    monkeypatch.setattr("dedupe.engine.compare_video_audio", lambda *a, **k: "different")
    result = _scan(tmp_path, video_versions=True, exact=False)
    group = result.groups[0]
    assert group.match_basis == "video_version"
    assert group.match_details["audio"] == "different"
    assert group.selected_for_removal == []


def test_media_details_video_metadata_and_stat_cache(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from dedupe import media_details

    media_details.comparison_details.cache_clear()
    clip = tmp_path / "sample.mp4"
    clip.write_bytes(b"video bytes v1")

    calls = 0

    def fake_ffprobe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 3840,
                        "height": 2160,
                        "bit_rate": "12500000",
                        "avg_frame_rate": "24000/1001",
                        "r_frame_rate": "24/1",
                        "pix_fmt": "yuv420p10le",
                        "color_transfer": "smpte2084",
                        "color_primaries": "bt2020",
                        "side_data_list": [{"rotation": -90}],
                    },
                    {"codec_type": "audio", "codec_name": "aac", "channels": 2},
                ],
                "format": {
                    "duration": "12.5",
                    "bit_rate": "13000000",
                    "tags": {"creation_time": "2025-05-01T12:00:00Z"},
                },
            }),
        )

    monkeypatch.setattr(media_details.shutil, "which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr(media_details.subprocess, "run", fake_ffprobe)

    first = media_details.comparison_details(clip)
    assert first["duration"] == "12.5"
    assert first["audio"] == [{"codec_type": "audio", "codec_name": "aac", "channels": 2}]
    assert first["video_codec"] == "hevc"
    assert first["bit_rate"] == 12500000
    assert first["fps"] == pytest.approx(23.976, abs=0.001)
    assert first["pix_fmt"] == "yuv420p10le"
    assert first["hdr"] is True
    assert first["rotation"] == 270
    assert first["creation_time"] == "2025-05-01T12:00:00Z"

    # Mutating returned audio list must not corrupt the cached copy; stat hit avoids ffprobe.
    first["audio"].append({"codec_type": "audio", "codec_name": "fake"})
    second = media_details.comparison_details(clip)
    assert calls == 1
    assert len(second["audio"]) == 1

    # Modifying file size/mtime invalidates cache.
    clip.write_bytes(b"video bytes v2 longer")
    media_details.comparison_details(clip)
    assert calls == 2

    # Missing ffprobe fails explicitly instead of raising FileNotFoundError on subprocess.
    monkeypatch.setattr(media_details.shutil, "which", lambda _name: None)
    clip.write_bytes(b"video bytes v3 even longer")
    with pytest.raises(RuntimeError, match="ffprobe"):
        media_details.comparison_details(clip)


def test_media_details_guards_other_type_and_threadsafe_heif(tmp_path, monkeypatch):
    import sys
    import threading
    from types import SimpleNamespace

    from PIL import Image

    from dedupe import media_details

    media_details.comparison_details.cache_clear()
    note = tmp_path / "notes.txt"
    note.write_text("hello")

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("PIL.Image.open must not be called for MediaType.OTHER")

    monkeypatch.setattr(Image, "open", forbidden_open)
    other = media_details.comparison_details(note)
    assert other == {"format": None, "capture_date": None, "camera": ""}

    # Concurrent ensure_heif_registered calls invoke register_heif_opener once.
    monkeypatch.setattr(media_details, "_HEIF_REGISTERED", False)
    calls = 0
    call_lock = threading.Lock()

    def fake_register():
        nonlocal calls
        with call_lock:
            calls += 1

    monkeypatch.setitem(sys.modules, "pillow_heif", SimpleNamespace(register_heif_opener=fake_register))
    barrier = threading.Barrier(8)
    threads = [
        threading.Thread(target=lambda: (barrier.wait(), media_details.ensure_heif_registered()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert calls == 1


def test_video_versions_has_separate_progress_budget_and_audio_progress(
    tmp_path: Path, monkeypatch
) -> None:
    """Standard video clustering must not peg progress at 100% before excerpts run."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.mp4").write_bytes(b"video-a")
    (media / "b.mp4").write_bytes(b"video-b")
    (media / "c.mp4").write_bytes(b"video-c")

    snapshots: list[tuple[str, int, int, str, bool]] = []
    progress_before_versions: list[tuple[int, int]] = []

    def on_progress(prog) -> None:
        snapshots.append(
            (prog.phase, prog.files_processed, prog.files_found, prog.message, prog.done)
        )

    def fake_standard(records, *, progress=None, **_kwargs):
        for r in records:
            r.duration = 20.0
            r.width = r.height = 100
        if progress:
            progress("video-hash", len(records), len(records))
            progress("video-cluster", len(records), len(records), "")
        return [records[:2]]

    def fake_versions(records, *, progress=None, **_kwargs):
        # Capture latest processing snapshot right as excerpts start.
        proc = [(d, t) for ph, d, t, _m, _done in snapshots if ph == "processing"]
        if proc:
            progress_before_versions.append(proc[-1])
        if progress:
            progress("video-versions", 1, len(records), "Finding video excerpts")
            progress("video-versions-verify", 1, 1, "Verifying video excerpts")
        return []

    monkeypatch.setattr("dedupe.engine.PROGRESS_CALLBACK_MIN_INTERVAL", 0.0)
    monkeypatch.setattr("dedupe.engine.find_similar_video_groups", fake_standard)
    monkeypatch.setattr("dedupe.engine.find_video_version_matches", fake_versions)
    monkeypatch.setattr("dedupe.engine.compare_video_audio", lambda *_a, **_k: "same")
    monkeypatch.setattr("dedupe.engine._verify_video_offset", lambda *_a, **_k: None)

    run_scan(
        [media],
        exact=False,
        similar=True,
        video_versions=True,
        find_low_resolution=False,
        random_review_count=0,
        use_cache=False,
        progress=on_progress,
    )

    assert progress_before_versions
    done_before, total_before = progress_before_versions[0]
    assert done_before < total_before, "progress pegged at 100% before video_versions started"
    assert any("Comparing video audio 1/1" in msg for _ph, _d, _t, msg, _done in snapshots)
    assert any(ph == "finalizing" for ph, _d, _t, _m, _done in snapshots)


def test_run_scan_on_groups_batching_and_emit_done_false(tmp_path: Path, video_pair) -> None:
    batches: list[list] = []
    single_calls: list = []
    progress_events: list = []

    result = run_scan(
        [video_pair],
        exact=False,
        similar=True,
        video_versions=True,
        find_low_resolution=False,
        random_review_count=0,
        use_cache=False,
        on_group=single_calls.append,
        on_groups=batches.append,
        emit_done=False,
        progress=progress_events.append,
    )

    assert len(result.groups) == 1
    assert len(batches) == 1
    assert len(batches[0]) == 1
    assert single_calls == []
    assert progress_events
    assert progress_events[-1].phase == "finalizing"
    assert progress_events[-1].done is False
    assert all(not ev.done for ev in progress_events)


def test_cpu_stage_count_and_late_video_versions_worker_budget(
    tmp_path: Path, monkeypatch
) -> None:
    import dedupe.engine as engine_mod

    # Borders blocks image and video, so they do not double-count.
    assert (
        engine_mod._concurrent_cpu_stage_count(
            exact=True,
            borders=True,
            image=True,
            video=False,
            semantic=False,
            human=False,
            faces=False,
        )
        == 2
    )
    assert (
        engine_mod._concurrent_cpu_stage_count(
            exact=False,
            borders=True,
            image=True,
            video=True,
            semantic=False,
            human=False,
            faces=False,
        )
        == 2
    )

    media = tmp_path / "media"
    media.mkdir()
    (media / "a.mp4").write_bytes(b"video-a")
    (media / "b.mp4").write_bytes(b"video-b")
    seen_version_workers: list[int] = []

    def fake_versions(records, *, workers=None, **_kwargs):
        seen_version_workers.append(workers)
        return []

    monkeypatch.setattr(engine_mod, "resolve_workers", lambda workers=None, **_k: 8)
    monkeypatch.setattr(engine_mod, "find_similar_video_groups", lambda *_a, **_k: [])
    monkeypatch.setattr(engine_mod, "find_video_version_matches", fake_versions)

    run_scan(
        [media],
        exact=True,
        similar=True,
        video_versions=True,
        find_low_resolution=False,
        random_review_count=0,
        use_cache=False,
    )
    # Exact is already done and no other CPU stages remain -> full n_workers (8).
    assert seen_version_workers == [8]


def test_video_versions_index_and_verify_advance_distinct_progress_bands(
    tmp_path: Path, monkeypatch
) -> None:
    """Indexing video excerpts must not pin progress at 90% before verification runs."""
    media = tmp_path / "media"
    media.mkdir()
    for i in range(20):
        (media / f"v{i:02d}.mp4").write_bytes(f"video-{i}".encode())

    seen: list[tuple[str, int, int]] = []

    def on_progress(prog) -> None:
        if prog.phase == "processing":
            seen.append((prog.message, prog.files_processed, prog.files_found))

    def fake_versions(records, *, progress=None, **_kwargs):
        if progress:
            progress("video-versions", len(records), len(records), "Finding video excerpts")
            progress("video-versions-index", len(records), len(records), "Indexing video excerpts")
            progress("video-versions-verify", 1, 2, "Checking video excerpts")
            progress("video-versions-verify", 2, 2, "Checking video excerpts")
        return []

    monkeypatch.setattr("dedupe.engine.PROGRESS_CALLBACK_MIN_INTERVAL", 0.0)
    monkeypatch.setattr("dedupe.engine.find_video_version_matches", fake_versions)

    run_scan(
        [media],
        exact=False,
        similar=False,
        video_versions=True,
        find_low_resolution=False,
        random_review_count=0,
        use_cache=False,
        progress=on_progress,
    )

    after_index = [d for msg, d, _t in seen if "Indexing video excerpts" in msg]
    after_verify = [d for msg, d, _t in seen if "Checking video excerpts" in msg]
    assert after_index and after_verify
    assert after_index[-1] < after_verify[-1]


def test_run_scans_parallel_emit_done_false_never_emits_done(tmp_path: Path) -> None:
    from dedupe.engine import run_scans_parallel

    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    (folder_a / "a.jpg").write_bytes(b"img-a")
    (folder_b / "b.jpg").write_bytes(b"img-b")

    events: list = []
    run_scans_parallel(
        [folder_a, folder_b],
        exact=False,
        similar=False,
        find_low_resolution=False,
        random_review_count=0,
        use_cache=False,
        emit_done=False,
        progress=events.append,
    )
    assert events
    assert all(not ev.done for ev in events)
    assert all(ev.phase != "done" for ev in events)
    assert events[-1].phase == "finalizing"

    empty_events: list = []
    run_scans_parallel(
        [tmp_path / "missing"],
        use_cache=False,
        emit_done=False,
        progress=empty_events.append,
    )
    assert empty_events
    assert all(not ev.done for ev in empty_events)
    assert empty_events[-1].phase == "finalizing"
