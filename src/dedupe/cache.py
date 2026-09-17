"""SQLite cache for hashes and person checks keyed by strong file identity."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import NamedTuple

from .human_policy import CACHEABLE_HUMAN_STATUSES, MANUALLY_CONFIRMED_HUMAN_STATUS
from .models import FileRecord, MediaType

CACHE_ALGORITHM_VERSION = "dedupe-hashes-v2"
# Rows per executemany batch when persisting a scan.
STORE_BATCH_SIZE = 1000
# Records per hydration query batch; each path/device+inode pair is one bind
# variable and SQLite's default variable limit is 999.
HYDRATE_BATCH_SIZE = 400

_UPSERT_SQL = """
    INSERT INTO hashes (
        path, size, mtime, mtime_ns, device, inode, algorithm_version,
        media_type, width, height, sha256, partial_hash, phash, dhash,
        tile_phashes, video_fingerprint, duration, human_detection_status,
        human_detector, human_detection_signature, human_frames_analyzed,
        human_max_confidence, face_count, face_detector, face_detection_signature
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(path) DO UPDATE SET
        size=excluded.size,
        mtime=excluded.mtime,
        mtime_ns=excluded.mtime_ns,
        device=excluded.device,
        inode=excluded.inode,
        algorithm_version=excluded.algorithm_version,
        media_type=excluded.media_type,
        width=excluded.width,
        height=excluded.height,
        sha256=excluded.sha256,
        partial_hash=excluded.partial_hash,
        phash=excluded.phash,
        dhash=excluded.dhash,
        tile_phashes=excluded.tile_phashes,
        video_fingerprint=excluded.video_fingerprint,
        duration=excluded.duration,
        human_detection_status=excluded.human_detection_status,
        human_detector=excluded.human_detector,
        human_detection_signature=excluded.human_detection_signature,
        human_frames_analyzed=excluded.human_frames_analyzed,
        human_max_confidence=excluded.human_max_confidence,
        face_count=excluded.face_count,
        face_detector=excluded.face_detector,
        face_detection_signature=excluded.face_detection_signature
"""


def _upsert_row(rec: FileRecord) -> tuple:
    return (
        rec.path,
        rec.size,
        rec.mtime,
        rec.mtime_ns,
        rec.device,
        rec.inode,
        CACHE_ALGORITHM_VERSION,
        rec.media_type.value,
        rec.width,
        rec.height,
        rec.sha256,
        rec.partial_hash,
        rec.phash,
        rec.dhash,
        rec.tile_phashes,
        rec.video_fingerprint,
        rec.duration,
        rec.human_detection_status,
        rec.human_detector,
        rec.human_detection_signature,
        rec.human_frames_analyzed,
        rec.human_max_confidence,
        rec.face_count,
        rec.face_detector,
        rec.face_detection_signature,
    )


def default_cache_path() -> Path:
    base = Path.home() / ".cache" / "dedupe"
    base.mkdir(parents=True, exist_ok=True)
    return base / "hashes.sqlite3"


def _content_identity(rec: FileRecord) -> str:
    """Perceptual content identity for reviewed-distinct pairs.

    Survives metadata-only drift (mtime/inode/device changes, renames); a real
    content change produces different perceptual hashes and revives the pair
    for review. Empty when the record carries no perceptual hash yet, which
    only ever means "cannot confirm" — never "confirmed distinct".
    """
    if rec.media_type in (MediaType.IMAGE, MediaType.GIF):
        if rec.phash or rec.dhash:
            return (
                f"{CACHE_ALGORITHM_VERSION}|img:{rec.phash or ''}:{rec.dhash or ''}"
            )
    elif rec.media_type == MediaType.VIDEO and rec.video_fingerprint:
        return f"{CACHE_ALGORITHM_VERSION}|vid:{rec.video_fingerprint}"
    return ""


def _parse_identity(identity: str) -> tuple[int, int, int] | None:
    """(device, inode, size) from a stored identity string, when recoverable."""
    try:
        size, _mtime_ns, _mtime, device, inode = json.loads(identity)
    except (ValueError, TypeError):
        return None
    if device is None or inode is None or size is None:
        return None
    return (int(device), int(inode), int(size))


class _DistinctEntry(NamedTuple):
    # True when both sides' stored stat identities match the current records
    # (unchanged files — found at their stored paths or after a pure rename).
    stat_ok: bool
    # Stored perceptual content identity per resolved current path; consulted
    # only when stat_ok is False (metadata drifted) and compared against the
    # records' *current* hashes, which exist by the time matchers ask.
    content: tuple[tuple[str, str], tuple[str, str]]


class DistinctReviews:
    """Reviewed-distinct pairs resolved against one scan's records.

    A pair stays suppressed while both files' stored stat identities still
    match (unchanged files, renamed or not), or while both stored content
    identities match the records' current perceptual hashes (metadata-only
    drift). A real content change fails both checks and the pair may surface
    for review again.
    """

    def __init__(self, entries: dict[tuple[str, str], list[_DistinctEntry]]) -> None:
        self._entries = entries

    def __bool__(self) -> bool:
        return bool(self._entries)

    @classmethod
    def empty(cls) -> DistinctReviews:
        return cls({})

    @classmethod
    def from_pairs(cls, pairs: set[tuple[str, str]]) -> DistinctReviews:
        """Test helper: a resolver that always suppresses the given path pairs."""
        return cls(
            {
                tuple(sorted(pair)): [_DistinctEntry(True, (("", ""), ("", "")))]
                for pair in pairs
            }
        )

    def is_distinct(self, a: FileRecord, b: FileRecord) -> bool:
        for entry in self._entries.get(tuple(sorted((a.path, b.path))), ()):
            if entry.stat_ok:
                return True
            expected = dict(entry.content)
            content_a = expected.get(a.path, "")
            content_b = expected.get(b.path, "")
            if (
                content_a
                and content_b
                and content_a == _content_identity(a)
                and content_b == _content_identity(b)
            ):
                return True
        return False

    def stat_pairs(self) -> set[tuple[str, str]]:
        """Pairs confirmed by stat identity alone (no hash check needed)."""
        return {
            pair
            for pair, entries in self._entries.items()
            if any(entry.stat_ok for entry in entries)
        }


class HashCache:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        # WAL + a busy timeout let independent per-folder scan streams share one
        # cache file concurrently without tripping over "database is locked".
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.DatabaseError:
            pass
        self._closed = False
        self._init()

    def _init(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hashes (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                mtime_ns INTEGER,
                device INTEGER,
                inode INTEGER,
                algorithm_version TEXT NOT NULL DEFAULT '',
                media_type TEXT,
                width INTEGER,
                height INTEGER,
                sha256 TEXT,
                partial_hash TEXT,
                phash TEXT,
                dhash TEXT,
                tile_phashes TEXT,
                video_fingerprint TEXT,
                duration REAL,
                human_detection_status TEXT,
                human_detector TEXT,
                human_detection_signature TEXT,
                human_frames_analyzed INTEGER,
                human_max_confidence REAL,
                face_count INTEGER,
                face_detector TEXT,
                face_detection_signature TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS distinct_similar_pairs (
                path_a TEXT NOT NULL,
                identity_a TEXT NOT NULL,
                content_a TEXT,
                path_b TEXT NOT NULL,
                identity_b TEXT NOT NULL,
                content_b TEXT,
                source TEXT NOT NULL DEFAULT 'verified',
                recorded_at TEXT,
                PRIMARY KEY (path_a, path_b)
            )
            """
        )
        distinct_existing = {
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(distinct_similar_pairs)"
            ).fetchall()
        }
        distinct_migrations = {
            "content_a": "TEXT",
            "content_b": "TEXT",
            "source": "TEXT NOT NULL DEFAULT 'verified'",
            "recorded_at": "TEXT",
        }
        for column, declaration in distinct_migrations.items():
            if column not in distinct_existing:
                self._conn.execute(
                    f"ALTER TABLE distinct_similar_pairs ADD COLUMN {column} {declaration}"
                )
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(hashes)").fetchall()
        }
        migrations = {
            "mtime_ns": "INTEGER",
            "device": "INTEGER",
            "inode": "INTEGER",
            "algorithm_version": "TEXT NOT NULL DEFAULT ''",
            "tile_phashes": "TEXT",
            "human_detection_status": "TEXT",
            "human_detector": "TEXT",
            "human_detection_signature": "TEXT",
            "human_frames_analyzed": "INTEGER",
            "human_max_confidence": "REAL",
            "face_count": "INTEGER",
            "face_detector": "TEXT",
            "face_detection_signature": "TEXT",
        }
        for column, declaration in migrations.items():
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE hashes ADD COLUMN {column} {declaration}"
                )
        # Create this after column migrations so caches from early releases can
        # still open when they predate the identity/version fields.
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS hashes_identity_idx
            ON hashes (algorithm_version, device, inode, size, media_type)
            """
        )
        self._conn.commit()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __del__(self) -> None:
        # Cancellation may unwind a scan before the engine reaches its normal close.
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _validate_row(rec: FileRecord, row) -> dict | None:
        """Check a candidate cache row against the record's current identity."""
        cached = dict(row)
        if cached.get("size") is not None and int(cached["size"]) != rec.size:
            return None
        if rec.mtime_ns is not None and cached.get("mtime_ns") is not None:
            if int(cached["mtime_ns"]) != int(rec.mtime_ns):
                return None
        elif abs(float(cached["mtime"]) - rec.mtime) >= 0.001:
            return None
        for key in ("device", "inode"):
            current = getattr(rec, key)
            prior = cached.get(key)
            if current is not None and prior is not None and int(current) != int(prior):
                return None
        return cached

    def get(self, rec: FileRecord) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM hashes WHERE path = ? AND size = ? AND algorithm_version = ?",
            (rec.path, rec.size, CACHE_ALGORITHM_VERSION),
        ).fetchone()
        if not row and rec.device is not None and rec.inode is not None:
            # Paths are not file identity. Reuse work after a rename or move on
            # the same filesystem, while requiring metadata and media type to
            # prevent an inode-reuse or extension-change false hit.
            row = self._conn.execute(
                """
                SELECT * FROM hashes
                WHERE size = ? AND algorithm_version = ? AND device = ?
                    AND inode = ? AND media_type = ?
                ORDER BY path
                LIMIT 1
                """,
                (
                    rec.size,
                    CACHE_ALGORITHM_VERSION,
                    rec.device,
                    rec.inode,
                    rec.media_type.value,
                ),
            ).fetchone()
        if not row:
            return None
        return self._validate_row(rec, row)

    def commit(self) -> None:
        self._conn.commit()

    @staticmethod
    def _identity(rec: FileRecord) -> str:
        """Stable file identity used to invalidate reviews when either file changes."""
        # Byte-for-byte the old json.dumps(..., separators=(",", ":")) output so
        # identities recorded by earlier versions still compare equal.
        return "[{},{},{},{},{}]".format(
            *(
                "null" if value is None else repr(value)
                for value in (rec.size, rec.mtime_ns, rec.mtime, rec.device, rec.inode)
            )
        )

    def mark_distinct(
        self, records: list[FileRecord], *, source: str = "verified"
    ) -> int:
        """Persist every pair in a reviewed Similar group as intentionally distinct.

        ``source`` records how the pair was decided: ``verified`` for pairs
        the user compared side by side (or explicitly confirmed as a whole
        group), ``inferred`` for the pairs backfilled at dissolution that
        were never shown together. A re-mark never downgrades a verified
        row to inferred.
        """
        stamp = datetime.now(UTC).isoformat()
        count = 0
        for left, right in combinations(sorted(records, key=lambda rec: rec.path), 2):
            self._conn.execute(
                """
                INSERT INTO distinct_similar_pairs (
                    path_a, identity_a, content_a, path_b, identity_b, content_b,
                    source, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path_a, path_b) DO UPDATE SET
                    identity_a=excluded.identity_a,
                    content_a=excluded.content_a,
                    identity_b=excluded.identity_b,
                    content_b=excluded.content_b,
                    source=CASE
                        WHEN excluded.source = 'verified' THEN 'verified'
                        ELSE distinct_similar_pairs.source END,
                    recorded_at=excluded.recorded_at
                """,
                (
                    left.path,
                    self._identity(left),
                    _content_identity(left),
                    right.path,
                    self._identity(right),
                    _content_identity(right),
                    source,
                    stamp,
                ),
            )
            count += 1
        self._conn.commit()
        return count

    def unmark_distinct_pair(
        self,
        path_a: str,
        path_b: str,
        records: list[FileRecord] | None = None,
    ) -> int:
        """Drop one recorded distinct pair (undo of a pair-level review).

        The files may have been renamed since the review; when no row matches
        the given paths, fall back to the current files' stored identities.
        """
        left, right = sorted((path_a, path_b))
        cursor = self._conn.execute(
            "DELETE FROM distinct_similar_pairs WHERE path_a = ? AND path_b = ?",
            (left, right),
        )
        removed = cursor.rowcount
        if not removed and records:
            wanted = {
                self._identity(rec)
                for rec in records
                if rec.path in (left, right)
            }
            if len(wanted) == 2:
                stale = [
                    (row["path_a"], row["path_b"])
                    for row in self._conn.execute(
                        "SELECT path_a, identity_a, path_b, identity_b "
                        "FROM distinct_similar_pairs"
                    )
                    if row["identity_a"] in wanted and row["identity_b"] in wanted
                ]
                for stored_a, stored_b in stale:
                    removed += self._conn.execute(
                        "DELETE FROM distinct_similar_pairs "
                        "WHERE path_a = ? AND path_b = ?",
                        (stored_a, stored_b),
                    ).rowcount
        self._conn.commit()
        return removed

    def list_distinct_pairs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        path_contains: str | None = None,
    ) -> tuple[list[dict], int]:
        """Recorded distinct pairs for the decisions view, newest first.

        ``path_contains`` filters with a plain case-insensitive substring
        against either side's path. Returns ``(rows, total)``; legacy rows
        without a timestamp sort last.
        """
        where = ""
        params: list = []
        if path_contains:
            where = " WHERE instr(lower(path_a), ?) > 0 OR instr(lower(path_b), ?) > 0"
            params = [path_contains.lower(), path_contains.lower()]
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM distinct_similar_pairs{where}", params
        ).fetchone()[0]
        rows = self._conn.execute(
            f"""
            SELECT path_a, path_b, source, recorded_at
            FROM distinct_similar_pairs{where}
            ORDER BY (recorded_at IS NULL), recorded_at DESC, path_a, path_b
            LIMIT ? OFFSET ?
            """,
            [*params, max(0, int(limit)), max(0, int(offset))],
        ).fetchall()
        return (
            [
                {
                    "path_a": row["path_a"],
                    "path_b": row["path_b"],
                    "source": row["source"] or "verified",
                    "recorded_at": row["recorded_at"],
                }
                for row in rows
            ],
            int(total),
        )

    def backfill_distinct_content(self, records: list[FileRecord]) -> int:
        """Fill perceptual content identities onto rows recorded before hashing.

        Rows written while a file carried no perceptual hash (legacy caches,
        decisions recorded from unhashed loaded sessions) are stat-only: any
        metadata touch revives the pair for review. Once a scan has hashed
        the file again, the row can carry the content identity and survive
        drift. Only sides whose stored stat identity still matches the
        current record are backfilled — a changed file must not have its
        decision hardened. Returns the number of rows updated.
        """
        by_path: dict[str, FileRecord] = {}
        by_identity: dict[str, FileRecord] = {}
        for rec in records:
            by_path[rec.path] = rec
            by_identity.setdefault(self._identity(rec), rec)
        updated = 0
        rows = self._conn.execute(
            "SELECT path_a, identity_a, content_a, path_b, identity_b, content_b "
            "FROM distinct_similar_pairs "
            "WHERE content_a IS NULL OR content_a = '' "
            "   OR content_b IS NULL OR content_b = ''"
        ).fetchall()
        for row in rows:
            fills: dict[str, str] = {}
            for path_key, identity_key, content_key in (
                ("path_a", "identity_a", "content_a"),
                ("path_b", "identity_b", "content_b"),
            ):
                if row[content_key]:
                    continue
                rec = by_path.get(row[path_key]) or by_identity.get(row[identity_key])
                # Renames keep the stored identity; drift does not. Only an
                # identity match proves the hashes describe the decided file.
                if rec is None or self._identity(rec) != row[identity_key]:
                    continue
                content = _content_identity(rec)
                if content:
                    fills[content_key] = content
            if fills:
                self._conn.execute(
                    "UPDATE distinct_similar_pairs "
                    "SET content_a = COALESCE(?, content_a), "
                    "    content_b = COALESCE(?, content_b) "
                    "WHERE path_a = ? AND path_b = ?",
                    (
                        fills.get("content_a"),
                        fills.get("content_b"),
                        row["path_a"],
                        row["path_b"],
                    ),
                )
                updated += 1
        if updated:
            self._conn.commit()
        return updated

    def distinct_reviews(self, records: list[FileRecord]) -> DistinctReviews:
        """Resolve reviewed-distinct rows against one scan's records.

        Each side of a stored row is found by path, then by its full stored
        identity (a pure rename/move keeps it), then by device+inode+size
        (rename combined with metadata drift — the content check downstream
        still has to confirm before the pair is suppressed).
        """
        by_path: dict[str, FileRecord] = {}
        by_identity: dict[str, FileRecord] = {}
        by_inode: dict[tuple[int, int, int], FileRecord] = {}
        for rec in records:
            by_path[rec.path] = rec
            by_identity[self._identity(rec)] = rec
            if rec.device is not None and rec.inode is not None:
                by_inode.setdefault((rec.device, rec.inode, rec.size), rec)

        entries: dict[tuple[str, str], list[_DistinctEntry]] = {}
        for row in self._conn.execute(
            "SELECT path_a, identity_a, content_a, path_b, identity_b, content_b "
            "FROM distinct_similar_pairs"
        ):
            sides = []
            for path, identity in (
                (row["path_a"], row["identity_a"]),
                (row["path_b"], row["identity_b"]),
            ):
                rec = by_path.get(path) or by_identity.get(identity)
                if rec is None:
                    parsed = _parse_identity(identity)
                    if parsed is not None:
                        rec = by_inode.get(parsed)
                sides.append((rec, identity))
            (rec_a, identity_a), (rec_b, identity_b) = sides
            if rec_a is None or rec_b is None:
                continue
            stat_ok = (
                self._identity(rec_a) == identity_a
                and self._identity(rec_b) == identity_b
            )
            key = tuple(sorted((rec_a.path, rec_b.path)))
            entries.setdefault(key, []).append(
                _DistinctEntry(
                    stat_ok,
                    (
                        (rec_a.path, row["content_a"] or ""),
                        (rec_b.path, row["content_b"] or ""),
                    ),
                )
            )
        return DistinctReviews(entries)

    def distinct_pairs(self, records: list[FileRecord]) -> set[tuple[str, str]]:
        """Reviewed-distinct pairs confirmed by stat identity alone."""
        return self.distinct_reviews(records).stat_pairs()

    @staticmethod
    def _apply_row(rec: FileRecord, row: dict) -> None:
        rec.width = row["width"] if row["width"] is not None else rec.width
        rec.height = row["height"] if row["height"] is not None else rec.height
        rec.sha256 = row["sha256"] or rec.sha256
        rec.partial_hash = row["partial_hash"] or rec.partial_hash
        rec.phash = row["phash"] or rec.phash
        rec.dhash = row["dhash"] or rec.dhash
        rec.tile_phashes = row["tile_phashes"] or rec.tile_phashes
        rec.video_fingerprint = row["video_fingerprint"] or rec.video_fingerprint
        rec.duration = row["duration"] if row["duration"] is not None else rec.duration
        rec.human_detection_status = (
            row["human_detection_status"] or rec.human_detection_status
        )
        rec.human_detector = row["human_detector"] or rec.human_detector
        rec.human_detection_signature = (
            row["human_detection_signature"] or rec.human_detection_signature
        )
        rec.human_frames_analyzed = (
            row["human_frames_analyzed"]
            if row["human_frames_analyzed"] is not None
            else rec.human_frames_analyzed
        )
        rec.human_max_confidence = (
            row["human_max_confidence"]
            if row["human_max_confidence"] is not None
            else rec.human_max_confidence
        )
        rec.face_count = (
            row["face_count"] if row["face_count"] is not None else rec.face_count
        )
        rec.face_detector = row["face_detector"] or rec.face_detector
        rec.face_detection_signature = (
            row["face_detection_signature"] or rec.face_detection_signature
        )
        if row["media_type"]:
            try:
                rec.media_type = MediaType(row["media_type"])
            except ValueError:
                pass

    def hydrate(self, records: list[FileRecord]) -> int:
        """Fill records from cache. Returns number of cache hits.

        Batched: one chunked ``IN`` query per ~400 paths (plus one batched
        device/inode fallback pass for path misses) instead of 1–2 round trips
        per record, which dominates scan startup on 10k+ file libraries.
        """
        rows_by_path: dict[str, dict] = {}
        paths = [rec.path for rec in records]
        for start in range(0, len(paths), HYDRATE_BATCH_SIZE):
            chunk = paths[start : start + HYDRATE_BATCH_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            for row in self._conn.execute(
                f"SELECT * FROM hashes WHERE algorithm_version = ? "
                f"AND path IN ({placeholders})",
                (CACHE_ALGORITHM_VERSION, *chunk),
            ):
                rows_by_path[row["path"]] = row

        hits = 0
        misses: list[FileRecord] = []
        for rec in records:
            row = rows_by_path.get(rec.path)
            cached = self._validate_row(rec, row) if row is not None else None
            if cached is None:
                misses.append(rec)
                continue
            hits += 1
            self._apply_row(rec, cached)

        # Identity fallback for path misses: reuse hashes after a rename/move
        # on the same filesystem (same device+inode, same size and media type).
        fallback_candidates = [
            rec for rec in misses if rec.device is not None and rec.inode is not None
        ]
        if fallback_candidates:
            identities: dict[tuple, list[dict]] = {}
            for start in range(0, len(fallback_candidates), HYDRATE_BATCH_SIZE):
                chunk = fallback_candidates[start : start + HYDRATE_BATCH_SIZE]
                pairs = ",".join("(?,?)" for _ in chunk)
                params: list = [CACHE_ALGORITHM_VERSION]
                for rec in chunk:
                    params.extend((rec.device, rec.inode))
                for row in self._conn.execute(
                    f"SELECT * FROM hashes WHERE algorithm_version = ? "
                    f"AND (device, inode) IN ({pairs}) ORDER BY path",
                    params,
                ):
                    identities.setdefault((row["device"], row["inode"]), []).append(
                        dict(row)
                    )
            for rec in fallback_candidates:
                candidates = identities.get((rec.device, rec.inode), [])
                for row in candidates:
                    if row["media_type"] != rec.media_type.value:
                        continue
                    cached = self._validate_row(rec, row)
                    if cached is None:
                        continue
                    hits += 1
                    self._apply_row(rec, cached)
                    break
        return hits

    def store_all(self, records: list[FileRecord]) -> None:
        rows: list[tuple] = []
        for rec in records:
            has_person_decision = (
                rec.human_detection_status == MANUALLY_CONFIRMED_HUMAN_STATUS
                or (
                    rec.human_detection_status in CACHEABLE_HUMAN_STATUSES
                    and bool(rec.human_detection_signature)
                )
            )
            has_face_count = (
                rec.face_count is not None and bool(rec.face_detection_signature)
            )
            if (
                rec.sha256
                or rec.phash
                or rec.video_fingerprint
                or rec.partial_hash
                or (rec.width and rec.height)
                or has_person_decision
                or has_face_count
            ):
                rows.append(_upsert_row(rec))
        # One statement per batch instead of one round trip per record.
        for start in range(0, len(rows), STORE_BATCH_SIZE):
            self._conn.executemany(_UPSERT_SQL, rows[start : start + STORE_BATCH_SIZE])
        self.commit()
