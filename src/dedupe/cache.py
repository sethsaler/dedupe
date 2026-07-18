"""SQLite cache for hashes keyed by path + size + mtime."""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
                video_fingerprint TEXT,
                duration REAL
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
        }
        for column, declaration in migrations.items():
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE hashes ADD COLUMN {column} {declaration}"
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
                video_fingerprint, duration
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                video_fingerprint=excluded.video_fingerprint,
                duration=excluded.duration
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
                rec.video_fingerprint,
                rec.duration,
            ),
        )

    def commit(self) -> None:
        self._conn.commit()

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
            rec.video_fingerprint = row["video_fingerprint"] or rec.video_fingerprint
            rec.duration = row["duration"] if row["duration"] is not None else rec.duration
            if row["media_type"]:
                try:
                    rec.media_type = MediaType(row["media_type"])
                except ValueError:
                    pass
        return hits

    def store_all(self, records: list[FileRecord]) -> None:
        for rec in records:
            if rec.sha256 or rec.phash or rec.video_fingerprint or rec.partial_hash:
                self.put(rec)
        self.commit()
