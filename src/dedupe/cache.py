"""SQLite cache for hashes and person checks keyed by strong file identity."""

from __future__ import annotations

import json
import sqlite3
from itertools import combinations
from pathlib import Path

from .human_policy import CACHEABLE_HUMAN_STATUSES, MANUALLY_CONFIRMED_HUMAN_STATUS
from .models import FileRecord, MediaType

CACHE_ALGORITHM_VERSION = "dedupe-hashes-v2"


def default_cache_path() -> Path:
    base = Path.home() / ".cache" / "dedupe"
    base.mkdir(parents=True, exist_ok=True)
    return base / "hashes.sqlite3"


class HashCache:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
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
                human_max_confidence REAL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS distinct_similar_pairs (
                path_a TEXT NOT NULL,
                identity_a TEXT NOT NULL,
                path_b TEXT NOT NULL,
                identity_b TEXT NOT NULL,
                PRIMARY KEY (path_a, path_b)
            )
            """
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
        cached = dict(row)
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

    def put(self, rec: FileRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO hashes (
                path, size, mtime, mtime_ns, device, inode, algorithm_version,
                media_type, width, height, sha256, partial_hash, phash, dhash,
                tile_phashes, video_fingerprint, duration, human_detection_status,
                human_detector, human_detection_signature, human_frames_analyzed,
                human_max_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                human_max_confidence=excluded.human_max_confidence
            """,
            (
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
            ),
        )

    def commit(self) -> None:
        self._conn.commit()

    @staticmethod
    def _identity(rec: FileRecord) -> str:
        """Stable file identity used to invalidate reviews when either file changes."""
        return json.dumps(
            [rec.size, rec.mtime_ns, rec.mtime, rec.device, rec.inode],
            separators=(",", ":"),
        )

    def mark_distinct(self, records: list[FileRecord]) -> int:
        """Persist every pair in a reviewed Similar group as intentionally distinct."""
        count = 0
        for left, right in combinations(sorted(records, key=lambda rec: rec.path), 2):
            self._conn.execute(
                """
                INSERT INTO distinct_similar_pairs (
                    path_a, identity_a, path_b, identity_b
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(path_a, path_b) DO UPDATE SET
                    identity_a=excluded.identity_a,
                    identity_b=excluded.identity_b
                """,
                (
                    left.path,
                    self._identity(left),
                    right.path,
                    self._identity(right),
                ),
            )
            count += 1
        self._conn.commit()
        return count

    def distinct_pairs(self, records: list[FileRecord]) -> set[tuple[str, str]]:
        """Return reviewed-distinct pairs whose two file identities still match."""
        by_path = {record.path: record for record in records}
        pairs: set[tuple[str, str]] = set()
        for row in self._conn.execute(
            "SELECT path_a, identity_a, path_b, identity_b FROM distinct_similar_pairs"
        ):
            left = by_path.get(row["path_a"])
            right = by_path.get(row["path_b"])
            if left is None or right is None:
                continue
            if (
                self._identity(left) == row["identity_a"]
                and self._identity(right) == row["identity_b"]
            ):
                pairs.add((left.path, right.path))
        return pairs

    def hydrate(self, records: list[FileRecord]) -> int:
        """Fill records from cache. Returns number of cache hits."""
        hits = 0
        for rec in records:
            row = self.get(rec)
            if not row:
                continue
            hits += 1
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
            if row["media_type"]:
                try:
                    rec.media_type = MediaType(row["media_type"])
                except ValueError:
                    pass
        return hits

    def store_all(self, records: list[FileRecord]) -> None:
        for rec in records:
            has_person_decision = (
                rec.human_detection_status == MANUALLY_CONFIRMED_HUMAN_STATUS
                or (
                    rec.human_detection_status in CACHEABLE_HUMAN_STATUSES
                    and bool(rec.human_detection_signature)
                )
            )
            if (
                rec.sha256
                or rec.phash
                or rec.video_fingerprint
                or rec.partial_hash
                or has_person_decision
            ):
                self.put(rec)
        self.commit()
