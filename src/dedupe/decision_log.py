"""Append-only log of crop and distinct review decisions.

Every crop apply/skip/reconsider/undo and every mark/unmark-distinct review
appends one JSON object per line, with the detector output the user was
looking at when they decided (the suggested crop box, the pair's hash
distances). The stores those decisions also write — action receipts, the
hash cache's ``border_crop`` and ``distinct_similar_pairs`` — only remember
the latest verdict and, for a skipped crop, overwrite the suggestion that
was rejected; this log is the history future detector tuning needs:
positives, negatives, and reversals, with features intact.

The log is local only, holds paths and hashes (never file bytes), and is
safe to delete: nothing in a scan reads it. Writes must never fail the
review action they describe, so callers log through a helper that records
the error for the status API instead of raising.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from .models import FileRecord

DECISION_LOG_VERSION = 1
DECISION_LOG_ENV = "DEDUPE_DECISION_LOG_PATH"
MAX_NOTE_CHARS = 500

#: Why a suggested crop was skipped, offered as one-tap chips after the
#: skip lands (never required). Ids are stable; labels are display text.
SKIP_REASONS: dict[str, str] = {
    "cuts_content": "Cuts real content",
    "cuts_person": "Cuts a person",
    "leaves_bars": "Leaves bars behind",
    "bars_intentional": "Bars are intentional",
    "keep_interface": "Keep the interface",
    "not_bars": "Not bars / not a screenshot",
    "other": "Other",
}

CROP_DECISIONS = ("applied", "skipped", "reconsidered", "undone", "annotated")
DISTINCT_DECISIONS = ("marked", "unmarked")

_lock = threading.Lock()


def default_decision_log_path() -> Path:
    override = os.environ.get(DECISION_LOG_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    state = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return state / "dedupe" / "decision-log.jsonl"


def resolve_decision_log_path(path: str | Path | None = None) -> Path:
    return Path(path).expanduser() if path else default_decision_log_path()


def validate_skip_annotation(reason: Any, note: Any) -> tuple[str | None, str | None]:
    """Clean a skip annotation; raises ``ValueError`` on an unknown reason."""
    if reason is not None and reason not in SKIP_REASONS:
        raise ValueError(f"unknown skip reason: {reason!r}")
    if note is not None:
        if not isinstance(note, str):
            raise ValueError("note must be text")
        note = note.strip()[:MAX_NOTE_CHARS] or None
    return reason, note


def _stamp(event: dict) -> dict:
    return {
        "version": DECISION_LOG_VERSION,
        "event_id": uuid.uuid4().hex,
        "recorded_at": datetime.now(UTC).isoformat(),
        **event,
    }


def append_decisions(events: list[dict], path: str | Path | None = None) -> int:
    """Append decision events as JSON lines; returns how many were written."""
    if not events:
        return 0
    target = resolve_decision_log_path(path)
    lines = "".join(
        json.dumps(_stamp(event), separators=(",", ":"), ensure_ascii=False) + "\n"
        for event in events
    )
    with _lock:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(lines)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    return len(events)


def read_decisions(path: str | Path | None = None) -> tuple[list[dict], int]:
    """Read every event; returns (events, malformed lines skipped)."""
    target = resolve_decision_log_path(path)
    events: list[dict] = []
    skipped = 0
    try:
        stream = target.open(encoding="utf-8")
    except FileNotFoundError:
        return [], 0
    with stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(event, dict) and event.get("kind") in ("crop", "distinct"):
                events.append(event)
            else:
                skipped += 1
    return events, skipped


def file_snapshot(record: FileRecord) -> dict:
    """The per-file features worth keeping next to a decision.

    Raw perceptual hashes ride along so pair metrics invented later can
    still be computed after the file itself is trashed, cropped, or moved.
    """
    return {
        "path": record.path,
        "media_type": record.media_type.value,
        "extension": record.extension,
        "size": record.size,
        "mtime_ns": record.mtime_ns,
        "device": record.device,
        "inode": record.inode,
        "width": record.width,
        "height": record.height,
        "duration": record.duration,
        "sha256": record.sha256,
        "phash": record.phash,
        "dhash": record.dhash,
        "tile_phashes": record.tile_phashes,
        "orientation_phashes": record.orientation_phashes,
        "video_fingerprint": record.video_fingerprint,
    }


def _hamming(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return None


def pair_features(left: FileRecord, right: FileRecord) -> dict:
    """Distances between two records' stored hashes (no file reads)."""
    from .similar_image import decode_tile_phashes
    from .similar_video import video_fingerprint_distances

    features: dict[str, Any] = {
        "phash_distance": _hamming(left.phash, right.phash),
        "dhash_distance": _hamming(left.dhash, right.dhash),
        "tile_max_distance": None,
        "tile_mean_distance": None,
        "tile_count": None,
        "video_mean_distance": None,
        "video_max_distance": None,
        "video_frame_count": None,
        "same_dimensions": (
            (left.width, left.height) == (right.width, right.height)
            if left.width and right.width
            else None
        ),
        "size_ratio": (
            round(min(left.size, right.size) / max(left.size, right.size), 4)
            if left.size and right.size
            else None
        ),
    }
    tiles_left = decode_tile_phashes(left.tile_phashes)
    tiles_right = decode_tile_phashes(right.tile_phashes)
    if tiles_left and tiles_right and len(tiles_left) == len(tiles_right):
        dists = [d for d in (_hamming(a, b) for a, b in zip(tiles_left, tiles_right)) if d is not None]
        if len(dists) == len(tiles_left):
            features.update(
                tile_max_distance=max(dists),
                tile_mean_distance=round(sum(dists) / len(dists), 3),
                tile_count=len(dists),
            )
    if left.video_fingerprint and right.video_fingerprint:
        dists = video_fingerprint_distances(left.video_fingerprint, right.video_fingerprint)
        if dists:
            features.update(
                video_mean_distance=round(sum(dists) / len(dists), 3),
                video_max_distance=max(dists),
                video_frame_count=len(dists),
            )
    return features


