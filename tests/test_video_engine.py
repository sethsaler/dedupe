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
