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
CACHE_SCHEMA_VERSION = 3
# Rows per executemany batch when persisting a scan.
STORE_BATCH_SIZE = 1000
# Records per hydration query batch; each path is one bind variable and SQLite's
# legacy variable limit is 999.
HYDRATE_BATCH_SIZE = 400
# Identity fallback binds 4 values per record plus algorithm_version.
IDENTITY_HYDRATE_BATCH_SIZE = 200
# Content cache binds 2 values (size, sha256) per record.
CONTENT_BATCH_SIZE = 400
# Regional tile hashes per still frame: four quadrants and the center.
TILES_PER_FRAME = 5
JPEG_EXTS = frozenset({".jpg", ".jpeg"})


def _is_multi_frame_jpeg_hash(rec: FileRecord) -> bool:
    """A JPEG is never an animation, so cached tiles for several frames are stale."""
    if rec.extension not in JPEG_EXTS or not rec.tile_phashes:
        return False
    values = rec.tile_phashes.split(":", 1)[-1]
    return len([part for part in values.split(",") if part]) > TILES_PER_FRAME


_HASH_COLUMNS = (
    "path",
    "size",
    "mtime",
    "mtime_ns",
    "device",
    "inode",
    "algorithm_version",
    "media_type",
    "width",
    "height",
    "sha256",
    "partial_hash",
    "phash",
    "dhash",
    "tile_phashes",
    "orientation_phashes",
    "video_fingerprint",
    "duration",
    "human_detection_status",
    "human_detector",
    "human_detection_signature",
    "human_frames_analyzed",
    "human_max_confidence",
    "face_count",
    "male_face_count",
    "female_face_count",
    "face_detector",
    "face_detection_signature",
    "ctime_ns",
    "people_score",
    "people_score_signature",
    "non_person_coverage",
    "non_person_coverage_signature",
    "border_crop",
    "border_signature",
    "border_crop_conservative",
    "border_crop_aggressive",
    "border_levels_signature",
    "capture_metadata",
    "sharpness",
    "video_codec",
    "video_bitrate",
    "has_audio",
)

_IDENTITY_UNCHANGED_SQL = (
    "hashes.algorithm_version = excluded.algorithm_version "
    "AND hashes.size = excluded.size "
    "AND ("
    "    (hashes.mtime_ns IS NOT NULL AND excluded.mtime_ns IS NOT NULL "
    "     AND hashes.mtime_ns = excluded.mtime_ns) "
    "    OR ((hashes.mtime_ns IS NULL OR excluded.mtime_ns IS NULL) "
    "        AND abs(hashes.mtime - excluded.mtime) < 0.001)"
    ") "
    "AND hashes.ctime_ns IS excluded.ctime_ns "
    "AND hashes.device IS excluded.device "
    "AND hashes.inode IS excluded.inode"
)


def _coalesce_if_unchanged(col: str, guard_col: str | None = None) -> str:
    if guard_col is None:
        return (
            f"{col}=CASE WHEN ({_IDENTITY_UNCHANGED_SQL}) "
            f"THEN COALESCE(excluded.{col}, hashes.{col}) ELSE excluded.{col} END"
        )
    return (
        f"{col}=CASE WHEN ({_IDENTITY_UNCHANGED_SQL}) AND excluded.{guard_col} IS NULL "
        f"THEN hashes.{col} ELSE excluded.{col} END"
    )


_UPSERT_SQL = f"""
    INSERT INTO hashes (
        {", ".join(_HASH_COLUMNS)}
    ) VALUES ({", ".join("?" for _ in _HASH_COLUMNS)})
    ON CONFLICT(path) DO UPDATE SET
        size=excluded.size,
        mtime=excluded.mtime,
        mtime_ns=excluded.mtime_ns,
        ctime_ns=excluded.ctime_ns,
        device=excluded.device,
        inode=excluded.inode,
        algorithm_version=excluded.algorithm_version,
        media_type=excluded.media_type,
        width=CASE WHEN ({_IDENTITY_UNCHANGED_SQL})
            THEN COALESCE(excluded.width, hashes.width) ELSE excluded.width END,
        height=CASE WHEN ({_IDENTITY_UNCHANGED_SQL})
            THEN COALESCE(excluded.height, hashes.height) ELSE excluded.height END,
        sha256=CASE WHEN ({_IDENTITY_UNCHANGED_SQL})
            THEN COALESCE(excluded.sha256, hashes.sha256) ELSE excluded.sha256 END,
        partial_hash=CASE WHEN ({_IDENTITY_UNCHANGED_SQL})
            THEN COALESCE(excluded.partial_hash, hashes.partial_hash)
            ELSE excluded.partial_hash END,
        phash=excluded.phash,
        dhash=excluded.dhash,
        tile_phashes=excluded.tile_phashes,
        orientation_phashes=excluded.orientation_phashes,
        video_fingerprint=excluded.video_fingerprint,
        duration=CASE WHEN ({_IDENTITY_UNCHANGED_SQL})
            THEN COALESCE(excluded.duration, hashes.duration) ELSE excluded.duration END,
        {_coalesce_if_unchanged("human_detection_status")},
        {_coalesce_if_unchanged("human_detector", "human_detection_status")},
        {_coalesce_if_unchanged("human_detection_signature", "human_detection_status")},
        {_coalesce_if_unchanged("human_frames_analyzed", "human_detection_status")},
        {_coalesce_if_unchanged("human_max_confidence", "human_detection_status")},
        {_coalesce_if_unchanged("face_count")},
        {_coalesce_if_unchanged("male_face_count", "face_count")},
        {_coalesce_if_unchanged("female_face_count", "face_count")},
        {_coalesce_if_unchanged("face_detector", "face_count")},
        {_coalesce_if_unchanged("face_detection_signature", "face_count")},
        {_coalesce_if_unchanged("people_score")},
        {_coalesce_if_unchanged("people_score_signature", "people_score")},
        {_coalesce_if_unchanged("non_person_coverage")},
        {_coalesce_if_unchanged("non_person_coverage_signature", "non_person_coverage")},
        {_coalesce_if_unchanged("border_crop")},
        {_coalesce_if_unchanged("border_signature", "border_crop")},
        {_coalesce_if_unchanged("border_crop_conservative", "border_levels_signature")},
        {_coalesce_if_unchanged("border_crop_aggressive", "border_levels_signature")},
        {_coalesce_if_unchanged("border_levels_signature")},
        {_coalesce_if_unchanged("capture_metadata")},
        {_coalesce_if_unchanged("sharpness")},
        {_coalesce_if_unchanged("video_codec")},
        {_coalesce_if_unchanged("video_bitrate")},
        {_coalesce_if_unchanged("has_audio")}
"""