def make_crop_event(
    decision: str,
    record: FileRecord,
    box=None,
    *,
    group_id: str | None = None,
    scan_id: str | None = None,
    source: str = "single",
    after: dict | None = None,
    reason: str | None = None,
    note: str | None = None,
    receipt_path: str | None = None,
    adjusted_box=None,
) -> dict:
    """One crop decision; ``box`` is the suggestion the user decided on.

    ``adjusted_box`` is the zone the user dragged the suggestion to before
    deciding — what an applied crop actually cut, or the corrected zone a
    skip still rejected. It is kept beside the suggestion, never in place
    of it, so detector tuning can measure how far users move its boxes.
    """
    adjusted = adjusted_box if adjusted_box is not None and adjusted_box != box else None
    return {
        "kind": "crop",
        "decision": decision,
        "source": source,
        "group_id": group_id,
        "scan_id": scan_id,
        "file": file_snapshot(record),
        "suggestion": box.to_dict() if box is not None else None,
        "suggestion_encoded": box.encode() if box is not None else record.border_crop,
        "adjusted": adjusted.to_dict() if adjusted is not None else None,
        "adjusted_encoded": adjusted.encode() if adjusted is not None else None,
        "border_signature": record.border_signature,
        "after": after,
        "reason": reason,
        "note": note,
        "receipt_path": receipt_path,
    }


def make_distinct_event(
    decision: str,
    left: FileRecord,
    right: FileRecord,
    *,
    group_id: str | None = None,
    scan_id: str | None = None,
    source: str = "unknown",
    label_scope: str = "pair",
    match_basis: str | None = None,
    match_score: float | None = None,
) -> dict:
    """One pair-level distinct decision (pairs sorted by path).

    ``label_scope`` says how much of the label the user actually saw:
    ``pair`` for two files compared/decided together, ``group`` for one
    whole-group confirmation expanded to its pairs, ``backfill`` for pairs
    suppressed at group dissolution that were never shown together.
    """
    left, right = sorted((left, right), key=lambda rec: rec.path)
    return {
        "kind": "distinct",
        "decision": decision,
        "source": source,
        "label_scope": label_scope,
        "group_id": group_id,
        "scan_id": scan_id,
        "path_a": left.path,
        "path_b": right.path,
        "file_a": file_snapshot(left),
        "file_b": file_snapshot(right),
        "pair": pair_features(left, right),
        "match_basis": match_basis,
        "match_score": match_score,
    }


def make_distinct_path_event(
    decision: str,
    path_a: str,
    path_b: str,
    *,
    group_id: str | None = None,
    scan_id: str | None = None,
    source: str = "unknown",
    label_scope: str = "pair",
) -> dict:
    """A distinct decision for files no longer in the scan (paths only).

    Reversals from History, or for dissolved groups, may have no records
    left to feature; the pair identity alone still folds correctly.
    """
    left, right = sorted((path_a, path_b))
    return {
        "kind": "distinct",
        "decision": decision,
        "source": source,
        "label_scope": label_scope,
        "group_id": group_id,
        "scan_id": scan_id,
        "path_a": left,
        "path_b": right,
        "file_a": {"path": left},
        "file_b": {"path": right},
        "pair": {},
        "match_basis": None,
        "match_score": None,
    }


