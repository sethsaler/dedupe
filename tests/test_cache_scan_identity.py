"""Real-filesystem coverage for cached scan identity invalidation."""

from __future__ import annotations

import os

import pytest

from dedupe.engine import run_scan


@pytest.mark.parametrize("rename_after_edit", [False, True])
def test_cached_scan_rehashes_same_size_edit_with_preserved_mtime(tmp_path, rename_after_edit):
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

    previous = copy.stat()
    copy.write_bytes(prefix + b"modified")
    os.utime(copy, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    if rename_after_edit:
        copy = copy.rename(right / "renamed.jpg")
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
