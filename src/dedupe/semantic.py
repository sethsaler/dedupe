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
import hashlib
import json
import os
import selectors
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
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
STARTUP_TIMEOUT = 180.0
REQUEST_TIMEOUT = 120.0
MAX_REPLY_BYTES = 8 * 1024 * 1024

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

    def __init__(
        self, helper: str | None = None, *,
        cancelled: Callable[[], bool] | None = None,
        startup_timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self._cancelled = cancelled
        self._request_timeout = REQUEST_TIMEOUT if request_timeout is None else request_timeout
        self._buffer = bytearray()
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
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
            bufsize=0,
            start_new_session=True,
        )
        self._next_id = 0
        try:
            ready = self._read_message(
                STARTUP_TIMEOUT if startup_timeout is None else startup_timeout
            )
            if not ready.get("ready"):
                raise RuntimeError(f"DigUp worker failed to start: {ready.get('error')}")
        except BaseException:
            self.close(force=True)
            raise
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

    def _read_message(self, timeout: float | None = None) -> dict:
        assert self._process.stdout is not None
        deadline = time.monotonic() + (self._request_timeout if timeout is None else timeout)
        with selectors.DefaultSelector() as selector:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            while True:
                if self._cancelled and self._cancelled():
                    raise InterruptedError("scan cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("DigUp stopped responding; retry the deep scan to continue")
                if b"\n" in self._buffer:
                    line, _, rest = self._buffer.partition(b"\n")
                    self._buffer = bytearray(rest)
                    if line.lstrip().startswith(b"{"):
                        reply = json.loads(line)
                        if not isinstance(reply, dict):
                            raise RuntimeError("DigUp returned an invalid reply")
                        return reply
                    continue
                if not selector.select(min(0.05, remaining)):
                    continue
                chunk = os.read(self._process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("DigUp worker exited unexpectedly; retry the deep scan")
                self._buffer.extend(chunk)
                if len(self._buffer) > MAX_REPLY_BYTES:
                    raise RuntimeError("DigUp returned an oversized reply")

    def embed_images(
        self, paths: list[str], budget: int = DEFAULT_BUDGET
    ) -> tuple[list[bytes | None], list[str | None]]:
        """Float32 vectors (raw bytes) for each path; None and a message on failure."""
        import numpy as np

        assert self._process.stdin is not None
        self._next_id += 1
        request = {"id": self._next_id, "op": "image", "inputs": paths, "budget": budget}
        try:
            self._process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            self._process.stdin.flush()
            reply = self._read_message()
        except BaseException:
            self.close(force=True)
            raise
        if reply.get("id") != self._next_id:
            raise RuntimeError("DigUp returned a reply for a different request")
        if not reply.get("ok"):
            raise RuntimeError(f"DigUp worker: {reply.get('error')}")
        dim = int(reply.get("dim") or self.dim)
        matrix = np.frombuffer(base64.b64decode(reply["vectors"]), dtype="<f4")
        matrix = matrix.reshape(int(reply["count"]), dim)
        errors = list(reply.get("errors") or [None] * len(paths))
        if len(matrix) != len(paths) or len(errors) != len(paths) or not np.isfinite(matrix).all():
            raise RuntimeError("DigUp returned invalid embedding vectors")
        vectors: list[bytes | None] = []
        for row, error in zip(matrix, errors, strict=True):
            vectors.append(None if error else row.astype("<f4").tobytes())
        return vectors, errors

    def close(self, *, force: bool = False) -> None:
        process = self._process
        if process.poll() is None:
            try:
                if force:
                    raise TimeoutError
                assert process.stdin is not None
                process.stdin.write(b'{"id": 0, "op": "quit"}\n')
                process.stdin.flush()
                process.wait(timeout=0.5)
            except Exception:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

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
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS semantic_content_embeddings (
                content_hash TEXT NOT NULL,
                signature TEXT NOT NULL,
                vector BLOB NOT NULL,
                PRIMARY KEY (content_hash, signature)
            )"""
        )
        self._digests: dict[str, str] = {}
        self._identities: dict[str, str] = {}
        self._conn.commit()

    @staticmethod
    def _identity(record: FileRecord) -> str:
        # Include change time even for callers with an old serialized record.
        # The filesystem identity also prevents a same-size/mtime replacement
        # from reusing a stale embedding.
        stat = Path(record.path).stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        return f"v2|{HashCache._identity(record)}|{identity}"

    @staticmethod
    def _digest(record: FileRecord, cancelled: Callable[[], bool] | None = None) -> str:
        digest = hashlib.sha256()
        with open(record.path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                digest.update(chunk)
        return digest.hexdigest()

    def get_many(
        self, records: list[FileRecord], signature: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, bytes]:
        found: dict[str, bytes] = {}
        by_path = {record.path: record for record in records}
        for record in records:
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            try:
                self._identities[record.path] = self._identity(record)
            except OSError:
                pass  # The preparation pass reports unreadable files individually.
        paths = list(by_path)
        for start in range(0, len(paths), 400):
            chunk = paths[start : start + 400]
            rows = self._conn.execute(
                "SELECT path, identity, signature, vector FROM semantic_embeddings "
                f"WHERE path IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall()
            for path, identity, row_signature, vector in rows:
                if row_signature == signature and identity == self._identities.get(path):
                    found[path] = bytes(vector)
        renamed: list[tuple[FileRecord, bytes]] = []
        for record in records:
            if record.path in found:
                continue
            try:
                digest = self._digest(record, cancelled)
                if self._identity(record) != self._identities.get(record.path):
                    continue
                self._digests[record.path] = digest
            except OSError:
                continue
            row = self._conn.execute(
                "SELECT vector FROM semantic_content_embeddings "
                "WHERE content_hash = ? AND signature = ?", (digest, signature),
            ).fetchone()
            if row:
                found[record.path] = bytes(row[0])
                renamed.append((record, bytes(row[0])))
        if renamed:
            self.put_many(renamed, signature)
        return found

    def put_many(self, items: list[tuple[FileRecord, bytes]], signature: str) -> None:
        for record, vector in items:
            try:
                identity = self._identity(record)
                if record.path in self._identities and identity != self._identities[record.path]:
                    continue
                digest = self._digests.get(record.path) or self._digest(record)
                if identity != self._identity(record):
                    continue
            except OSError:
                continue
            self._conn.execute(
                "INSERT OR REPLACE INTO semantic_embeddings (path, identity, signature, vector) "
                "VALUES (?, ?, ?, ?)", (record.path, identity, signature, vector),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO semantic_content_embeddings "
                "(content_hash, signature, vector) VALUES (?, ?, ?)", (digest, signature, vector),
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
        with EmbeddingWorker(helper, cancelled=cancelled) as worker:
            signature = worker.signature
            if cache is not None:
                vectors.update(cache.get_many(candidates, signature, cancelled))
            pending = [r for r in candidates if r.path not in vectors]
            done = total - len(pending)
            if progress:
                progress("semantic-embed", done, total)
            with (
                tempfile.TemporaryDirectory(prefix="dedupe-semantic-") as tmp,
                ThreadPoolExecutor(max_workers=max(1, workers)) as pool,
            ):

                def prepare(record: FileRecord) -> str | None:
                    check_cancelled()
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
                        try:
                            rows, row_errors = worker.embed_images([p for _r, p in ready])
                        except InterruptedError:
                            raise
                        except (RuntimeError, OSError, ValueError) as exc:
                            # Keep completed batches useful and cached. Failed
                            # files remain pending on a retry of these folders.
                            unfinished = [r for r, _p in ready] + pending[start + len(batch):]
                            errors.extend(
                                f"{ERROR_SEMANTIC_FAILED}: {r.path}: {exc}"[:300]
                                for r in unfinished
                            )
                            break
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
    use_cache: bool = False,
    cache_path: str | Path | None = None,
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
    # Content keys make the comparison snapshot independent of file paths,
    # renames, exact-copy suppression, and the model that produced the bytes.
    paths_by_key: dict[str, list[str]] = {}
    by_key: dict[str, bytes] = {}
    for path in paths:
        key = hashlib.sha256(vectors[path]).hexdigest()
        by_key[key] = vectors[path]
        paths_by_key.setdefault(key, []).append(path)
    keys = sorted(by_key)
    matrix = np.stack([np.frombuffer(by_key[key], dtype="<f4") for key in keys])
    if not np.isfinite(matrix).all():
        raise ValueError("semantic embeddings contain invalid numbers; retry the deep scan")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.maximum(norms, 1e-12)
    sets = same_set or {}
    cache = NeighborCache(cache_path) if use_cache else None
    try:
        known, cached_scores = cache.load(threshold) if cache else (set(), {})
        current = set(keys)
        key_scores = {
            pair: score for pair, score in cached_scores.items()
            if pair[0] in current and pair[1] in current
        }
        pending = [i for i, key in enumerate(keys) if key not in known]
        pending_set = set(pending)
        for start in range(0, len(pending), SIMILARITY_BLOCK):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            indexes = pending[start : start + SIMILARITY_BLOCK]
            # Prior new-vector blocks have already compared these rows.
            # Keep every old vector, but omit those completed new blocks.
            columns = [i for i in range(len(keys)) if i not in pending_set or i >= indexes[0]]
            block = _cosine_block(matrix[indexes], matrix[columns])
            rows, cols = np.nonzero(block >= threshold)
            for row, local_col in zip(rows.tolist(), cols.tolist(), strict=True):
                col = columns[local_col]
                i = indexes[row]
                if col == i or (col in pending_set and col < i):
                    continue
                a, b = sorted((keys[i], keys[col]))
                key_scores[(a, b)] = float(block[row, local_col])
            if progress:
                progress("semantic-cluster", min(len(pending), start + len(indexes)), len(pending))
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        if cache:
            cache.save(keys, key_scores, threshold)

        def add_pair(a: str, b: str, score: float) -> None:
            if a in sets and sets.get(b) == sets[a]:
                return
            a, b = sorted((a, b))
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)
            scores[(a, b)] = score

        for (left, right), score in key_scores.items():
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            for a in paths_by_key[left]:
                for b in paths_by_key[right]:
                    add_pair(a, b, score)
        # Equal vector bytes can belong to different media. Compare their
        # diagonal once rather than omitting new copies of a cached vector.
        for index, key in enumerate(keys):
            siblings = paths_by_key[key]
            score = float(matrix[index] @ matrix[index])
            if len(siblings) > 1 and score >= threshold:
                for i, a in enumerate(siblings):
                    for b in siblings[i + 1:]:
                        add_pair(a, b, score)
        if progress:
            progress("semantic-cluster", len(paths), len(paths))
    finally:
        if cache:
            cache.close()
    return adjacency, scores


def _cosine_block(rows, matrix):
    """One bounded matrix operation, kept separate for comparison-work metrics."""
    return rows @ matrix.T


class NeighborCache:
    """Atomic sparse snapshot of one completely compared set of vectors.

    Missing edges mean below the saved threshold. An added/changed vector is
    compared with every current vector; unchanged pairs reuse those results.
    Lowering the threshold recomputes every pair. Removing or renaming paths
    cannot change coverage because the snapshot is keyed by vector content.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        target = Path(path) if path else default_cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(target), timeout=30.0)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS semantic_neighbor_snapshot "
            "(id INTEGER PRIMARY KEY, threshold REAL, payload TEXT NOT NULL)"
        )
        self._conn.commit()

    def load(self, threshold: float) -> tuple[set[str], dict[tuple[str, str], float]]:
        row = self._conn.execute(
            "SELECT threshold, payload FROM semantic_neighbor_snapshot WHERE id = 1"
        ).fetchone()
        if not row or row[0] > threshold:
            return set(), {}
        try:
            payload = json.loads(row[1])
            if payload.get("version") != 1:
                return set(), {}
            keys = payload["keys"]
            edges = {(keys[a], keys[b]): score for a, b, score in payload["edges"]
                     if score >= threshold}
            return set(keys), edges
        except (ValueError, TypeError, KeyError, IndexError):
            return set(), {}

    def save(self, keys: list[str], scores: dict[tuple[str, str], float], threshold: float) -> None:
        # Very dense libraries can produce huge graphs. Do not turn a useful
        # cache into unbounded disk growth; detection still returns all pairs.
        if len(scores) > 250_000:
            return
        index = {key: i for i, key in enumerate(keys)}
        payload = {"version": 1, "keys": keys,
                   "edges": [[index[a], index[b], score] for (a, b), score in scores.items()]}
        self._conn.execute(
            "INSERT OR REPLACE INTO semantic_neighbor_snapshot VALUES (1, ?, ?)",
            (threshold, json.dumps(payload, separators=(",", ":"))),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


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
    judge: Callable[[FileRecord, FileRecord], float | None] | None = None,
    judge_min: float = 0.8,
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

    ``judge`` (Decider) returns P(same picture) for a candidate pair. With one,
    candidates OpenCV cannot confirm are duplicates when the judge says so with
    ``judge_min``; the brightness fallback applies only to pairs it can't judge.
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
        use_cache=use_cache,
        cache_path=cache_path,
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
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        if score < duplicate_threshold or a in excluded or b in excluded:
            return False
        # Broader discovery must establish image correspondence, not just
        # matching subject matter or a similar brightness distribution.
        verdict = edited_copy_verdict(a, b)
        if verdict is True:
            return True
        if judge is not None:
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            probability = judge(by_path[a], by_path[b])
            if probability is not None:
                return probability >= judge_min
        if verdict is not None:
            return verdict
        left, right = histogram(a), histogram(b)
        return (score >= DEFAULT_DUPLICATE_THRESHOLD and left is not None
                and right is not None and histograms_agree(left, right))

    duplicates = build(restrict(duplicate_pair), distinct)
    del adjacency
    return sets, duplicates, errors