def distinct_events_for_records(
    decision: str,
    records: list[FileRecord],
    **kwargs: Any,
) -> list[dict]:
    """Pair events for every pair among ``records`` (mark_distinct's shape)."""
    return [
        make_distinct_event(decision, left, right, **kwargs)
        for left, right in combinations(sorted(records, key=lambda rec: rec.path), 2)
    ]


def _crop_key(event: dict) -> tuple[str, str | None] | None:
    file = event.get("file") or {}
    path = file.get("path")
    return (path, event.get("suggestion_encoded")) if path else None


def current_crop_labels(events: list[dict]) -> list[dict]:
    """Latest standing verdict per (file, suggestion), newest event last.

    ``reconsidered`` withdraws a skip; ``undone`` stays as its own verdict
    (the user saw the cropped result and reversed it — a stronger negative
    than a skip, and not the same one). Annotations fold their reason/note
    into the verdict they annotate.
    """
    labels: dict[tuple[str, str | None], dict] = {}
    for event in events:
        if event.get("kind") != "crop":
            continue
        key = _crop_key(event)
        if key is None:
            continue
        decision = event.get("decision")
        if decision == "annotated":
            if key in labels:
                labels[key] = {
                    **labels[key],
                    "reason": event.get("reason"),
                    "note": event.get("note"),
                    "annotated_at": event.get("recorded_at"),
                }
        elif decision == "reconsidered":
            labels.pop(key, None)
        elif decision in ("applied", "skipped", "undone"):
            labels[key] = event
    return list(labels.values())


def _same_file_snapshot(left: dict | None, right: dict | None) -> bool:
    """Only preserve provenance when snapshots positively identify the same file.

    Paths alone are insufficient: new media may replace a previously reviewed
    file. Require complete stat identity and matching saved features rather
    than carrying an old explicit label onto uncertain or changed content.
    """
    if not left or not right:
        return False
    identity = ("size", "mtime_ns", "device", "inode")
    return all(left.get(key) is not None for key in identity) and left == right


def current_distinct_labels(events: list[dict]) -> list[dict]:
    """Pairs whose latest distinct decision still stands.

    Dissolution backfill is inferred, so it cannot replace a standing
    explicit verdict. Apply this when reading as well as writing so logs
    recorded before that safeguard retain their inspected-pair labels.
    """
    labels: dict[tuple[str, str], dict] = {}
    for event in events:
        if event.get("kind") != "distinct":
            continue
        path_a, path_b = event.get("path_a"), event.get("path_b")
        if not path_a or not path_b:
            continue
        key = tuple(sorted((path_a, path_b)))
        if event.get("decision") == "marked":
            if (
                event.get("label_scope") == "backfill"
                and labels.get(key, {}).get("label_scope") in ("pair", "group")
                and all(
                    _same_file_snapshot(labels[key].get(field), event.get(field))
                    for field in ("file_a", "file_b")
                )
            ):
                continue
            labels[key] = event
        elif event.get("decision") == "unmarked":
            labels.pop(key, None)
    return list(labels.values())


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _removed_band(event: dict) -> str:
    fraction = (event.get("suggestion") or {}).get("removed_fraction")
    if not isinstance(fraction, (int, float)):
        return "unknown"
    for limit, label in ((0.05, "0-5%"), (0.15, "5-15%"), (0.30, "15-30%"), (0.50, "30-50%")):
        if fraction < limit:
            return label
    return "50%+"


