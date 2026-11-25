"""Exact duplicate detection via size → partial hash → SHA-256 and pixel-exact matching."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from .models import FileRecord, MediaType
from .parallel import DEFAULT_EXACT_WORKERS_CAP, map_parallel, resolve_workers
from .similar_image import canonical_pixel_sha256, jpeg_bitstream_sha256

ProgressCb = Callable[[str, int, int], None]

PARTIAL_SIZE = 64 * 1024
CHUNK_SIZE = 1024 * 1024
LARGE_SPARSE_FILTER_MIN_BYTES = 4 * 1024 * 1024
_JPEG_EXTS = frozenset({".jpg", ".jpeg"})

# Error-kind prefixes; engine diagnostics classify on these.
ERROR_PARTIAL_HASH_FAILED = "partial hash failed"
ERROR_SHA256_FAILED = "sha256 failed"


def file_partial_hash(path: str | Path, nbytes: int = PARTIAL_SIZE) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(nbytes))
    return h.hexdigest()


def file_sha256(
    path: str | Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    if cancelled is None:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    if cancelled():
        raise InterruptedError("scan cancelled")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancelled():
                raise InterruptedError("scan cancelled")
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


_DEFAULT_FILE_SHA256 = file_sha256


def _file_sparse_mid_tail_hash(path: str | Path, size: int) -> str:
    """Hash 64 KiB slices at the midpoint and tail of a large file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(size // 2)
        h.update(f.read(PARTIAL_SIZE))
        f.seek(max(0, size - PARTIAL_SIZE))
        h.update(f.read(PARTIAL_SIZE))
    return h.hexdigest()


def _sparse_job(item: tuple[str, int]) -> tuple[str, str | None]:
    """Worker: (path, sparse_digest_or_none)."""
    path, size = item
    try:
        return path, _file_sparse_mid_tail_hash(path, size)
    except OSError:
        return path, None


def _partial_job(path: str) -> tuple[str, str | None, str | None]:
    """Worker: (path, partial_hash, error)."""
    try:
        return path, file_partial_hash(path), None
    except OSError as exc:
        return path, None, f"{ERROR_PARTIAL_HASH_FAILED}: {exc}"


def _sha256_job(
    path: str,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str, str | None, str | None]:
    """Worker: (path, sha256, error)."""
    try:
        if cancelled is not None and file_sha256 is _DEFAULT_FILE_SHA256:
            return path, file_sha256(path, cancelled=cancelled), None
        return path, file_sha256(path), None
    except OSError as exc:
        return path, None, f"{ERROR_SHA256_FAILED}: {exc}"


def find_exact_groups(
    records: list[FileRecord],
    *,
    progress: ProgressCb | None = None,
    hash_fn: Callable[[str | Path], str] | None = None,
    partial_fn: Callable[[str | Path], str] | None = None,
    workers: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    arrival_paths: set[str] | None = None,
    errors: list[str] | None = None,
) -> list[list[FileRecord]]:
    """
    Return groups of 2+ files that are byte-identical.
    Mutates records to set partial_hash and sha256 where computed.
    """
    n_workers = resolve_workers(workers, cap=DEFAULT_EXACT_WORKERS_CAP)
    # Custom hash fns (tests) force sequential — worker jobs call the defaults.
    default_hashes = hash_fn is None and partial_fn is None
    use_pool = n_workers > 1 and default_hashes
    hash_fn = hash_fn or file_sha256
    partial_fn = partial_fn or file_partial_hash

    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for rec in records:
        if rec.size > 0:
            by_size[rec.size].append(rec)

    candidates = [
        group
        for group in by_size.values()
        if len(group) >= 2
        and (arrival_paths is None or any(rec.path in arrival_paths for rec in group))
    ]
    total_candidates = sum(len(g) for g in candidates)

    # --- Partial hash pass ---
    partial_targets: list[FileRecord] = []
    for group in candidates:
        for rec in group:
            if not rec.partial_hash:
                partial_targets.append(rec)
    partial_cached = total_candidates - len(partial_targets)

    if partial_targets:
        if progress and partial_cached:
            progress("exact-partial", partial_cached, max(total_candidates, 1))
        if use_pool:
            by_path = {r.path: r for r in partial_targets}
            partial_chunk = max(1, min(64, len(partial_targets) // (n_workers * 4)))

            def partial_progress(done: int, _total: int) -> None:
                if progress:
                    progress("exact-partial", partial_cached + done, max(total_candidates, 1))

            results = map_parallel(
                _partial_job,
                [r.path for r in partial_targets],
                workers=n_workers,
                backend="thread",
                progress=partial_progress,
                progress_every=1,
                cancelled=cancelled,
                chunk_size=partial_chunk,
            )
            for path, ph, err in results:
                rec = by_path[path]
                if err:
                    rec.error = err
                    if errors is not None:
                        errors.append(err)
                else:
                    rec.partial_hash = ph
        else:
            for i, rec in enumerate(partial_targets):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                try:
                    rec.partial_hash = partial_fn(rec.path)
                except OSError as exc:
                    err = f"{ERROR_PARTIAL_HASH_FAILED}: {exc}"
                    rec.error = err
                    if errors is not None:
                        errors.append(err)
                done = partial_cached + i + 1
                if progress and ((i + 1) % 20 == 0 or i + 1 == len(partial_targets)):
                    progress("exact-partial", done, max(total_candidates, 1))

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
    if default_hashes and file_sha256 is _DEFAULT_FILE_SHA256:
        # Skip buckets where any member already has a cached sha256 so cached
        # large files are never re-read just for a sparse pre-check.
        large_buckets = [
            group
            for group in full_candidates
            if group[0].size >= LARGE_SPARSE_FILTER_MIN_BYTES
            and all(not rec.sha256 for rec in group)
        ]
        if large_buckets:
            other_buckets = [
                group
                for group in full_candidates
                if not (
                    group[0].size >= LARGE_SPARSE_FILTER_MIN_BYTES
                    and all(not rec.sha256 for rec in group)
                )
            ]
            sparse_items = [
                (rec.path, rec.size)
                for group in large_buckets
                for rec in group
                if not rec.sha256
            ]
            sparse_by_path: dict[str, str | None] = {}
            if use_pool:
                for path, digest in map_parallel(
                    _sparse_job,
                    sparse_items,
                    workers=n_workers,
                    backend="thread",
                    cancelled=cancelled,
                ):
                    sparse_by_path[path] = digest
            else:
                for path, size in sparse_items:
                    if cancelled and cancelled():
                        raise InterruptedError("scan cancelled")
                    _, digest = _sparse_job((path, size))
                    sparse_by_path[path] = digest
            filtered_large: list[list[FileRecord]] = []
            for group in large_buckets:
                unreadable: list[FileRecord] = []
                by_sparse: dict[str, list[FileRecord]] = defaultdict(list)
                for rec in group:
                    digest = sparse_by_path.get(rec.path)
                    if digest is None:
                        unreadable.append(rec)
                    else:
                        by_sparse[digest].append(rec)
                if unreadable:
                    # A transient sparse-read error must not drop readable
                    # singletons that could still match it on full SHA-256.
                    filtered_large.append(group)
                else:
                    for sub in by_sparse.values():
                        if len(sub) >= 2:
                            filtered_large.append(sub)
            full_candidates = other_buckets + filtered_large

    full_total = sum(len(g) for g in full_candidates)

    full_targets: list[FileRecord] = []
    for group in full_candidates:
        for rec in group:
            if not rec.sha256:
                if default_hashes and rec.size <= PARTIAL_SIZE:
                    # The prefix pass already read the entire file. Its SHA-256
                    # is the full digest, including when hydrated from cache.
                    rec.sha256 = rec.partial_hash
                else:
                    full_targets.append(rec)
    full_cached = full_total - len(full_targets)

    if full_targets:
        if progress and full_cached:
            progress("exact-full", full_cached, max(full_total, 1))
        if use_pool:
            by_path = {r.path: r for r in full_targets}
            avg_size = sum(r.size for r in full_targets) / len(full_targets)
            full_chunk = (
                max(1, min(32, len(full_targets) // (n_workers * 4)))
                if avg_size <= 512 * 1024
                else 1
            )

            def full_progress(done: int, _total: int) -> None:
                if progress:
                    progress("exact-full", full_cached + done, max(full_total, 1))

            job_fn = (
                _sha256_job
                if cancelled is None
                else (lambda p: _sha256_job(p, cancelled))
            )
            results = map_parallel(
                job_fn,
                [r.path for r in full_targets],
                workers=n_workers,
                backend="thread",
                progress=full_progress,
                progress_every=1,
                cancelled=cancelled,
                chunk_size=full_chunk,
            )
            for path, sh, err in results:
                rec = by_path[path]
                if err:
                    rec.error = err
                    if errors is not None:
                        errors.append(err)
                else:
                    rec.sha256 = sh
        else:
            for i, rec in enumerate(full_targets):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                try:
                    if cancelled is not None and hash_fn is _DEFAULT_FILE_SHA256:
                        rec.sha256 = hash_fn(rec.path, cancelled=cancelled)
                    else:
                        rec.sha256 = hash_fn(rec.path)
                except OSError as exc:
                    err = f"{ERROR_SHA256_FAILED}: {exc}"
                    rec.error = err
                    if errors is not None:
                        errors.append(err)
                done = full_cached + i + 1
                if progress and ((i + 1) % 10 == 0 or i + 1 == len(full_targets)):
                    progress("exact-full", done, max(full_total, 1))

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
            if len(members) >= 2 and (
                arrival_paths is None or any(m.path in arrival_paths for m in members)
            ):
                exact_groups.append(members)

    return exact_groups


def _is_jpeg_record(rec: FileRecord) -> bool:
    ext = (rec.extension or Path(rec.path).suffix).lower()
    return ext in _JPEG_EXTS


def classify_pixel_exact_group(
    members: list[FileRecord],
    *,
    keeper_path: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str | None, dict[str, str]]:
    """Detect metadata-only / pixel-exact equivalence among ``SIMILAR`` image members.

    Returns ``(match_basis, match_explanations)`` where ``match_basis`` is
    ``"pixel_exact"`` when every member shares the same JPEG bitstream or
    canonical EXIF-transposed sRGB pixels (and display dimensions + pHash == 0).
    """
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    if len(members) < 2 or any(m.media_type != MediaType.IMAGE for m in members):
        return None, {}
    keeper = next((m for m in members if m.path == keeper_path), members[0])
    if not (keeper.width and keeper.height and keeper.width > 0 and keeper.height > 0 and keeper.phash):
        return None, {}
    keeper_dims = (keeper.width, keeper.height)
    keeper_phash = keeper.phash

    candidates = [
        m
        for m in members
        if m.width == keeper_dims[0]
        and m.height == keeper_dims[1]
        and m.phash == keeper_phash
    ]
    if len(candidates) < 2:
        return None, {}

    others = [m for m in candidates if m.path != keeper.path]

    # Check fast JPEG bitstream identity first when all candidates are JPEGs.
    if all(_is_jpeg_record(m) for m in candidates):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        k_digest = jpeg_bitstream_sha256(keeper.path, cancelled=cancelled)
        if k_digest:
            matched = []
            for m in others:
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                if jpeg_bitstream_sha256(m.path, cancelled=cancelled) == k_digest:
                    matched.append(m)
            if matched:
                explanations = {
                    m.path: "Identical JPEG image bitstream (metadata differs)"
                    for m in matched
                }
                basis = "pixel_exact" if len(matched) == len(members) - 1 else None
                return basis, explanations

    # Fall back to canonical decoded sRGB pixel SHA-256 (EXIF-transposed).
    # Decode keeper first so an unreadable/animated keeper skips decoding siblings.
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    k_pixel = canonical_pixel_sha256(keeper.path)
    if not k_pixel:
        return None, {}
    matched = []
    for m in others:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        if canonical_pixel_sha256(m.path) == k_pixel:
            matched.append(m)
    if not matched:
        return None, {}
    explanations = {
        m.path: "Identical decoded pixels (metadata or encoding differs)"
        for m in matched
    }
    basis = "pixel_exact" if len(matched) == len(members) - 1 else None
    return basis, explanations


def find_metadata_exact_groups(
    records: list[FileRecord],
    *,
    cancelled: Callable[[], bool] | None = None,
    arrival_paths: set[str] | None = None,
) -> list[list[FileRecord]]:
    """Return groups of non-byte-identical images sharing bitstream or canonical pixel SHA-256."""
    buckets: dict[tuple[int, int, str], list[FileRecord]] = defaultdict(list)
    for rec in records:
        if (
            rec.media_type == MediaType.IMAGE
            and rec.width
            and rec.height
            and rec.phash
        ):
            buckets[(rec.width, rec.height, rec.phash)].append(rec)

    groups: list[list[FileRecord]] = []
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        if arrival_paths is not None and not any(r.path in arrival_paths for r in bucket):
            continue
        if all(r.sha256 and r.sha256 == bucket[0].sha256 and r.size == bucket[0].size for r in bucket):
            continue

        matched_paths: set[str] = set()
        if all(_is_jpeg_record(r) for r in bucket):
            by_bitstream: dict[str, list[FileRecord]] = defaultdict(list)
            for rec in bucket:
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                digest = jpeg_bitstream_sha256(rec.path, cancelled=cancelled)
                if digest:
                    by_bitstream[digest].append(rec)
            for sub in by_bitstream.values():
                if len(sub) >= 2 and not all(
                    r.sha256 and r.sha256 == sub[0].sha256 and r.size == sub[0].size for r in sub
                ):
                    groups.append(sub)
                    matched_paths.update(r.path for r in sub)

        remaining = [r for r in bucket if r.path not in matched_paths]
        if len(remaining) >= 2:
            by_pixel: dict[str, list[FileRecord]] = defaultdict(list)
            for rec in remaining:
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                digest = canonical_pixel_sha256(rec.path)
                if digest:
                    by_pixel[digest].append(rec)
            for sub in by_pixel.values():
                if len(sub) >= 2 and not all(
                    r.sha256 and r.sha256 == sub[0].sha256 and r.size == sub[0].size for r in sub
                ):
                    groups.append(sub)
    return groups
