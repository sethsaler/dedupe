"""Image similarity tests using generated near-identical JPEGs."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from dedupe.models import FileRecord, MediaType
from dedupe.similar_image import (
    compute_image_hashes,
    find_similar_image_groups,
    is_near_identical,
    tile_distances,
)


def _make_image(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (64, 64), quality: int = 90) -> None:
    img = Image.new("RGB", size, color)
    # Draw a simple shape so hashes aren't pure flat color edge cases only
    for x in range(10, 40):
        for y in range(10, 40):
            img.putpixel((x, y), (255 - color[0], color[1], color[2]))
    img.save(path, format="JPEG", quality=quality)


def _soft_photo(path: Path, size: tuple[int, int] = (256, 256), shift: int = 0) -> None:
    """Smooth gradient photo-like image (pHash-friendly under rescales)."""
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        for x in range(w):
            # soft gradients + a soft "subject" blob whose center can shift
            cx, cy = w // 2 + shift, h // 2
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            r = int(40 + 80 * x / w + max(0, 60 - dist / 2))
            g = int(60 + 50 * y / h + max(0, 40 - dist / 3))
            b = int(90 + 40 * ((x + y) % w) / w)
            px[x, y] = (min(255, r), min(255, g), min(255, b))
    img.save(path, format="JPEG", quality=92)


def _scene(path: Path, person_xy: tuple[int, int], size: tuple[int, int] = (200, 200)) -> None:
    """Simple background + a 'person' blob at person_xy (different poses = different positions)."""
    img = Image.new("RGB", size, (30, 80, 40))  # green background
    draw = ImageDraw.Draw(img)
    # fixed background features
    draw.rectangle([0, 150, 200, 200], fill=(60, 40, 20))  # ground
    draw.rectangle([10, 20, 40, 80], fill=(180, 180, 200))  # building
    # movable subject
    x, y = person_xy
    draw.ellipse([x - 20, y - 40, x + 20, y + 20], fill=(200, 160, 140))
    draw.rectangle([x - 12, y + 20, x + 12, y + 70], fill=(40, 40, 160))
    img.save(path, format="JPEG", quality=90)


def _rec_for(path: Path) -> FileRecord:
    st = path.stat()
    return FileRecord(
        path=str(path.resolve()),
        size=st.st_size,
        mtime=st.st_mtime,
        media_type=MediaType.IMAGE,
        extension=path.suffix.lower(),
    )


def test_compute_image_hashes(tmp_path: Path) -> None:
    p = tmp_path / "a.jpg"
    _make_image(p, (40, 80, 120))
    ph, dh, w, h = compute_image_hashes(p)
    assert ph and dh
    assert w == 64 and h == 64


def test_similar_groups_near_identical_quality(tmp_path: Path) -> None:
    a = tmp_path / "orig.jpg"
    b = tmp_path / "reexport.jpg"
    c = tmp_path / "other.jpg"
    _make_image(a, (40, 80, 120), quality=95)
    _make_image(b, (40, 80, 120), quality=60)  # same content, different quality
    _make_image(c, (200, 10, 10), quality=90)  # different content

    groups = find_similar_image_groups(
        [_rec_for(a), _rec_for(b), _rec_for(c)],
        threshold=10,
    )
    assert len(groups) >= 1
    paths = {m.path for m in groups[0]}
    assert str(a.resolve()) in paths
    assert str(b.resolve()) in paths
    # different image should not be forced into the same group
    assert str(c.resolve()) not in paths


def test_reviewed_distinct_pair_is_not_grouped(tmp_path: Path) -> None:
    left = tmp_path / "left.jpg"
    right = tmp_path / "right.jpg"
    _make_image(left, (40, 80, 120), quality=95)
    _make_image(right, (40, 80, 120), quality=60)
    left_record = _rec_for(left)
    right_record = _rec_for(right)
    pair = tuple(sorted((left_record.path, right_record.path)))

    groups = find_similar_image_groups(
        [left_record, right_record],
        threshold=10,
        distinct_pairs={pair},
    )

    assert groups == []


def test_dissimilar_not_grouped(tmp_path: Path) -> None:
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    _make_image(a, (0, 0, 0))
    _make_image(b, (255, 255, 255))
    groups = find_similar_image_groups([_rec_for(a), _rec_for(b)], threshold=2)
    assert groups == []


def test_downscaled_duplicate_still_matches(tmp_path: Path) -> None:
    # Same image re-exported at lower quality (common exact-visual duplicate case).
    hi = tmp_path / "hi.jpg"
    lo = tmp_path / "lo.jpg"
    img = Image.new("RGB", (400, 300), (20, 60, 100))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 200, 400, 300], fill=(40, 30, 20))
    draw.rectangle([40, 80, 160, 200], fill=(180, 160, 140))
    draw.ellipse([220, 40, 340, 160], fill=(220, 200, 60))
    draw.polygon([(50, 80), (100, 20), (150, 80)], fill=(100, 40, 40))
    draw.line([(0, 250), (400, 220)], fill=(200, 200, 200), width=6)
    for i in range(0, 400, 25):
        draw.rectangle([i, 260, i + 10, 290], fill=(80 + (i % 50), 90, 70))
    img.save(hi, format="JPEG", quality=95)
    img.save(lo, format="JPEG", quality=50)

    assert is_near_identical(str(hi.resolve()), str(lo.resolve()))
    groups = find_similar_image_groups([_rec_for(hi), _rec_for(lo)], threshold=8)
    assert len(groups) == 1


def test_different_pose_rejected_by_tiles(tmp_path: Path) -> None:
    a = tmp_path / "pose_a.jpg"
    b = tmp_path / "pose_b.jpg"
    # Same scene background, subject in different place (stand-in for pose change)
    _scene(a, (60, 80))
    _scene(b, (150, 90))

    dists = tile_distances(str(a.resolve()), str(b.resolve()))
    assert dists is not None
    # Regional structure should diverge
    assert max(dists) > 8 or (sum(dists) / len(dists)) > 5

    groups = find_similar_image_groups([_rec_for(a), _rec_for(b)], threshold=12)
    assert groups == []


def test_real_false_positive_pairs_if_present(tmp_path: Path) -> None:
    """Regression: pose-different pairs should not be near-identical.

    Optional local fixture: set DEDUPE_TEST_REVIEW_ROOT to a directory whose
    immediate subfolders each contain 2+ JPEGs that should *not* match.
    """
    import os

    env_root = os.environ.get("DEDUPE_TEST_REVIEW_ROOT")
    if env_root:
        roots = [p for p in Path(env_root).iterdir() if p.is_dir()] if Path(env_root).is_dir() else []
    else:
        # Always-on synthetic stand-in: same background, subject in different place
        a = tmp_path / "pose_a.jpg"
        b = tmp_path / "pose_b.jpg"
        _scene(a, (60, 80))
        _scene(b, (150, 90))
        assert not is_near_identical(str(a), str(b))
        return

    for root in roots:
        imgs = sorted(
            p
            for p in root.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg"} and p.is_file()
        )
        if len(imgs) < 2:
            continue
        assert not is_near_identical(str(imgs[0]), str(imgs[1])), root.name


def test_similarity_chain_does_not_create_transitive_group(monkeypatch) -> None:
    """A~B and B~C must not imply A~C in one automatically removable group."""
    import dedupe.similar_image as module

    def record(path: str, phash: str) -> FileRecord:
        return FileRecord(
            path=path,
            size=1,
            mtime=1,
            media_type=MediaType.IMAGE,
            extension=".jpg",
            phash=phash,
            dhash="0000000000000000",
        )

    a = record("/tmp/a.jpg", "0000000000000000")
    b = record("/tmp/b.jpg", "000000000000003f")
    c = record("/tmp/c.jpg", "0000000000000fff")
    monkeypatch.setattr(module, "is_near_identical", lambda *_args, **_kwargs: True)

    groups = find_similar_image_groups([a, b, c], threshold=6, workers=1)

    assert all(len(group) == 2 for group in groups)
    assert not any({a.path, c.path} <= {member.path for member in group} for group in groups)