def summarize_decisions(events: list[dict], *, skipped_lines: int = 0) -> dict:
    """Tuning-ready rollup: current labels, not raw event counts, decide rates."""
    crop_events = [e for e in events if e.get("kind") == "crop"]
    distinct_events = [e for e in events if e.get("kind") == "distinct"]
    crops = current_crop_labels(events)
    distinct = current_distinct_labels(events)

    def crop_rate(labels: list[dict]) -> dict:
        counts = _counts(e.get("decision") for e in labels)
        decided = sum(counts.get(d, 0) for d in ("applied", "skipped", "undone"))
        return {
            "labels": counts,
            "decided": decided,
            "acceptance_rate": (
                round(counts.get("applied", 0) / decided, 4) if decided else None
            ),
        }

    def rate_by(key_of) -> dict:
        groups: dict[str, list[dict]] = {}
        for event in crops:
            groups.setdefault(str(key_of(event)), []).append(event)
        return {name: crop_rate(groups[name]) for name in sorted(groups)}

    phash_dists = [
        (e.get("pair") or {}).get("phash_distance") for e in distinct
    ]
    phash_dists = [d for d in phash_dists if isinstance(d, int)]
    stamps = [e.get("recorded_at") for e in events if e.get("recorded_at")]
    return {
        "version": DECISION_LOG_VERSION,
        "events": len(events),
        "skipped_lines": skipped_lines,
        "first_recorded_at": min(stamps) if stamps else None,
        "last_recorded_at": max(stamps) if stamps else None,
        "crop": {
            "events_by_decision": _counts(e.get("decision") for e in crop_events),
            **crop_rate(crops),
            "by_media_type": rate_by(lambda e: (e.get("file") or {}).get("media_type")),
            "by_kind": rate_by(
                lambda e: "screenshot_interface"
                if (e.get("suggestion") or {}).get("chrome")
                else "black_bars"
            ),
            "by_removed_fraction": rate_by(_removed_band),
            "by_border_signature": rate_by(lambda e: e.get("border_signature") or "unknown"),
            "applied_by_source": _counts(
                e.get("source") for e in crops if e.get("decision") == "applied"
            ),
            "skip_reasons": _counts(
                e.get("reason") or "unspecified"
                for e in crops
                if e.get("decision") == "skipped"
            ),
            "annotated_skips": sum(
                1 for e in crops if e.get("decision") == "skipped" and e.get("reason")
            ),
            "adjusted_zones": sum(1 for e in crops if e.get("adjusted")),
        },
        "distinct": {
            "events_by_decision": _counts(e.get("decision") for e in distinct_events),
            "current_pairs": len(distinct),
            "by_label_scope": _counts(e.get("label_scope") for e in distinct),
            "by_source": _counts(e.get("source") for e in distinct),
            "by_match_basis": _counts(e.get("match_basis") or "perceptual" for e in distinct),
            "phash_distance": {
                "count": len(phash_dists),
                "min": min(phash_dists) if phash_dists else None,
                "max": max(phash_dists) if phash_dists else None,
                "mean": round(sum(phash_dists) / len(phash_dists), 3) if phash_dists else None,
                "by_distance": _counts(phash_dists),
            },
        },
    }


def crop_manifest(events: list[dict]) -> dict:
    """Standing crop verdicts as a border-detector tuning corpus.

    Each item is one file as suggested (the original bytes for an applied
    crop are in the Trash destination named in ``after``), with the user's
    verdict: ``applied`` is a positive, ``skipped``/``undone`` negatives.
    """
    items = [
        {
            "path": (event.get("file") or {}).get("path"),
            "decision": event.get("decision"),
            "source": event.get("source"),
            "reason": event.get("reason"),
            "note": event.get("note"),
            "suggestion": event.get("suggestion"),
            "suggestion_encoded": event.get("suggestion_encoded"),
            "adjusted": event.get("adjusted"),
            "adjusted_encoded": event.get("adjusted_encoded"),
            "border_signature": event.get("border_signature"),
            "media_type": (event.get("file") or {}).get("media_type"),
            "after": event.get("after"),
            "recorded_at": event.get("recorded_at"),
        }
        for event in current_crop_labels(events)
    ]
    items.sort(key=lambda item: (item["path"] or "", item["suggestion_encoded"] or ""))
    return {
        "note": (
            "User crop verdicts from the decision log. Applied = suggestion "
            "accepted (bulk applies were not individually inspected); "
            "skipped/undone = rejected. Not independent ground truth."
        ),
        "items": items,
    }


def benchmark_manifest(
    events: list[dict],
    *,
    scopes: tuple[str, ...] = ("pair",),
    only_existing: bool = False,
) -> dict:
    """Negative pairs for ``dedupe benchmark-similarity`` from standing labels.

    Only pairs the user actually decided (``pair`` scope) by default:
    whole-group expansions and dissolution backfill are weaker labels.
    The result states plainly that it is negatives-only — precision-side
    evidence, never a recall measure.
    """
    pairs = []
    missing = 0
    for event in current_distinct_labels(events):
        if event.get("label_scope") not in scopes:
            continue
        path_a, path_b = event["path_a"], event["path_b"]
        if only_existing and not (Path(path_a).is_file() and Path(path_b).is_file()):
            missing += 1
            continue
        pairs.append({
            "path_a": path_a,
            "path_b": path_b,
            "similar": False,
            "category": f"distinct-{event.get('label_scope') or 'pair'}",
        })
    pairs.sort(key=lambda pair: (pair["path_a"], pair["path_b"]))
    return {
        "note": (
            "User-reviewed distinct pairs from the decision log: negatives "
            "only. Measures false-positive grouping, not recall. Not "
            "independent ground truth."
        ),
        "scopes": list(scopes),
        "missing_skipped": missing,
        "pairs": pairs,
    }