_CONTENT_COLUMNS = (
    "size",
    "sha256",
    "algorithm_version",
    "media_type",
    "width",
    "height",
    "partial_hash",
    "phash",
    "dhash",
    "tile_phashes",
    "orientation_phashes",
    "video_fingerprint",
    "duration",
    "human_detection_status",
    "human_detector",
    "human_detection_signature",
    "human_frames_analyzed",
    "human_max_confidence",
    "face_count",
    "male_face_count",
    "female_face_count",
    "face_detector",
    "face_detection_signature",
    "people_score",
    "people_score_signature",
    "non_person_coverage",
    "non_person_coverage_signature",
    "border_crop",
    "border_signature",
    "border_crop_conservative",
    "border_crop_aggressive",
    "border_levels_signature",
    "capture_metadata",
    "sharpness",
    "video_codec",
    "video_bitrate",
    "has_audio",
)


def _content_coalesce(col: str, guard_col: str | None = None) -> str:
    same_ver = "content_cache.algorithm_version = excluded.algorithm_version"
    if guard_col is None:
        return (
            f"{col}=CASE WHEN ({same_ver}) "
            f"THEN COALESCE(excluded.{col}, content_cache.{col}) ELSE excluded.{col} END"
        )
    return (
        f"{col}=CASE WHEN ({same_ver}) AND excluded.{guard_col} IS NULL "
        f"THEN content_cache.{col} ELSE excluded.{col} END"
    )


_CONTENT_UPSERT_SQL = f"""
    INSERT INTO content_cache (
        {", ".join(_CONTENT_COLUMNS)}
    ) VALUES ({", ".join("?" for _ in _CONTENT_COLUMNS)})
    ON CONFLICT(size, sha256) DO UPDATE SET
        algorithm_version=excluded.algorithm_version,
        media_type=CASE WHEN content_cache.algorithm_version = excluded.algorithm_version
            THEN COALESCE(excluded.media_type, content_cache.media_type)
            ELSE excluded.media_type END,
        width=CASE WHEN content_cache.algorithm_version = excluded.algorithm_version
            THEN COALESCE(excluded.width, content_cache.width) ELSE excluded.width END,
        height=CASE WHEN content_cache.algorithm_version = excluded.algorithm_version
            THEN COALESCE(excluded.height, content_cache.height) ELSE excluded.height END,
        partial_hash=CASE WHEN content_cache.algorithm_version = excluded.algorithm_version
            THEN COALESCE(excluded.partial_hash, content_cache.partial_hash)
            ELSE excluded.partial_hash END,
        phash=CASE WHEN content_cache.algorithm_version = excluded.algorithm_version
            THEN COALESCE(excluded.phash, content_cache.phash) ELSE excluded.phash END,
        dhash=CASE WHEN content_cache.algorithm_version = excluded.algorithm_version
            THEN COALESCE(excluded.dhash, content_cache.dhash) ELSE excluded.dhash END,
        tile_phashes=CASE WHEN content_cache.algorithm_version = excluded.algorithm_version
            THEN COALESCE(excluded.tile_phashes, content_cache.tile_phashes)
            ELSE excluded.tile_phashes END,
        orientation_phashes=CASE WHEN content_cache.algorithm_version = excluded.algorithm_version
            THEN COALESCE(excluded.orientation_phashes, content_cache.orientation_phashes)
            ELSE excluded.orientation_phashes END,
        video_fingerprint=CASE WHEN content_cache.algorithm_version = excluded.algorithm_version
            THEN COALESCE(excluded.video_fingerprint, content_cache.video_fingerprint)
            ELSE excluded.video_fingerprint END,
        duration=CASE WHEN content_cache.algorithm_version = excluded.algorithm_version
            THEN COALESCE(excluded.duration, content_cache.duration)
            ELSE excluded.duration END,
        {_content_coalesce("human_detection_status")},
        {_content_coalesce("human_detector", "human_detection_status")},
        {_content_coalesce("human_detection_signature", "human_detection_status")},
        {_content_coalesce("human_frames_analyzed", "human_detection_status")},
        {_content_coalesce("human_max_confidence", "human_detection_status")},
        {_content_coalesce("face_count")},
        {_content_coalesce("male_face_count", "face_count")},
        {_content_coalesce("female_face_count", "face_count")},
        {_content_coalesce("face_detector", "face_count")},
        {_content_coalesce("face_detection_signature", "face_count")},
        {_content_coalesce("people_score")},
        {_content_coalesce("people_score_signature", "people_score")},
        {_content_coalesce("non_person_coverage")},
        {_content_coalesce("non_person_coverage_signature", "non_person_coverage")},
        {_content_coalesce("border_crop")},
        {_content_coalesce("border_signature", "border_crop")},
        {_content_coalesce("border_crop_conservative", "border_levels_signature")},
        {_content_coalesce("border_crop_aggressive", "border_levels_signature")},
        {_content_coalesce("border_levels_signature")},
        {_content_coalesce("capture_metadata")},
        {_content_coalesce("sharpness")},
        {_content_coalesce("video_codec")},
        {_content_coalesce("video_bitrate")},
        {_content_coalesce("has_audio")}
"""


