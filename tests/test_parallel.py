"""Parallel hashing helpers and workers-equivalence tests."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from dedupe.exact import find_exact_groups
from dedupe.models import FileRecord, MediaType
from dedupe.parallel import (
    DEFAULT_EXACT_WORKERS_CAP,
    DEFAULT_HUMAN_WORKERS_CAP,
    DEFAULT_IMAGE_WORKERS_CAP,
    DEFAULT_VIDEO_WORKERS_CAP,
    DEFAULT_WORKERS_CAP,
    map_parallel,
    resolve_workers,
    split_cpu_budget,
)
from dedupe.similar_image import find_similar_image_groups


def _write(p: Path, data: bytes) -> FileRecord:
    p.write_bytes(data)
    st = p.stat()
    return FileRecord(
        path=str(p.resolve()),
        size=st.st_size,
        mtime=st.st_mtime,
        media_type=MediaType.IMAGE,
        extension=p.suffix.lower(),
    )


def _save_img(path: Path, color: tuple[int, int, int], quality: int = 90) -> None:
    img = Image.new("RGB", (48, 48), color)
    for x in range(5, 25):
        for y in range(5, 25):
            img.putpixel((x, y), (255, color[1], 0))
    img.save(path, format="JPEG", quality=quality)


def _img_rec(path: Path) -> FileRecord:
    st = path.stat()
    return FileRecord(
        path=str(path.resolve()),
        size=st.st_size,
        mtime=st.st_mtime,
        media_type=MediaType.IMAGE,
        extension=path.suffix.lower(),
    )


def test_resolve_workers() -> None:
    assert resolve_workers(1) == 1
    assert resolve_workers(4) == 4
    assert resolve_workers(0) >= 1
    assert resolve_workers(-1) >= 1
    assert resolve_workers(None) >= 1
    # Auto never exceeds the conservative overall cap
    assert resolve_workers(0) <= DEFAULT_WORKERS_CAP
    assert resolve_workers(None) <= DEFAULT_WORKERS_CAP
    # Stage caps always apply
    assert resolve_workers(64, cap=DEFAULT_IMAGE_WORKERS_CAP) == DEFAULT_IMAGE_WORKERS_CAP
    assert resolve_workers(64, cap=DEFAULT_EXACT_WORKERS_CAP) == DEFAULT_EXACT_WORKERS_CAP
    assert resolve_workers(64, cap=DEFAULT_VIDEO_WORKERS_CAP) == DEFAULT_VIDEO_WORKERS_CAP
    assert resolve_workers(64, cap=DEFAULT_HUMAN_WORKERS_CAP) == DEFAULT_HUMAN_WORKERS_CAP
    assert resolve_workers(1, cap=DEFAULT_VIDEO_WORKERS_CAP) == 1


def test_split_cpu_budget_divides_between_concurrent_cpu_stages() -> None:
    # Zero or one CPU-bound stage keeps the full budget.
    assert split_cpu_budget(8, 0) == 8
    assert split_cpu_budget(8, 1) == 8
    # Concurrent CPU-bound stages share it equally.
    assert split_cpu_budget(8, 2) == 4
    assert split_cpu_budget(8, 3) == 2
    # Never below one worker.
    assert split_cpu_budget(1, 3) == 1
    assert split_cpu_budget(0, 2) == 1
    # Small budgets floor at 2 per stage: parallel scan streams divide the
    # budget first, and a second division to 1 would serialize a CPU stage.
    assert split_cpu_budget(2, 2) == 2
    assert split_cpu_budget(3, 2) == 2


def test_map_parallel_cancel_returns_promptly() -> None:
    """Cancel must not block on shutdown(wait=True) behind a slow work item."""
    import threading
    import time

    import pytest

    started = threading.Event()
    cancel = threading.Event()

    def slow(x: int) -> int:
        started.set()
        # Far longer than the test is willing to wait; queued items are
        # cancelled, running ones finish in the background.
        deadline = time.monotonic() + 30
        while not cancel.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        return x

    cancel_at = time.monotonic()
    try:
        with pytest.raises(InterruptedError):
            map_parallel(
                slow,
                list(range(6)),
                workers=2,
                cancelled=lambda: started.is_set(),
            )
    finally:
        cancel.set()  # let the abandoned workers exit
    assert time.monotonic() - cancel_at < 5


def test_map_parallel_preserves_order() -> None:
    def square(x: int) -> int:
        return x * x

    items = list(range(20))
    serial = map_parallel(square, items, workers=1)
    threaded = map_parallel(square, items, workers=4, backend="thread")
    assert serial == threaded == [x * x for x in items]


def test_map_parallel_progress_callback() -> None:
    seen: list[tuple[int, int]] = []

    def progress(done: int, total: int) -> None:
        seen.append((done, total))

    map_parallel(lambda x: x, list(range(5)), workers=2, progress=progress, progress_every=1)
    assert seen[-1] == (5, 5)
    assert all(d <= t for d, t in seen)


def test_map_parallel_handles_large_lists() -> None:
    """Windowed submission: many items must not require all futures upfront."""
    n = 200
    out = map_parallel(lambda x: x + 1, list(range(n)), workers=3, backend="thread")
    assert out == [i + 1 for i in range(n)]


def test_exact_groups_workers_equivalent(tmp_path: Path) -> None:
    payload = b"exact-payload-for-parallel-test-!!!!"
    a = _write(tmp_path / "a.jpg", payload)
    b = _write(tmp_path / "b.jpg", payload)
    c = _write(tmp_path / "c.jpg", b"other-content-not-matching")
    d = _write(tmp_path / "d.jpg", payload)

    g1 = find_exact_groups([a, b, c, d], workers=1)
    # Fresh records for second run (hashes already set would short-circuit)
    a2 = _write(tmp_path / "a2.jpg", payload)
    b2 = _write(tmp_path / "b2.jpg", payload)
    c2 = _write(tmp_path / "c2.jpg", b"other-content-not-matching")
    d2 = _write(tmp_path / "d2.jpg", payload)
    g2 = find_exact_groups([a2, b2, c2, d2], workers=4)

    assert len(g1) == 1
    assert len(g2) == 1
    assert len(g1[0]) == 3
    assert len(g2[0]) == 3


def test_similar_image_groups_workers_equivalent(tmp_path: Path) -> None:
    _save_img(tmp_path / "s1.jpg", (30, 60, 90), quality=95)
    _save_img(tmp_path / "s2.jpg", (30, 60, 90), quality=50)
    _save_img(tmp_path / "u.jpg", (200, 10, 200), quality=90)

    recs1 = [_img_rec(tmp_path / n) for n in ("s1.jpg", "s2.jpg", "u.jpg")]
    recs2 = [_img_rec(tmp_path / n) for n in ("s1.jpg", "s2.jpg", "u.jpg")]

    g1 = find_similar_image_groups(recs1, threshold=12, workers=1)
    g2 = find_similar_image_groups(recs2, threshold=12, workers=4)

    def norm(groups: list) -> set[frozenset[str]]:
        return {frozenset(m.path for m in g) for g in groups}

    assert norm(g1) == norm(g2)
    assert len(g1) >= 1
