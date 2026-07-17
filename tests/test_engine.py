"""End-to-end engine scan with image fixtures."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from dedupe.engine import run_scan
from dedupe.models import GroupKind


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