def _opt_bool_int(value: object) -> int | None:
    return None if value is None else int(bool(value))


def _opt_int(value: object) -> int | None:
    return None if value is None else int(value)


def _opt_float(value: object) -> float | None:
    return None if value is None else float(value)


def _upsert_row(rec: FileRecord) -> tuple:
    return (
        rec.path,
        int(rec.size),
        float(rec.mtime),
        _opt_int(rec.mtime_ns),
        _opt_int(rec.device),
        _opt_int(rec.inode),
        CACHE_ALGORITHM_VERSION,
        rec.media_type.value,
        _opt_int(rec.width),
        _opt_int(rec.height),
        rec.sha256,
        rec.partial_hash,
        rec.phash,
        rec.dhash,
        rec.tile_phashes,
        rec.orientation_phashes,
        rec.video_fingerprint,
        _opt_float(rec.duration),
        rec.human_detection_status,
        rec.human_detector,
        rec.human_detection_signature,
        _opt_int(rec.human_frames_analyzed),
        _opt_float(rec.human_max_confidence),
        _opt_int(rec.face_count),
        _opt_int(rec.male_face_count),
        _opt_int(rec.female_face_count),
        rec.face_detector,
        rec.face_detection_signature,
        _opt_int(rec.ctime_ns),
        _opt_float(rec.people_score),
        rec.people_score_signature,
        _opt_float(rec.non_person_coverage),
        rec.non_person_coverage_signature,
        rec.border_crop,
        rec.border_signature,
        rec.border_crop_conservative,
        rec.border_crop_aggressive,
        rec.border_levels_signature,
        _opt_bool_int(rec.capture_metadata),
        _opt_float(rec.sharpness),
        rec.video_codec,
        _opt_int(rec.video_bitrate),
        _opt_bool_int(rec.has_audio),
    )


def _row_snapshot(row: dict) -> tuple:
    """Canonical snapshot matching ``_upsert_row`` for dirty-row comparison."""
    return (
        row["path"],
        int(row["size"]),
        float(row["mtime"]),
        _opt_int(row.get("mtime_ns")),
        _opt_int(row.get("device")),
        _opt_int(row.get("inode")),
        row.get("algorithm_version") or "",
        row.get("media_type"),
        _opt_int(row.get("width")),
        _opt_int(row.get("height")),
        row.get("sha256"),
        row.get("partial_hash"),
        row.get("phash"),
        row.get("dhash"),
        row.get("tile_phashes"),
        row.get("orientation_phashes"),
        row.get("video_fingerprint"),
        _opt_float(row.get("duration")),
        row.get("human_detection_status"),
        row.get("human_detector"),
        row.get("human_detection_signature"),
        _opt_int(row.get("human_frames_analyzed")),
        _opt_float(row.get("human_max_confidence")),
        _opt_int(row.get("face_count")),
        _opt_int(row.get("male_face_count")),
        _opt_int(row.get("female_face_count")),
        row.get("face_detector"),
        row.get("face_detection_signature"),
        _opt_int(row.get("ctime_ns")),
        _opt_float(row.get("people_score")),
        row.get("people_score_signature"),
        _opt_float(row.get("non_person_coverage")),
        row.get("non_person_coverage_signature"),
        row.get("border_crop"),
        row.get("border_signature"),
        row.get("border_crop_conservative"),
        row.get("border_crop_aggressive"),
        row.get("border_levels_signature"),
        _opt_bool_int(row.get("capture_metadata")),
        _opt_float(row.get("sharpness")),
        row.get("video_codec"),
        _opt_int(row.get("video_bitrate")),
        _opt_bool_int(row.get("has_audio")),
    )


