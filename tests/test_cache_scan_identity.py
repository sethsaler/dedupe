"""Real-filesystem coverage for cached scan identity invalidation."""

from __future__ import annotations

import os

from dedupe.engine import run_scan


def test_cached_scan_rehashes_same_path_edit_with_preserved_mtime_and_reuses_pure_rename(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    original, copy = left / "original.jpg", right / "copy.jpg"
    prefix = b"x" * (64 * 1024)
    original.write_bytes(prefix + b"original")
    copy.write_bytes(original.read_bytes())
    options = {
        "cache_path": tmp_path / "hashes.sqlite3",
        "similar": False,
        "find_low_resolution": False,
        "random_review_count": 0,
        "workers": 2,
    }

    cold = run_scan([left, right], **options)
    warm = run_scan([left, right], **options)
    assert cold.exact_groups == warm.exact_groups == 1
    assert cold.diagnostics.cache_hits == 0
    assert warm.diagnostics.cache_hits == 2

    # Same-path byte replacement preserving mtime updates ctime_ns -> must invalidate cache.
    previous = copy.stat()
    copy.write_bytes(prefix + b"modified")
    os.utime(copy, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    current = copy.stat()
    assert current.st_size == previous.st_size
    assert current.st_mtime_ns == previous.st_mtime_ns
    assert current.st_ino == previous.st_ino
    assert current.st_ctime_ns != previous.st_ctime_ns

    changed = run_scan([left, right], **options)
    assert changed.diagnostics.cache_hits == 1
    assert changed.exact_groups == 0
    assert {record.path for record in changed.files} == {str(original), str(copy)}
    assert len({record.sha256 for record in changed.files}) == 2

    # Freshly computed hashes become reusable on the following scan.
    refreshed = run_scan([left, right], **options)
    assert refreshed.diagnostics.cache_hits == 2
    assert refreshed.exact_groups == 0

    # Pure POSIX rename updates ctime_ns while keeping (dev, ino, size, mtime_ns) intact ->
    # inode fallback must reuse the cached hashes instead of re-reading from disk.
    renamed = copy.rename(right / "renamed.jpg")
    renamed_stat = renamed.stat()
    assert renamed_stat.st_ctime_ns != current.st_ctime_ns
    after_rename = run_scan([left, right], **options)
    assert after_rename.diagnostics.cache_hits == 2
    assert after_rename.exact_groups == 0
