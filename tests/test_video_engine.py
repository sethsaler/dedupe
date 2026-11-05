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