def _content_row(rec: FileRecord) -> tuple:
    return (
        int(rec.size),
        rec.sha256,
        CACHE_ALGORITHM_VERSION,
        rec.media_type.value,
        _opt_int(rec.width),
        _opt_int(rec.height),
        rec.partial_hash,
        rec.phash,
        rec.dhash,
        rec.tile_phashes,
        rec.orientation_phashes,
        rec.video_fingerprint,
        _opt_float(rec.duration),
        rec.human_detection_status,
        rec.human_detector,
        rec.human_detection_signature,
        _opt_int(rec.human_frames_analyzed),
        _opt_float(rec.human_max_confidence),
        _opt_int(rec.face_count),
        _opt_int(rec.male_face_count),
        _opt_int(rec.female_face_count),
        rec.face_detector,
        rec.face_detection_signature,
        _opt_float(rec.people_score),
        rec.people_score_signature,
        _opt_float(rec.non_person_coverage),
        rec.non_person_coverage_signature,
        rec.border_crop,
        rec.border_signature,
        rec.border_crop_conservative,
        rec.border_crop_aggressive,
        rec.border_levels_signature,
        _opt_bool_int(rec.capture_metadata),
        _opt_float(rec.sharpness),
        rec.video_codec,
        _opt_int(rec.video_bitrate),
        _opt_bool_int(rec.has_audio),
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
        # WAL + NORMAL sync + busy timeout let independent scan streams share
        # one cache file concurrently without tripping over "database is locked".
        try:
            mode_row = self._conn.execute("PRAGMA journal_mode").fetchone()
            mode = str(mode_row[0]).lower() if mode_row else ""
            if mode not in ("wal", "memory"):
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.DatabaseError:
            pass
        self._closed = False
        self._hydrated_snapshots: dict[str, tuple] = {}
        self._content_snapshots: dict[tuple[int, str], tuple] = {}
        self._init()

    def _init(self) -> None:
        version_row = self._conn.execute("PRAGMA user_version").fetchone()
        if version_row is not None and int(version_row[0]) == CACHE_SCHEMA_VERSION:
            return
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hashes (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                mtime_ns INTEGER,
                ctime_ns INTEGER,
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
                orientation_phashes TEXT,
                video_fingerprint TEXT,
                duration REAL,
                human_detection_status TEXT,
                human_detector TEXT,
                human_detection_signature TEXT,
                human_frames_analyzed INTEGER,
                human_max_confidence REAL,
                face_count INTEGER,
                male_face_count INTEGER,
                female_face_count INTEGER,
                face_detector TEXT,
                face_detection_signature TEXT,
                people_score REAL,
                people_score_signature TEXT,
                non_person_coverage REAL,
                non_person_coverage_signature TEXT,
                border_crop TEXT,
                border_signature TEXT,
                border_crop_conservative TEXT,
                border_crop_aggressive TEXT,
                border_levels_signature TEXT,
                capture_metadata INTEGER,
                sharpness REAL,
                video_codec TEXT,
                video_bitrate INTEGER,
                has_audio INTEGER
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content_cache (
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                algorithm_version TEXT NOT NULL DEFAULT '',
                media_type TEXT,
                width INTEGER,
                height INTEGER,
                partial_hash TEXT,
                phash TEXT,
                dhash TEXT,
                tile_phashes TEXT,
                orientation_phashes TEXT,
                video_fingerprint TEXT,
                duration REAL,
                human_detection_status TEXT,
                human_detector TEXT,
                human_detection_signature TEXT,
                human_frames_analyzed INTEGER,
                human_max_confidence REAL,
                face_count INTEGER,
                male_face_count INTEGER,
                female_face_count INTEGER,
                face_detector TEXT,
                face_detection_signature TEXT,
                people_score REAL,
                people_score_signature TEXT,
                non_person_coverage REAL,
                non_person_coverage_signature TEXT,
                border_crop TEXT,
                border_signature TEXT,
                border_crop_conservative TEXT,
                border_crop_aggressive TEXT,
                border_levels_signature TEXT,
                capture_metadata INTEGER,
                sharpness REAL,
                video_codec TEXT,
                video_bitrate INTEGER,
                has_audio INTEGER,
                PRIMARY KEY (size, sha256)
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
            "ctime_ns": "INTEGER",
            "device": "INTEGER",
            "inode": "INTEGER",
            "algorithm_version": "TEXT NOT NULL DEFAULT ''",
            "tile_phashes": "TEXT",
            "orientation_phashes": "TEXT",
            "human_detection_status": "TEXT",
            "human_detector": "TEXT",
            "human_detection_signature": "TEXT",
            "human_frames_analyzed": "INTEGER",
            "human_max_confidence": "REAL",
            "face_count": "INTEGER",
            "male_face_count": "INTEGER",
            "female_face_count": "INTEGER",
            "face_detector": "TEXT",
            "face_detection_signature": "TEXT",
            "people_score": "REAL",
            "people_score_signature": "TEXT",
            "non_person_coverage": "REAL",
            "non_person_coverage_signature": "TEXT",
            "border_crop": "TEXT",
            "border_signature": "TEXT",
            "border_crop_conservative": "TEXT",
            "border_crop_aggressive": "TEXT",
            "border_levels_signature": "TEXT",
            "capture_metadata": "INTEGER",
            "sharpness": "REAL",
            "video_codec": "TEXT",
            "video_bitrate": "INTEGER",
            "has_audio": "INTEGER",
        }
        for column, declaration in migrations.items():
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE hashes ADD COLUMN {column} {declaration}"
                )
        # Create indexes after column migrations so caches from early releases
        # still open cleanly when they predate the identity/version fields.
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS hashes_identity_idx
            ON hashes (algorithm_version, device, inode, size, media_type)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS hashes_content_idx
            ON hashes (algorithm_version, size, sha256)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS distinct_similar_pairs_recorded_at_idx
            ON distinct_similar_pairs (recorded_at, path_a, path_b)
            """
        )
        content_cols = ", ".join(_CONTENT_COLUMNS)
        self._conn.execute(
            f"""
            INSERT OR IGNORE INTO content_cache ({content_cols})
            SELECT {content_cols}
            FROM hashes
            WHERE size > 0 AND sha256 IS NOT NULL AND algorithm_version = ?
            """,
            (CACHE_ALGORITHM_VERSION,),
        )
        self._conn.execute(f"PRAGMA user_version = {CACHE_SCHEMA_VERSION}")
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
        same_path = cached.get("path") == rec.path
        inode_rename = (
            not same_path
            and rec.device is not None
            and rec.inode is not None
            and cached.get("device") is not None
            and cached.get("inode") is not None
            and int(cached["device"]) == int(rec.device)
            and int(cached["inode"]) == int(rec.inode)
            and cached.get("media_type") == rec.media_type.value
        )
        # Same-path edits/attribute overwrites with preserved mtime update ctime.
        # POSIX rename() also updates ctime_ns while preserving device+inode+size+mtime_ns,
        # so allow ctime_ns drift only when hydrating via a renamed (different-path) inode hit.
        if rec.ctime_ns is not None:
            cached_ctime = cached.get("ctime_ns")
            if cached_ctime is None:
                return None
            if not inode_rename and int(cached_ctime) != int(rec.ctime_ns):
                return None
        return cached

    def get(self, rec: FileRecord) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM hashes WHERE path = ? AND size = ? AND algorithm_version = ?",
            (rec.path, rec.size, CACHE_ALGORITHM_VERSION),
        ).fetchone()
        if row is not None:
            validated = self._validate_row(rec, row)
            if validated is not None:
                return validated
            # Same path and same device+inode failing validation is an in-place
            # edit/overwritten attribute, not a rename into this path.
            if (
                rec.device is not None
                and rec.inode is not None
                and row["device"] == rec.device
                and row["inode"] == rec.inode
            ):
                return None
        if rec.device is not None and rec.inode is not None:
            # Paths are not file identity. Reuse work after a rename or move on
            # the same filesystem, while requiring metadata and media type to
            # prevent an inode-reuse or extension-change false hit.
            candidates = self._conn.execute(
                """
                SELECT * FROM hashes
                WHERE algorithm_version = ? AND device = ? AND inode = ?
                    AND size = ? AND media_type = ?
                """,
                (
                    CACHE_ALGORITHM_VERSION,
                    rec.device,
                    rec.inode,
                    rec.size,
                    rec.media_type.value,
                ),
            ).fetchall()
            if len(candidates) > 1:
                candidates.sort(key=lambda item: item["path"])
            for candidate in candidates:
                validated = self._validate_row(rec, candidate)
                if validated is not None:
                    return validated
        if rec.size > 0 and rec.sha256:
            content_row = self._conn.execute(
                """
                SELECT * FROM content_cache
                WHERE size = ? AND sha256 = ? AND algorithm_version = ? AND media_type = ?
                """,
                (rec.size, rec.sha256, CACHE_ALGORITHM_VERSION, rec.media_type.value),
            ).fetchone()
            if content_row is not None:
                return dict(content_row)
        return None

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

    def recorded_pairs_among(self, paths: set[str] | list[str]) -> set[tuple[str, str]]:
        """Path-keyed distinct rows whose two files are both in ``paths``.

        Used to tell the pairs a whole-group review is about to create apart
        from pairs decided earlier, so undoing that review drops only its own.
        """
        wanted = sorted(set(paths))
        if len(wanted) < 2:
            return set()
        if len(wanted) <= HYDRATE_BATCH_SIZE:
            marks = ",".join("?" for _ in wanted)
            rows = self._conn.execute(
                "SELECT path_a, path_b FROM distinct_similar_pairs "
                f"WHERE path_a IN ({marks}) AND path_b IN ({marks})",
                (*wanted, *wanted),
            )
            return {(row["path_a"], row["path_b"]) for row in rows}
        wanted_set = set(wanted)
        found: set[tuple[str, str]] = set()
        for start in range(0, len(wanted), HYDRATE_BATCH_SIZE):
            chunk = wanted[start : start + HYDRATE_BATCH_SIZE]
            marks = ",".join("?" for _ in chunk)
            for row in self._conn.execute(
                "SELECT path_a, path_b FROM distinct_similar_pairs "
                f"WHERE path_a IN ({marks})",
                chunk,
            ):
                if row["path_b"] in wanted_set:
                    found.add((row["path_a"], row["path_b"]))
        return found

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
        """Bring stored perceptual content identities up to the current hashes.

        Rows written while a file carried no perceptual hash (legacy caches,
        decisions recorded from unhashed loaded sessions) are stat-only: any
        metadata touch revives the pair for review. Once a scan has hashed
        the file again, the row can carry the content identity and survive
        drift. A row whose identity came from an older hashing pipeline is
        refreshed the same way, so a change to the hashes does not strand
        the decision on values no scan will compute again. Only sides whose
        stored stat identity still matches the current record are written —
        a changed file must not have its decision hardened. Returns the
        number of rows updated.
        """
        by_path: dict[str, FileRecord] = {}
        by_identity: dict[str, FileRecord] = {}
        for rec in records:
            by_path[rec.path] = rec
            by_identity.setdefault(self._identity(rec), rec)
        updates: list[tuple[str | None, str | None, str, str]] = []
        rows = self._conn.execute(
            "SELECT path_a, identity_a, content_a, path_b, identity_b, content_b "
            "FROM distinct_similar_pairs"
        ).fetchall()
        for row in rows:
            fills: dict[str, str] = {}
            for path_key, identity_key, content_key in (
                ("path_a", "identity_a", "content_a"),
                ("path_b", "identity_b", "content_b"),
            ):
                rec = by_path.get(row[path_key]) or by_identity.get(row[identity_key])
                # Renames keep the stored identity; drift does not. Only an
                # identity match proves the hashes describe the decided file.
                if rec is None or self._identity(rec) != row[identity_key]:
                    continue
                content = _content_identity(rec)
                if content and content != row[content_key]:
                    fills[content_key] = content
            if fills:
                updates.append(
                    (
                        fills.get("content_a"),
                        fills.get("content_b"),
                        row["path_a"],
                        row["path_b"],
                    )
                )
        if updates:
            for start in range(0, len(updates), STORE_BATCH_SIZE):
                self._conn.executemany(
                    "UPDATE distinct_similar_pairs "
                    "SET content_a = COALESCE(?, content_a), "
                    "    content_b = COALESCE(?, content_b) "
                    "WHERE path_a = ? AND path_b = ?",
                    updates[start : start + STORE_BATCH_SIZE],
                )
            self._conn.commit()
        return len(updates)

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
        rec.orientation_phashes = row["orientation_phashes"] or rec.orientation_phashes
        if _is_multi_frame_jpeg_hash(rec):
            # Earlier versions hashed a multi-picture JPEG's gain or depth map
            # as animation frames. Rehash it as the still photo it is.
            rec.phash = rec.dhash = rec.tile_phashes = rec.orientation_phashes = None
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
        if row.get("male_face_count") is not None:
            rec.male_face_count = int(row["male_face_count"])
        if row.get("female_face_count") is not None:
            rec.female_face_count = int(row["female_face_count"])
        rec.face_detector = row["face_detector"] or rec.face_detector
        rec.face_detection_signature = (
            row["face_detection_signature"] or rec.face_detection_signature
        )
        if row.get("people_score") is not None and row.get("people_score_signature"):
            rec.people_score = float(row["people_score"])
            rec.people_score_signature = row["people_score_signature"]
        if row.get("non_person_coverage") is not None and row.get("non_person_coverage_signature"):
            rec.non_person_coverage = float(row["non_person_coverage"])
            rec.non_person_coverage_signature = row["non_person_coverage_signature"]
        if row.get("border_signature") and row.get("border_crop") is not None:
            rec.border_crop = row["border_crop"]
            rec.border_signature = row["border_signature"]
        if row.get("border_levels_signature"):
            rec.border_crop_conservative = row.get("border_crop_conservative")
            rec.border_crop_aggressive = row.get("border_crop_aggressive")
            rec.border_levels_signature = row["border_levels_signature"]
        if row.get("capture_metadata") is not None:
            rec.capture_metadata = bool(row["capture_metadata"])
        if row.get("sharpness") is not None:
            rec.sharpness = float(row["sharpness"])
        if row.get("video_codec"):
            rec.video_codec = row["video_codec"]
        if row.get("video_bitrate") is not None:
            rec.video_bitrate = int(row["video_bitrate"])
        if row.get("has_audio") is not None:
            rec.has_audio = bool(row["has_audio"])
        if row.get("media_type"):
            try:
                rec.media_type = MediaType(row["media_type"])
            except ValueError:
                pass

    @staticmethod
    def _apply_content_row(rec: FileRecord, row: dict) -> bool:
        """Fill missing content-derived analysis from ``row`` without altering identity."""
        row_mt = row.get("media_type")
        if row_mt and row_mt != rec.media_type.value:
            return False
        changed = False
        if not (rec.width and rec.height) and row.get("width") and row.get("height"):
            rec.width = int(row["width"])
            rec.height = int(row["height"])
            changed = True
        if not rec.partial_hash and row.get("partial_hash"):
            rec.partial_hash = row["partial_hash"]
            changed = True
        if rec.media_type in (MediaType.IMAGE, MediaType.GIF):
            if not rec.phash and row.get("phash"):
                rec.phash = row["phash"]
                rec.dhash = rec.dhash or row.get("dhash")
                rec.tile_phashes = rec.tile_phashes or row.get("tile_phashes")
                rec.orientation_phashes = (
                    rec.orientation_phashes or row.get("orientation_phashes")
                )
                if _is_multi_frame_jpeg_hash(rec):
                    rec.phash = rec.dhash = rec.tile_phashes = rec.orientation_phashes = None
                else:
                    changed = True
        elif rec.media_type == MediaType.VIDEO:
            if not rec.video_fingerprint and row.get("video_fingerprint"):
                rec.video_fingerprint = row["video_fingerprint"]
                changed = True
            if rec.duration is None and row.get("duration") is not None:
                rec.duration = float(row["duration"])
                changed = True
            if not rec.video_codec and row.get("video_codec"):
                rec.video_codec = row["video_codec"]
                changed = True
            if rec.video_bitrate is None and row.get("video_bitrate") is not None:
                rec.video_bitrate = int(row["video_bitrate"])
                changed = True
            if rec.has_audio is None and row.get("has_audio") is not None:
                rec.has_audio = bool(row["has_audio"])
                changed = True
        if (
            rec.human_detection_status is None
            and (
                row.get("human_detection_status") == MANUALLY_CONFIRMED_HUMAN_STATUS
                or (
                    row.get("human_detection_status") in CACHEABLE_HUMAN_STATUSES
                    and row.get("human_detection_signature")
                )
            )
        ):
            rec.human_detection_status = row["human_detection_status"]
            rec.human_detector = row.get("human_detector")
            rec.human_detection_signature = row.get("human_detection_signature")
            rec.human_frames_analyzed = _opt_int(row.get("human_frames_analyzed"))
            rec.human_max_confidence = _opt_float(row.get("human_max_confidence"))
            changed = True
        if (
            rec.face_count is None
            and row.get("face_count") is not None
            and row.get("face_detection_signature")
        ):
            rec.face_count = int(row["face_count"])
            rec.male_face_count = _opt_int(row.get("male_face_count"))
            rec.female_face_count = _opt_int(row.get("female_face_count"))
            rec.face_detector = row.get("face_detector")
            rec.face_detection_signature = row["face_detection_signature"]
            changed = True
        if (
            rec.people_score is None
            and row.get("people_score") is not None
            and row.get("people_score_signature")
        ):
            rec.people_score = float(row["people_score"])
            rec.people_score_signature = row["people_score_signature"]
            changed = True
        if (
            rec.non_person_coverage is None
            and row.get("non_person_coverage") is not None
            and row.get("non_person_coverage_signature")
        ):
            rec.non_person_coverage = float(row["non_person_coverage"])
            rec.non_person_coverage_signature = row["non_person_coverage_signature"]
            changed = True
        if (
            rec.border_crop is None
            and row.get("border_signature")
            and row.get("border_crop") is not None
        ):
            rec.border_crop = row["border_crop"]
            rec.border_signature = row["border_signature"]
            changed = True
        if (
            rec.border_levels_signature is None
            and row.get("border_levels_signature")
        ):
            rec.border_crop_conservative = row.get("border_crop_conservative")
            rec.border_crop_aggressive = row.get("border_crop_aggressive")
            rec.border_levels_signature = row["border_levels_signature"]
            changed = True
        if rec.capture_metadata is None and row.get("capture_metadata") is not None:
            rec.capture_metadata = bool(row["capture_metadata"])
            changed = True
        if rec.sharpness is None and row.get("sharpness") is not None:
            rec.sharpness = float(row["sharpness"])
            changed = True
        return changed

    def _lookup_content_cache(
        self, records: list[FileRecord]
    ) -> dict[tuple[int, str], dict]:
        """Batch-fetch content_cache (and hashes fallback) rows keyed by (size, sha256)."""
        keys = sorted(
            {
                (int(rec.size), rec.sha256)
                for rec in records
                if rec.size > 0 and rec.sha256
            }
        )
        if not keys:
            return {}
        by_key: dict[tuple[int, str], dict] = {}
        for start in range(0, len(keys), CONTENT_BATCH_SIZE):
            chunk = keys[start : start + CONTENT_BATCH_SIZE]
            pairs = ",".join("(?,?)" for _ in chunk)
            params: list = [CACHE_ALGORITHM_VERSION]
            for sz, sha in chunk:
                params.extend((sz, sha))
            for row in self._conn.execute(
                f"SELECT * FROM content_cache WHERE algorithm_version = ? "
                f"AND (size, sha256) IN ({pairs})",
                params,
            ):
                key = (int(row["size"]), row["sha256"])
                d = dict(row)
                by_key[key] = d
                self._content_snapshots[key] = tuple(d.get(c) for c in _CONTENT_COLUMNS)
        missing = [k for k in keys if k not in by_key]
        if missing:
            for start in range(0, len(missing), CONTENT_BATCH_SIZE):
                chunk = missing[start : start + CONTENT_BATCH_SIZE]
                pairs = ",".join("(?,?)" for _ in chunk)
                params = [CACHE_ALGORITHM_VERSION]
                for sz, sha in chunk:
                    params.extend((sz, sha))
                for row in self._conn.execute(
                    f"SELECT * FROM hashes WHERE algorithm_version = ? "
                    f"AND (size, sha256) IN ({pairs})",
                    params,
                ):
                    key = (int(row["size"]), row["sha256"])
                    if key not in by_key:
                        by_key[key] = dict(row)
        return by_key

    def reuse_content_analysis(self, records: list[FileRecord]) -> int:
        """Copy content-derived analysis across matching ``(size, sha256)`` siblings and cache."""
        targets = [rec for rec in records if rec.size > 0 and rec.sha256]
        if not targets:
            return 0
        updated_paths: set[str] = set()
        by_key = self._lookup_content_cache(targets)
        if by_key:
            for rec in targets:
                row = by_key.get((int(rec.size), rec.sha256))
                if row is not None and self._apply_content_row(rec, row):
                    updated_paths.add(rec.path)
        updated_paths.update(_share_exact_sibling_paths(targets))
        return len(updated_paths)

    def hydrate(self, records: list[FileRecord]) -> int:
        """Fill records from cache. Returns number of cache hits.

        Batched: one chunked ``IN`` query per ~400 paths (plus one batched
        device/inode fallback pass for path misses, and a content_cache pass
        when ``sha256`` is already present).
        """
        hits = 0
        hit_paths: set[str] = set()
        fallback_candidates: list[FileRecord] = []
        for start in range(0, len(records), HYDRATE_BATCH_SIZE):
            chunk = records[start : start + HYDRATE_BATCH_SIZE]
            paths = [rec.path for rec in chunk]
            placeholders = ",".join("?" for _ in paths)
            # Look up the primary keys first. Including algorithm_version in
            # this predicate can make SQLite scan every row of that version
            # for each batch instead of seeking the requested paths.
            rows_by_path = {
                row["path"]: row
                for row in self._conn.execute(
                    f"SELECT * FROM hashes WHERE path IN ({placeholders})", paths
                )
                if row["algorithm_version"] == CACHE_ALGORITHM_VERSION
            }
            for rec in chunk:
                row = rows_by_path.get(rec.path)
                cached = self._validate_row(rec, row) if row is not None else None
                if cached is not None:
                    hits += 1
                    hit_paths.add(rec.path)
                    self._hydrated_snapshots[rec.path] = _row_snapshot(cached)
                    self._apply_row(rec, cached)
                elif rec.device is not None and rec.inode is not None:
                    if (
                        row is not None
                        and row["device"] == rec.device
                        and row["inode"] == rec.inode
                    ):
                        # Same path + same inode failed validation -> in-place edit;
                        # never fall back to a stale hardlink alias.
                        continue
                    fallback_candidates.append(rec)

        # Identity fallback for path misses: reuse hashes after a rename/move
        # on the same filesystem (same device+inode, size, mtime, and media type).
        for start in range(0, len(fallback_candidates), IDENTITY_HYDRATE_BATCH_SIZE):
            chunk = fallback_candidates[start : start + IDENTITY_HYDRATE_BATCH_SIZE]
            pairs = ",".join("(?,?,?,?)" for _ in chunk)
            params: list = [CACHE_ALGORITHM_VERSION]
            for rec in chunk:
                params.extend((rec.device, rec.inode, rec.size, rec.media_type.value))
            identities: dict[tuple, list[sqlite3.Row]] = {}
            for row in self._conn.execute(
                f"SELECT * FROM hashes WHERE algorithm_version = ? "
                f"AND (device, inode, size, media_type) IN ({pairs})",
                params,
            ):
                key = (row["device"], row["inode"], row["size"], row["media_type"])
                identities.setdefault(key, []).append(row)
            for candidates in identities.values():
                if len(candidates) > 1:
                    candidates.sort(key=lambda item: item["path"])
            for rec in chunk:
                key = (rec.device, rec.inode, rec.size, rec.media_type.value)
                for row in identities.get(key, ()):
                    cached = self._validate_row(rec, row)
                    if cached is None:
                        continue
                    hits += 1
                    hit_paths.add(rec.path)
                    self._apply_row(rec, cached)
                    break

        # Content-keyed fallback & enrichment when sha256 is already populated.
        sha_records = [rec for rec in records if rec.size > 0 and rec.sha256]
        if sha_records:
            by_key = self._lookup_content_cache(sha_records)
            for rec in sha_records:
                content_row = by_key.get((int(rec.size), rec.sha256))
                if content_row is not None:
                    applied = self._apply_content_row(rec, content_row)
                    if rec.path not in hit_paths and applied:
                        hits += 1
                        hit_paths.add(rec.path)
        return hits

    def store_all(self, records: list[FileRecord]) -> None:
        rows: list[tuple] = []
        content_rows_by_key: dict[tuple[int, str], tuple] = {}
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
            worth_caching = bool(
                rec.sha256
                or rec.phash
                or rec.video_fingerprint
                or rec.partial_hash
                or (rec.width and rec.height)
                or has_person_decision
                or has_face_count
                or (rec.people_score is not None and bool(rec.people_score_signature))
                or (rec.non_person_coverage is not None and bool(rec.non_person_coverage_signature))
                or (rec.border_crop is not None and bool(rec.border_signature))
                or rec.capture_metadata is not None
                or rec.sharpness is not None
                or rec.video_codec
                or rec.video_bitrate is not None
                or rec.has_audio is not None
            )
            if not worth_caching:
                continue
            row = _upsert_row(rec)
            dirty_hash = self._hydrated_snapshots.get(rec.path) != row
            if dirty_hash:
                rows.append(row)
            if rec.size > 0 and rec.sha256:
                crow = _content_row(rec)
                ckey = (int(rec.size), rec.sha256)
                if self._content_snapshots.get(ckey) != crow:
                    content_rows_by_key[ckey] = crow
        if not rows and not content_rows_by_key:
            return
        # One statement per batch instead of one round trip per record.
        for start in range(0, len(rows), STORE_BATCH_SIZE):
            self._conn.executemany(_UPSERT_SQL, rows[start : start + STORE_BATCH_SIZE])
        content_rows = list(content_rows_by_key.values())
        for start in range(0, len(content_rows), STORE_BATCH_SIZE):
            self._conn.executemany(
                _CONTENT_UPSERT_SQL, content_rows[start : start + STORE_BATCH_SIZE]
            )
        self.commit()
        for row in rows:
            self._hydrated_snapshots[row[0]] = row
        for ckey, crow in content_rows_by_key.items():
            self._content_snapshots[ckey] = crow


def _copy_record_analysis(donor: FileRecord, target: FileRecord) -> bool:
    """Copy content-derived analysis from ``donor`` onto ``target`` where missing."""
    if donor.media_type != target.media_type:
        return False
    changed = False
    if not (target.width and target.height) and donor.width and donor.height:
        target.width = donor.width
        target.height = donor.height
        changed = True
    if not target.partial_hash and donor.partial_hash:
        target.partial_hash = donor.partial_hash
        changed = True
    if target.media_type in (MediaType.IMAGE, MediaType.GIF):
        if not target.phash and donor.phash:
            target.phash = donor.phash
            target.dhash = target.dhash or donor.dhash
            target.tile_phashes = target.tile_phashes or donor.tile_phashes
            target.orientation_phashes = (
                target.orientation_phashes or donor.orientation_phashes
            )
            if _is_multi_frame_jpeg_hash(target):
                target.phash = target.dhash = target.tile_phashes = None
                target.orientation_phashes = None
            else:
                changed = True
    elif target.media_type == MediaType.VIDEO:
        if not target.video_fingerprint and donor.video_fingerprint:
            target.video_fingerprint = donor.video_fingerprint
            changed = True
        if target.duration is None and donor.duration is not None:
            target.duration = donor.duration
            changed = True
        if not target.video_codec and donor.video_codec:
            target.video_codec = donor.video_codec
            changed = True
        if target.video_bitrate is None and donor.video_bitrate is not None:
            target.video_bitrate = donor.video_bitrate
            changed = True
        if target.has_audio is None and donor.has_audio is not None:
            target.has_audio = donor.has_audio
            changed = True
    if target.human_detection_status is None and (
        donor.human_detection_status == MANUALLY_CONFIRMED_HUMAN_STATUS
        or (
            donor.human_detection_status in CACHEABLE_HUMAN_STATUSES
            and donor.human_detection_signature
        )
    ):
        target.human_detection_status = donor.human_detection_status
        target.human_detector = donor.human_detector
        target.human_detection_signature = donor.human_detection_signature
        target.human_frames_analyzed = donor.human_frames_analyzed
        target.human_max_confidence = donor.human_max_confidence
        changed = True
    if (
        target.face_count is None
        and donor.face_count is not None
        and donor.face_detection_signature
    ):
        target.face_count = donor.face_count
        target.male_face_count = donor.male_face_count
        target.female_face_count = donor.female_face_count
        target.face_detector = donor.face_detector
        target.face_detection_signature = donor.face_detection_signature
        changed = True
    if (
        target.people_score is None
        and donor.people_score is not None
        and donor.people_score_signature
    ):
        target.people_score = donor.people_score
        target.people_score_signature = donor.people_score_signature
        changed = True
    if (
        target.non_person_coverage is None
        and donor.non_person_coverage is not None
        and donor.non_person_coverage_signature
    ):
        target.non_person_coverage = donor.non_person_coverage
        target.non_person_coverage_signature = donor.non_person_coverage_signature
        changed = True
    if (
        target.border_crop is None
        and donor.border_signature
        and donor.border_crop is not None
    ):
        target.border_crop = donor.border_crop
        target.border_signature = donor.border_signature
        changed = True
    if target.border_levels_signature is None and donor.border_levels_signature:
        target.border_crop_conservative = donor.border_crop_conservative
        target.border_crop_aggressive = donor.border_crop_aggressive
        target.border_levels_signature = donor.border_levels_signature
        changed = True
    if target.capture_metadata is None and donor.capture_metadata is not None:
        target.capture_metadata = donor.capture_metadata
        changed = True
    if target.sharpness is None and donor.sharpness is not None:
        target.sharpness = donor.sharpness
        changed = True
    return changed


def _share_exact_sibling_paths(records: list[FileRecord]) -> set[str]:
    """Propagate cached/computed content analysis across ``(size, sha256)`` siblings."""
    by_content: dict[tuple[int, str, str], list[FileRecord]] = {}
    for rec in records:
        if rec.size > 0 and rec.sha256:
            by_content.setdefault((int(rec.size), rec.sha256, rec.media_type.value), []).append(
                rec
            )
    updated_paths: set[str] = set()
    for siblings in by_content.values():
        if len(siblings) < 2:
            continue
        merged = siblings[0]
        for other in siblings[1:]:
            if _copy_record_analysis(other, merged):
                updated_paths.add(merged.path)
        for other in siblings[1:]:
            if _copy_record_analysis(merged, other):
                updated_paths.add(other.path)
    return updated_paths


def share_exact_sibling_analysis(records: list[FileRecord]) -> int:
    """Propagate cached/computed content analysis across ``(size, sha256)`` siblings."""
    return len(_share_exact_sibling_paths(records))
