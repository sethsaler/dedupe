"""Semantic look-alikes: EmbeddingGemma 2 image embeddings through DigUp's helper.

DigUp (https://github.com/ARahim3/DigUp) ships ``digup worker``, a llama.cpp
build of Google DeepMind's EmbeddingGemma 2 that speaks JSON lines on
stdin/stdout. Dedupe drives that helper instead of bundling a model runtime:
no new Python dependencies, and the model files DigUp already downloaded
(~865 MB) are reused.

Perceptual hashes only see pixels that line up. Embeddings see what a picture
*shows*, so this layer catches crops, flips, grayscale or filtered edits,
recompressed screenshots, and burst shots that the hash matchers miss. In
measurements on real photos, edits of one image scored 0.90-0.99 cosine and
unrelated photos 0.51-0.69, so the default threshold sits at 0.88.

Protocol (see DigUp's ``worker/digup_worker.py``)::

    -> {"id": 1, "op": "image", "inputs": ["/tmp/a.jpg"], "budget": 140}
    <- {"id": 1, "ok": true, "dim": 768, "count": 1,
        "vectors": "<base64 float32 LE>", "errors": [null]}
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Self

from .cache import HashCache, default_cache_path
from .edited_copy import edited_copy_verdict
from .grouping import cluster_around_best
from .models import FileRecord, MediaType

if TYPE_CHECKING:
    from .cache import DistinctReviews

# Same subject or shoot: groups for the Sets review.
DEFAULT_THRESHOLD = 0.88
# Near-duplicates for the Similar review, when brightness histograms agree too.
DEFAULT_DUPLICATE_THRESHOLD = 0.97
DUPLICATE_HISTOGRAM_MIN = 0.75
HISTOGRAM_SIDE = 256
# Vision tokens per image. 140 separated edits from unrelated photos as well
# as 280 did, at roughly half the cost.
DEFAULT_BUDGET = 140
# Longest side of the JPEG handed to the helper. The model resizes to its own
# token budget anyway; this keeps HEIC/WebP/GIF decoding on our side, where
# Pillow (with pillow-heif) reads every format the scanner accepts.
PREPARE_MAX_SIDE = 896
PREPARE_VERSION = "p1"
REQUEST_BATCH = 16
SIMILARITY_BLOCK = 512
SEMANTIC_MEDIA_TYPES = (MediaType.IMAGE, MediaType.GIF)
ERROR_SEMANTIC_FAILED = "semantic embedding failed"
HELPER_ENV = "DEDUPE_DIGUP_HELPER"

ProgressCb = Callable[[str, int, int], None]


def find_digup_helper() -> str | None:
    """Path of DigUp's ``digup`` helper, or None when DigUp isn't installed."""
    override = os.environ.get(HELPER_ENV)
    if override:
        return override if os.access(override, os.X_OK) else None
    candidates = [
        Path("/Applications/DigUp.app/Contents/Helpers/digup"),
        Path.home() / "Applications/DigUp.app/Contents/Helpers/digup",
    ]
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("digup")


def semantic_available() -> bool:
    return find_digup_helper() is not None


class EmbeddingWorker:
    """One ``digup worker`` process; closes (and frees the model) on exit."""

    def __init__(self, helper: str | None = None) -> None:
        helper = helper or find_digup_helper()
        if helper is None:
            raise RuntimeError(
                "DigUp is not installed; get it from https://github.com/ARahim3/DigUp "
                f"or point {HELPER_ENV} at its digup helper"
            )
        # llama.cpp logs freely to stderr; discard it so the pipe never fills.
        self._process = subprocess.Popen(
            [helper, "worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        self._next_id = 0
        ready = self._read_message()
        if not ready.get("ready"):
            self.close()
            raise RuntimeError(f"DigUp worker failed to start: {ready.get('error')}")
        self.info: dict = ready.get("info") or {}
        self.dim = int(self.info.get("dim") or 768)

    @property
    def signature(self) -> str:
        """What a cached vector depends on: model revision, runtime, budget, prep."""
        return "|".join(
            (
                str(self.info.get("model", "")),
                str(self.info.get("revision", ""))[:12],
                str(self.info.get("runtime", "")),
                str(self.info.get("dtype", "")),
                f"b{DEFAULT_BUDGET}",
                PREPARE_VERSION,
            )
        )

    def _read_message(self) -> dict:
        assert self._process.stdout is not None
        while True:
            line = self._process.stdout.readline()
            if not line:
                raise RuntimeError("DigUp worker exited unexpectedly")
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)

    def embed_images(
        self, paths: list[str], budget: int = DEFAULT_BUDGET
    ) -> tuple[list[bytes | None], list[str | None]]:
        """Float32 vectors (raw bytes) for each path; None and a message on failure."""
        import numpy as np

        assert self._process.stdin is not None
        self._next_id += 1
        request = {"id": self._next_id, "op": "image", "inputs": paths, "budget": budget}
        self._process.stdin.write(json.dumps(request) + "\n")
        self._process.stdin.flush()
        reply = self._read_message()
        if not reply.get("ok"):
            raise RuntimeError(f"DigUp worker: {reply.get('error')}")
        dim = int(reply.get("dim") or self.dim)
        matrix = np.frombuffer(base64.b64decode(reply["vectors"]), dtype="<f4")
        matrix = matrix.reshape(int(reply["count"]), dim)
        errors = list(reply.get("errors") or [None] * len(paths))
        vectors: list[bytes | None] = []
        for row, error in zip(matrix, errors, strict=True):
            vectors.append(None if error else row.astype("<f4").tobytes())
        return vectors, errors

    def close(self) -> None:
        process = self._process
        if process.poll() is None:
            try:
                assert process.stdin is not None
                process.stdin.write('{"id": 0, "op": "quit"}\n')
                process.stdin.flush()
                process.wait(timeout=5)
            except Exception:
                process.kill()
                process.wait()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class EmbeddingCache:
    """Vectors keyed by file identity and model signature, in the hash cache file.

    Uses its own connection so the semantic stage can run on a scan-stage
    thread (sqlite connections are bound to the thread that opened them).
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.DatabaseError:
            pass
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_embeddings (
                path TEXT PRIMARY KEY,
                identity TEXT NOT NULL,
                signature TEXT NOT NULL,
                vector BLOB NOT NULL
            )
            """
        )
        self._conn.commit()

    def get_many(self, records: list[FileRecord], signature: str) -> dict[str, bytes]:
        found: dict[str, bytes] = {}
        by_path = {record.path: record for record in records}
        paths = list(by_path)
        for start in range(0, len(paths), 400):
            chunk = paths[start : start + 400]
            rows = self._conn.execute(
                "SELECT path, identity, signature, vector FROM semantic_embeddings "
                f"WHERE path IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall()
            for path, identity, row_signature, vector in rows:
                record = by_path[path]
                if row_signature == signature and identity == HashCache._identity(record):
                    found[path] = bytes(vector)
        return found

    def put_many(self, items: list[tuple[FileRecord, bytes]], signature: str) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO semantic_embeddings (path, identity, signature, vector) "
            "VALUES (?, ?, ?, ?)",
            [
                (record.path, HashCache._identity(record), signature, vector)
                for record, vector in items
            ],
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def prepare_image(path: str, out_dir: str) -> str:
    """Write an upright RGB JPEG of the first frame for the helper; return its path."""
    from PIL import Image, ImageOps

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass
    with Image.open(path) as img:
        img.seek(0)
        frame = ImageOps.exif_transpose(img).convert("RGB")
    frame.thumbnail((PREPARE_MAX_SIDE, PREPARE_MAX_SIDE))
    handle, target = tempfile.mkstemp(suffix=".jpg", dir=out_dir)
    os.close(handle)
    frame.save(target, "JPEG", quality=90)
    return target


def embed_records(
    records: list[FileRecord],
    *,
    use_cache: bool = True,
    cache_path: str | Path | None = None,
    workers: int = 4,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
    helper: str | None = None,
) -> tuple[dict[str, bytes], list[str]]:
    """Embed images and GIFs (first frame). Returns vectors by path and errors."""
    candidates = [r for r in records if r.media_type in SEMANTIC_MEDIA_TYPES]
    vectors: dict[str, bytes] = {}
    errors: list[str] = []
    if len(candidates) < 2:
        return vectors, errors

    def check_cancelled() -> None:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")

    total = len(candidates)
    cache = EmbeddingCache(cache_path) if use_cache else None
    try:
        with EmbeddingWorker(helper) as worker:
            signature = worker.signature
            if cache is not None:
                vectors.update(cache.get_many(candidates, signature))
            pending = [r for r in candidates if r.path not in vectors]
            done = total - len(pending)
            if progress:
                progress("semantic-embed", done, total)
            with (
                tempfile.TemporaryDirectory(prefix="dedupe-semantic-") as tmp,
                ThreadPoolExecutor(max_workers=max(1, workers)) as pool,
            ):

                def prepare(record: FileRecord) -> str | None:
                    try:
                        return prepare_image(record.path, tmp)
                    except Exception as exc:
                        message = f"{ERROR_SEMANTIC_FAILED}: {record.path}: {exc}"
                        errors.append(message[:300])
                        return None

                for start in range(0, len(pending), REQUEST_BATCH):
                    check_cancelled()
                    batch = pending[start : start + REQUEST_BATCH]
                    prepared = list(pool.map(prepare, batch))
                    ready = [(r, p) for r, p in zip(batch, prepared, strict=True) if p]
                    if ready:
                        rows, row_errors = worker.embed_images([p for _r, p in ready])
                        fresh: list[tuple[FileRecord, bytes]] = []
                        for (record, prepared_path), row, error in zip(
                            ready, rows, row_errors, strict=True
                        ):
                            os.unlink(prepared_path)
                            if row is None:
                                errors.append(
                                    f"{ERROR_SEMANTIC_FAILED}: {record.path}: {error}"[:300]
                                )
                                continue
                            vectors[record.path] = row
                            fresh.append((record, row))
                        if cache is not None and fresh:
                            cache.put_many(fresh, signature)
                    done += len(batch)
                    if progress:
                        progress("semantic-embed", done, total)
    finally:
        if cache is not None:
            cache.close()
    return vectors, errors


def semantic_neighbors(
    vectors: dict[str, bytes],
    threshold: float = DEFAULT_THRESHOLD,
    *,
    same_set: dict[str, int] | None = None,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[dict[str, set[str]], dict[tuple[str, str], float]]:
    """All pairs at or above ``threshold`` cosine, as adjacency plus pair scores.

    ``same_set`` maps paths to an exact-duplicate set id: byte-identical copies
    embed identically and are already handled by the exact layer, so pairs
    inside one set are skipped.
    """
    import numpy as np

    paths = sorted(vectors)
    adjacency: dict[str, set[str]] = {}
    scores: dict[tuple[str, str], float] = {}
    if len(paths) < 2:
        return adjacency, scores
    matrix = np.stack([np.frombuffer(vectors[p], dtype="<f4") for p in paths])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.maximum(norms, 1e-12)
    sets = same_set or {}
    total = len(paths)
    for start in range(0, total, SIMILARITY_BLOCK):
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        # Earlier blocks already compared this block with preceding rows.
        block = matrix[start : start + SIMILARITY_BLOCK] @ matrix[start:].T
        rows, cols = np.nonzero(block >= threshold)
        for row, col in zip(rows.tolist(), cols.tolist(), strict=True):
            i = start + row
            col += start
            if col <= i:
                continue
            a, b = paths[i], paths[col]
            if a in sets and sets.get(b) == sets[a]:
                continue
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)
            scores[(a, b)] = float(block[row, col])
        if progress:
            progress("semantic-cluster", min(total, start + SIMILARITY_BLOCK), total)
    return adjacency, scores


def pair_score(scores: dict[tuple[str, str], float], a: str, b: str) -> float | None:
    return scores.get((a, b) if a < b else (b, a))


def luminance_histogram(path: str):
    """Normalized 32-bin brightness histogram of the upright first frame."""
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as img:
        img.seek(0)
        frame = ImageOps.exif_transpose(img).convert("L")
    frame.thumbnail((HISTOGRAM_SIDE, HISTOGRAM_SIDE))
    counts = np.asarray(frame.histogram(), dtype=np.float64).reshape(32, 8).sum(axis=1)
    return counts / max(1.0, counts.sum())


def histograms_agree(left, right, minimum: float = DUPLICATE_HISTOGRAM_MIN) -> bool:
    """Histogram intersection: 1.0 for identical tonal distributions."""
    import numpy as np

    return float(np.minimum(left, right).sum()) >= minimum


def find_semantic_groups(
    records: list[FileRecord],
    *,
    set_threshold: float = DEFAULT_THRESHOLD,
    duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
    distinct: DistinctReviews | None = None,
    exclude_from_duplicates: set[str] | None = None,
    exact_path_sets: list[set[str]] | None = None,
    use_cache: bool = True,
    cache_path: str | Path | None = None,
    workers: int = 4,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
    helper: str | None = None,
) -> tuple[
    list[tuple[list[FileRecord], dict[str, float]]],
    list[tuple[list[FileRecord], dict[str, float]]],
    list[str],
]:
    """One embedding pass, two layers: ``(sets, duplicates, errors)``.

    *Sets* group every image at ``set_threshold`` cosine: same subject or
    shoot, for browsing. *Duplicates* keep only pairs at
    ``duplicate_threshold`` whose brightness histograms also agree (crops,
    flips, resizes, grayscale and recompression pass; other scenes the model
    rates as close do not) and whose files no perceptual Similar group holds
    (``exclude_from_duplicates``). Each entry is ``(members, scores)``: the
    members built around an anchor (first), and each other member's cosine
    with it.
    """
    vectors, errors = embed_records(
        records,
        use_cache=use_cache,
        cache_path=cache_path,
        workers=workers,
        progress=progress,
        cancelled=cancelled,
        helper=helper,
    )
    same_set: dict[str, int] = {}
    for index, paths in enumerate(exact_path_sets or []):
        for path in paths:
            same_set[path] = index
    adjacency, scores = semantic_neighbors(
        vectors,
        min(set_threshold, duplicate_threshold),
        same_set=same_set,
        progress=progress,
        cancelled=cancelled,
    )
    by_path = {r.path: r for r in records}

    def build(adjacent: dict[str, set[str]], reviews) -> list:
        built = []
        members_pool = [by_path[path] for path in adjacent]
        for members in cluster_around_best(members_pool, adjacent, reviews):
            anchor = members[0]
            member_scores = {
                member.path: score
                for member in members[1:]
                if (score := pair_score(scores, anchor.path, member.path)) is not None
            }
            built.append((members, member_scores))
        return built

    def restrict(keep) -> dict[str, set[str]]:
        restricted: dict[str, set[str]] = {}
        for (a, b), score in scores.items():
            if keep(a, b, score):
                restricted.setdefault(a, set()).add(b)
                restricted.setdefault(b, set()).add(a)
        return restricted

    sets = build(restrict(lambda _a, _b, score: score >= set_threshold), None)

    excluded = exclude_from_duplicates or set()
    histograms: dict[str, object] = {}

    def histogram(path: str):
        if path not in histograms:
            try:
                histograms[path] = luminance_histogram(path)
            except Exception:
                histograms[path] = None
        return histograms[path]

    def duplicate_pair(a: str, b: str, score: float) -> bool:
        if score < duplicate_threshold or a in excluded or b in excluded:
            return False
        # Broader discovery must establish image correspondence, not just
        # matching subject matter or a similar brightness distribution.
        verdict = edited_copy_verdict(a, b)
        if verdict is not None:
            return verdict
        left, right = histogram(a), histogram(b)
        return (score >= DEFAULT_DUPLICATE_THRESHOLD and left is not None
                and right is not None and histograms_agree(left, right))

    duplicates = build(restrict(duplicate_pair), distinct)
    del adjacency
    return sets, duplicates, errors
