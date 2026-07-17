"""Exact duplicate detection via size → partial hash → SHA-256."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from .models import FileRecord
from .parallel import DEFAULT_EXACT_WORKERS_CAP, map_parallel, resolve_workers

ProgressCb = Callable[[str, int, int], None]

PARTIAL_SIZE = 64 * 1024
CHUNK_SIZE = 1024 * 1024


def file_partial_hash(path: str | Path, nbytes: int = PARTIAL_SIZE) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(nbytes))
    return h.hexdigest()


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _partial_job(path: str) -> tuple[str, str | None, str | None]:
    """Worker: (path, partial_hash, error)."""
    try:
        return path, file_partial_hash(path), None
    except OSError as exc:
        return path, None, f"partial hash failed: {exc}"


def _sha256_job(path: str) -> tuple[str, str | None, str | None]:
    """Worker: (path, sha256, error)."""
    try:
        return path, file_sha256(path), None
    except OSError as exc:
        return path, None, f"sha256 failed: {exc}"


def find_exact_groups(
    records: list[FileRecord],
    *,
    progress: ProgressCb | None = None,
    hash_fn: Callable[[str | Path], str] | None = None,
    partial_fn: Callable[[str | Path], str] | None = None,
    workers: int | None = None,
) -> list[list[FileRecord]]:
    """
    Return groups of 2+ files that are byte-identical.
    Mutates records to set partial_hash and sha256 where computed.
    """
    n_workers = resolve_workers(workers, cap=DEFAULT_EXACT_WORKERS_CAP)
    # Custom hash fns (tests) force sequential — worker jobs call the defaults.
    use_pool = n_workers > 1 and hash_fn is None and partial_fn is None
    hash_fn = hash_fn or file_sha256
    partial_fn = partial_fn or file_partial_hash

    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for rec in records:
        if rec.size > 0:
            by_size[rec.size].append(rec)

    candidates = [group for group in by_size.values() if len(group) >= 2]
    total_candidates = sum(len(g) for g in candidates)

    # --- Partial hash pass ---
    partial_targets: list[FileRecord] = []
    for group in candidates:
        for rec in group:
            if not rec.partial_hash:
                partial_targets.append(rec)

    if partial_targets:
        if use_pool:
            by_path = {r.path: r for r in partial_targets}

            def partial_progress(done: int, total: int) -> None:
                if progress:
                    progress("exact-partial", done, max(total_candidates, 1))

            results = map_parallel(
                _partial_job,
                [r.path for r in partial_targets],
                workers=n_workers,
                backend="thread",
                progress=partial_progress,
                progress_every=20,
            )
            for path, ph, err in results:
                rec = by_path[path]
                if err:
                    rec.error = err
                else:
                    rec.partial_hash = ph
        else:
            for i, rec in enumerate(partial_targets):
                try:
                    rec.partial_hash = partial_fn(rec.path)
                except OSError as exc:
                    rec.error = f"partial hash failed: {exc}"
                if progress and ((i + 1) % 20 == 0 or i + 1 == len(partial_targets)):
                    progress("exact-partial", i + 1, max(total_candidates, 1))

    if progress:
        progress("exact-partial", total_candidates, max(total_candidates, 1))

    partial_buckets: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
    for group in candidates:
        for rec in group:
            if rec.partial_hash:
                partial_buckets[(rec.size, rec.partial_hash)].append(rec)

    # --- Full hash pass ---
    exact_groups: list[list[FileRecord]] = []
    full_candidates = [g for g in partial_buckets.values() if len(g) >= 2]
    full_total = sum(len(g) for g in full_candidates)

    full_targets: list[FileRecord] = []
    for group in full_candidates:
        for rec in group:
            if not rec.sha256:
                full_targets.append(rec)

    if full_targets:
        if use_pool:
            by_path = {r.path: r for r in full_targets}

            def full_progress(done: int, total: int) -> None:
                if progress:
                    progress("exact-full", done, max(full_total, 1))

            results = map_parallel(
                _sha256_job,
                [r.path for r in full_targets],
                workers=n_workers,
                backend="thread",
                progress=full_progress,
                progress_every=10,
            )
            for path, sh, err in results:
                rec = by_path[path]
                if err:
                    rec.error = err
                else:
                    rec.sha256 = sh
        else:
            for i, rec in enumerate(full_targets):
                try:
                    rec.sha256 = hash_fn(rec.path)
                except OSError as exc:
                    rec.error = f"sha256 failed: {exc}"
                if progress and ((i + 1) % 10 == 0 or i + 1 == len(full_targets)):
                    progress("exact-full", i + 1, max(full_total, 1))

    if progress:
        progress("exact-full", full_total, max(full_total, 1))

    sha_buckets: dict[str, list[FileRecord]] = defaultdict(list)
    for group in full_candidates:
        for rec in group:
            if rec.sha256:
                sha_buckets[rec.sha256].append(rec)

    for group in sha_buckets.values():
        if len(group) >= 2:
            # de-dupe by path (paranoia)
            by_path_g: dict[str, FileRecord] = {r.path: r for r in group}
            members = list(by_path_g.values())
            if len(members) >= 2:
                exact_groups.append(members)

    return exact_groups
