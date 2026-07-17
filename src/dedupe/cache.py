"""SQLite cache for hashes keyed by path + size + mtime."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import FileRecord, MediaType


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
        self._init()

    def _init(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hashes (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
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
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get(self, path: str, size: int, mtime: float) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM hashes WHERE path = ? AND size = ? AND ABS(mtime - ?) < 0.001",
            (path, size, mtime),
        ).fetchone()
        return dict(row) if row else None

    def put(self, rec: FileRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO hashes (
                path, size, mtime, media_type, width, height,
                sha256, partial_hash, phash, dhash, video_fingerprint, duration
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size=excluded.size,
                mtime=excluded.mtime,
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
            row = self.get(rec.path, rec.size, rec.mtime)
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
