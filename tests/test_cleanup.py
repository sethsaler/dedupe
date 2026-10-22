"""Cleanup priorities, keeper hints, edited copies and scan observability."""
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

from dedupe.cleanup import cleanup_opportunity
from dedupe.edited_copy import verify_edited_copy
from dedupe.engine import run_scan
from dedupe.grouping import build_one_group, rank_keep_candidate
from dedupe.models import FileRecord, GroupKind, MediaType, ScanDiagnostics


def record(path, **kwargs):
    return FileRecord(str(path), 1000, 1, MediaType.IMAGE, ".jpg", **kwargs)


def test_preferred_folder_precedes_resolution_and_is_explained(tmp_path):
    original = record(tmp_path / "original.jpg", width=600, height=400, keeper_preference=1)
    upscale = record(tmp_path / "upscaled.jpg", width=1200, height=800)
    group = build_one_group(GroupKind.SIMILAR, [original, upscale])
    assert group.suggested_keep == original.path
    assert "preferred folder" in cleanup_opportunity(group, [98])["keeper_reason"]


def test_capture_metadata_outranks_larger_export(tmp_path):
    image = Image.new("RGB", (80, 80), "blue")
    exif = Image.Exif()
    exif[271], exif[272], exif[36867] = "Camera", "Model", "2026:10:22 12:00:00"
    image.save(tmp_path / "original.jpg", exif=exif)
    original = record(tmp_path / "original.jpg", width=80, height=80)
    export = record(tmp_path / "export.jpg", width=160, height=160)
    assert rank_keep_candidate(original) > rank_keep_candidate(export)
    assert original.capture_metadata


def test_semantic_opportunities_remain_lower_priority():
    members = [record("/a.jpg", width=80, height=80), record("/b.jpg", width=40, height=40)]
    verified = build_one_group(GroupKind.SIMILAR, members)
    semantic = build_one_group(GroupKind.SIMILAR, members, match_basis="semantic")
    assert cleanup_opportunity(verified, [99])["score"] > cleanup_opportunity(semantic)["score"]
    assert semantic.selected_for_removal == []


def test_edited_copy_verifies_crop_filter_and_rejects_local_changes(tmp_path):
    import pytest
    pytest.importorskip("cv2")
    source = Path(__file__).parent / "fixtures/astronaut.png"
    with Image.open(source) as image:
        image = image.convert("RGB")
        crop = image.crop((64, 64, 448, 448))
        filtered = ImageEnhance.Brightness(ImageOps.grayscale(crop)).enhance(1.1)
        filtered.save(tmp_path / "crop.png")
        changed = image.copy()
        changed.paste((0, 0, 0), (170, 128, 256, 256))
        changed.save(tmp_path / "changed.png")
    assert verify_edited_copy(str(source), str(tmp_path / "crop.png"))
    assert not verify_edited_copy(str(source), str(tmp_path / "changed.png"))


def test_cross_folder_preferences_timing_and_cache(tmp_path):
    for directory in ("originals", "exports"):
        (tmp_path / directory).mkdir()
    image = Image.new("RGB", (100, 80), "blue")
    image.save(tmp_path / "originals/a.png")
    image.save(tmp_path / "exports/b.png")
    kwargs = {"preferred_folders": [str(tmp_path / "originals")],
                  "similar": False, "find_low_resolution": False, "random_review_count": 0,
                  "cache_path": tmp_path / "cache.sqlite3", "workers": 1}
    cold = run_scan([tmp_path / "originals", tmp_path / "exports"], **kwargs)
    warm = run_scan([tmp_path / "originals", tmp_path / "exports"], **kwargs)
    assert cold.exact_groups == 1
    assert cold.groups[0].suggested_keep == str(tmp_path / "originals/a.png")
    assert cold.diagnostics.first_duplicate_seconds is not None
    assert warm.diagnostics.cache_hits == 2
    assert ScanDiagnostics.from_dict(cold.diagnostics.to_dict()) == cold.diagnostics
    assert ScanDiagnostics.from_dict({}).first_duplicate_seconds is None


def test_semantic_discovery_verifies_lower_scoring_crop_and_rejects_same_scene(tmp_path, monkeypatch):
    import numpy as np
    import pytest

    from dedupe.semantic import find_semantic_groups

    pytest.importorskip("cv2")
    source = Path(__file__).parent / "fixtures/astronaut.png"
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.crop((64, 64, 448, 448)).save(tmp_path / "crop.png")
        changed = image.copy()
        changed.paste((0, 0, 0), (170, 128, 256, 256))
        changed.save(tmp_path / "changed.png")
    first = record(source)
    crop = record(tmp_path / "crop.png")
    changed = record(tmp_path / "changed.png")

    def embed(records, **kwargs):
        return {r.path: np.array([1, 0] if r is first else [0.95, 0.31225],
                                dtype="<f4").tobytes() for r in records}, []

    monkeypatch.setattr("dedupe.semantic.embed_records", embed)
    _, duplicates, _ = find_semantic_groups([first, crop], duplicate_threshold=0.94)
    assert len(duplicates) == 1
    _, duplicates, _ = find_semantic_groups([first, changed], duplicate_threshold=0.94)
    assert not duplicates


def test_metadata_endpoint_rejects_unscanned_files(tmp_path):
    from dedupe.web.app import create_app

    secret = tmp_path / "outside.png"
    Image.new("RGB", (20, 20)).save(secret)
    app = create_app(review_session_path=tmp_path / "review.json")
    response = app.test_client().get("/api/media-details", query_string={"path": str(secret)})
    assert response.status_code == 403


def test_semantic_block_search_keeps_cross_block_matches(monkeypatch):
    import numpy as np

    from dedupe import semantic

    monkeypatch.setattr(semantic, "SIMILARITY_BLOCK", 2)
    vectors = {str(i): np.array(v, dtype="<f4").tobytes()
               for i, v in enumerate(([1, 0], [0, 1], [1, 0], [0, 1], [-1, 0]))}
    _, scores = semantic.semantic_neighbors(vectors, 0.99)
    assert set(scores) == {("0", "2"), ("1", "3")}
