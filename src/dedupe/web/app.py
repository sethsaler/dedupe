"""Flask web UI for browsing and acting on duplicate groups."""

from __future__ import annotations

import hmac
import json
import math
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
import webbrowser
from dataclasses import replace
from itertools import combinations
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
)

from ..actions import (
    ActionItem,
    ActionResult,
    TrashBatch,
    _write_action_log,
    apply_actions,
    format_bytes,
    isolate_groups,
    undo_action,
    validate_record_with_stat,
)
from ..borders import (
    BORDER_DISMISSED,
    BORDER_SIGNATURE,
    CropBox,
    crop_file_in_place,
    restore_original_in_place,
    suggested_crop,
)
from ..cache import HashCache
from ..cleanup import cleanup_opportunity
from ..decider import decider_available
from ..engine import run_scan, run_scans_parallel
from ..grouping import (
    apply_smart_select,
    apply_smart_select_all,
    ensure_all_files_groups,
)
from ..human_detection import DEFAULT_PHOTON_MODEL, HUMAN_BACKENDS
from ..human_policy import MANUALLY_CONFIRMED_HUMAN_STATUS
from ..jobs import JobStore, focus_new_arrivals
from ..keep_decisions import update_keep_decisions
from ..media_details import comparison_details
from ..models import (
    GroupKind,
    ReviewPolicy,
    ScanProgress,
    ScanResult,
    SmartRule,
    effective_selected_paths,
)
from ..people_score import (
    has_current_people_score,
    people_scoring_available,
    score_people,
)
from ..review_session import (
    ReviewSessionLoad,
    discard_review_session,
    load_review_session,
    peek_review_session,
    save_review_session,
)
from ..semantic import semantic_available
from ..similar_image import (
    ORIENTATIONS,
    TILE_HASH_VERSION,
    decode_orientation_phashes,
)
from ..similar_video import aligned_position_indexes
from .media import cached_thumbnail, is_browser_safe_image, is_video, media_mimetype
from .native_picker import pick_native_paths

# Increment when adding/changing browser-facing API routes. The macOS launcher uses
# this to avoid pairing static files from the working tree with a stale Flask process.
WEB_API_VERSION = 30
PREVIEW_TOKEN_TTL_SECONDS = 600

#: Whole-group "not duplicates" reviews this server can still undo. The undo
#: history is in memory and scoped to one scan session.
DISSOLVED_SIMILAR_UNDO_LIMIT = 200

#: Flows whose members support direct per-file Trash + undo. The independent
#: review piles work one candidate at a time; similar groups use it for the
#: swipe review's same-photo (left swipe) decision, with keeper protection
#: re-derived through apply_actions.
IMMEDIATE_DELETE_KINDS = {"no_humans", "faces", "all_files", "sets", "similar"}
REVIEW_QUARANTINE_FOLDER = "_Dedupe Quarantine"

# How long after the last tab closes before the server exits. Long enough for a
# page reload to come back and cancel the shutdown, short enough to feel prompt.
SHUTDOWN_GRACE_SECONDS = 1.5

# Selection-change persistence is synchronous below this many groups and
# debounced (PERSIST_DEBOUNCE_SECONDS) above it.
PERSIST_DEBOUNCE_MIN_GROUPS = 2000
PERSIST_DEBOUNCE_SECONDS = 0.3

# Why an execute was refused, phrased so the UI can tell the user what happens next.
PREVIEW_STALE_MESSAGES = {
    "missing": (
        "this action needs a fresh preview; preview again and confirm the new numbers"
    ),
    "expired": (
        f"preview expired after {PREVIEW_TOKEN_TTL_SECONDS // 60} minutes; "
        "preview again and confirm the refreshed numbers"
    ),
    "changed": (
        "selection changed since the preview; preview again and confirm the new numbers"
    ),
}


def stale_preview_payload(reason: str) -> dict:
    return {
        "error": PREVIEW_STALE_MESSAGES[reason],
        "preview_stale": True,
        "preview_stale_reason": reason,
        "preview_ttl_seconds": PREVIEW_TOKEN_TTL_SECONDS,
    }


BULK_SELECT_OPERATIONS = {"select_all", "select_none", "invert", "criteria"}


def _hash_difference(left: str | None, right: str | None) -> tuple[int, int] | None:
    """Return differing and total bits for two same-sized hexadecimal hashes."""
    if not left or not right or len(left) != len(right):
        return None
    try:
        difference = (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return None
    return difference, len(left) * 4


def _stored_hashes(value: str | None, versions: tuple[str, ...]) -> list[str]:
    if not value or not value.startswith(tuple(f"{version}:" for version in versions)):
        return []
    return [part for part in value.split(":", 1)[1].split(",") if part]


ORIENTATION_LABELS = {
    "ROTATE_90": "rotated 90°",
    "ROTATE_270": "rotated 90°",
    "ROTATE_180": "rotated 180°",
    "FLIP_LEFT_RIGHT": "mirrored",
    "FLIP_TOP_BOTTOM": "flipped upside down",
    "TRANSPOSE": "rotated and mirrored",
    "TRANSVERSE": "rotated and mirrored",
}


def match_orientation(member, keeper) -> str | None:
    """The turn that makes a rotated or mirrored member line up with its keeper.

    None when the member already lines up as decoded (or cannot be judged).
    A turn only counts when it agrees better than the unturned file does.
    """
    turned = decode_orientation_phashes(member.orientation_phashes)
    upright = _hash_difference(member.phash, keeper.phash)
    if not turned or upright is None:
        return None
    best: tuple[int, str] | None = None
    for name, value in zip(ORIENTATIONS, turned, strict=True):
        result = _hash_difference(value, keeper.phash)
        if result is not None and (best is None or result[0] < best[0]):
            best = (result[0], name)
    if best is None or best[0] >= upright[0]:
        return None
    return best[1]


def _cosine(raw, default: float) -> float:
    """A cosine threshold from the scan form, clamped to a sane range."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if math.isnan(value):
        return default
    return min(0.999, max(0.5, value))


def similarity_percent(member, keeper) -> float | None:
    """Return perceptual-fingerprint bit agreement with a group's keeper."""
    comparisons: list[tuple[int, int]] = []
    orientation = (
        match_orientation(member, keeper) if member.media_type.value == "image" else None
    )
    if orientation is not None:
        # Only the global pHash is stored per orientation; dHash and tiles
        # describe the unturned file and would understate the agreement.
        turned = decode_orientation_phashes(member.orientation_phashes) or ()
        result = _hash_difference(turned[ORIENTATIONS.index(orientation)], keeper.phash)
        if result is not None:
            comparisons.append(result)
    elif member.media_type.value == "video":
        left = _stored_hashes(member.video_fingerprint, ("v2", "v3"))
        right = _stored_hashes(keeper.video_fingerprint, ("v2", "v3"))
        left_indexes, right_indexes = aligned_position_indexes(len(left), len(right))
        comparisons.extend(
            result
            for left_index, right_index in zip(left_indexes, right_indexes, strict=True)
            if (result := _hash_difference(left[left_index], right[right_index])) is not None
        )
    else:
        for left, right in ((member.phash, keeper.phash), (member.dhash, keeper.dhash)):
            result = _hash_difference(left, right)
            if result is not None:
                comparisons.append(result)
        left_tiles = _stored_hashes(member.tile_phashes, ("t2", TILE_HASH_VERSION))
        right_tiles = _stored_hashes(keeper.tile_phashes, ("t2", TILE_HASH_VERSION))
        if len(left_tiles) == len(right_tiles):
            comparisons.extend(
                result
                for left, right in zip(left_tiles, right_tiles, strict=True)
                if (result := _hash_difference(left, right)) is not None
            )

    differing_bits = sum(difference for difference, _total in comparisons)
    total_bits = sum(total for _difference, total in comparisons)
    if not total_bits:
        return None
    return round(100 * (1 - differing_bits / total_bits), 1)


def review_quarantine_dir(roots: list[str], selected_paths: set[str]) -> Path:
    """Place heuristic review removals beside the scan, not in system Trash."""
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve(strict=False)
        if root.is_dir():
            return root / REVIEW_QUARANTINE_FOLDER
        if root.parent.is_dir():
            return root.parent / REVIEW_QUARANTINE_FOLDER
    if selected_paths:
        return Path(next(iter(selected_paths))).parent / REVIEW_QUARANTINE_FOLDER
    return Path.cwd() / REVIEW_QUARANTINE_FOLDER


def parse_bulk_criteria(raw: dict) -> dict:
    """Validate bulk-selection criteria; unknown keys are ignored, bad values reject."""
    criteria: dict = {}
    for key in ("min_size", "max_size"):
        value = raw.get(key)
        if value in (None, ""):
            continue
        size = int(value)
        if size < 0:
            raise ValueError(f"{key} must not be negative")
        criteria[key] = size
    contains = raw.get("path_contains")
    if contains not in (None, ""):
        criteria["path_contains"] = str(contains).lower()
    if raw.get("smaller_than_keeper"):
        criteria["smaller_than_keeper"] = True
    min_faces = raw.get("min_faces")
    if min_faces not in (None, ""):
        faces = int(min_faces)
        if faces < 1:
            raise ValueError("min_faces must be at least 1")
        criteria["min_faces"] = faces
    return criteria


def bulk_member_matches(member, group, criteria: dict) -> bool:
    if "min_size" in criteria and member.size < criteria["min_size"]:
        return False
    if "max_size" in criteria and member.size > criteria["max_size"]:
        return False
    if "path_contains" in criteria and criteria["path_contains"] not in member.path.lower():
        return False
    if criteria.get("smaller_than_keeper"):
        keeper = next((m for m in group.members if m.path == group.suggested_keep), None)
        if keeper is None or member.size >= keeper.size:
            return False
    # Files without a trusted face count never match; a bulk deletion rule
    # must not select media the face counter did not actually analyze.
    return not (
        "min_faces" in criteria
        and (member.face_count is None or member.face_count < criteria["min_faces"])
    )


def bulk_selection_picks(group, operation: str, criteria: dict) -> list[str]:
    """Return the selection a bulk operation produces, with keep-one enforced.

    Duplicate groups never have their suggested keeper selected, so at least one
    member always survives. Non-human candidate groups are independent files and
    may have every candidate selected.
    """
    # All-Files browse groups have no selection semantics — the view trashes
    # one click at a time — so a bulk mark would only corrupt review progress.
    if group.kind in (GroupKind.ALL_FILES, GroupKind.SETS, GroupKind.CROP):
        return list(group.selected_for_removal)
    keep_one = group.policy == ReviewPolicy.KEEP_ONE
    keeper = None
    if keep_one and group.members:
        member_paths = [member.path for member in group.members]
        keeper = group.suggested_keep if group.suggested_keep in member_paths else member_paths[0]
    if operation == "select_none":
        return []
    already = set(group.selected_for_removal)
    picks = []
    for member in group.members:
        if member.path == keeper:
            continue
        if operation == "select_all":
            chosen = True
        elif operation == "invert":
            chosen = member.path not in already
        else:
            chosen = bulk_member_matches(member, group, criteria)
        if chosen:
            picks.append(member.path)
    return picks


def sync_low_resolution_keeps(result: ScanResult, paths: set[str]) -> str | None:
    """Persist keep decisions for low-resolution candidates among ``paths``.

    A candidate that has been reviewed and left unselected was explicitly kept;
    that decision is stored durably so future scans stop resurfacing the file.
    Any other state (selected for removal, or review withdrawn) clears it.

    Returns None on success, otherwise the error message. The durable store is
    a convenience, so a write failure never fails the selection request — but
    the caller surfaces it instead of swallowing it silently.
    """
    keep = []
    clear = []
    for group in result.groups:
        if group.kind != GroupKind.LOW_RESOLUTION:
            continue
        reviewed = set(group.reviewed_paths)
        selected = set(group.selected_for_removal)
        for member in group.members:
            if member.path not in paths:
                continue
            if member.path in reviewed and member.path not in selected:
                keep.append(member)
            else:
                clear.append(member.path)
    try:
        update_keep_decisions(keep=keep, clear=clear)
    except OSError as exc:
        return str(exc)
    return None


def drop_marked_distinct_groups(result: ScanResult, cache_path: str | Path | None) -> int:
    """Remove similar groups the user already marked distinct, when still current.

    A loaded result (``dedupe ui --load``, or an older saved session) can carry
    similar groups that a fresh scan would suppress through the hash cache's
    distinct pairs. Drop a similar group only when every pair of its members
    is a still-current distinct decision, judged against the members' present
    on-disk identity — changed, renamed, or unreadable files keep their
    group, so no unreviewed similarity is ever hidden. Never raises: without
    a readable cache every group is kept.
    """
    candidates = [
        group
        for group in result.groups
        if group.kind == GroupKind.SIMILAR and len(group.members) >= 2
    ]
    if not candidates:
        return 0
    try:
        cache = HashCache(cache_path)
    except (OSError, sqlite3.Error):
        return 0
    try:
        current: dict[str, object] = {}
        for group in candidates:
            for member in group.members:
                if member.path not in current:
                    try:
                        file_stat = os.stat(member.path)
                    except OSError:
                        current[member.path] = None
                        continue
                    current[member.path] = replace(
                        member,
                        size=int(file_stat.st_size),
                        mtime=float(file_stat.st_mtime),
                        mtime_ns=int(file_stat.st_mtime_ns),
                        device=int(file_stat.st_dev),
                        inode=int(file_stat.st_ino),
                        # The session's hashes predate the stat refresh: blank
                        # them so a metadata-drifted file cannot be confirmed
                        # distinct from stale perceptual data. Fresh scans
                        # re-verify content during matching; a loaded session
                        # stays conservative and keeps the group instead.
                        phash=None,
                        dhash=None,
                        tile_phashes=None,
                        orientation_phashes=None,
                        video_fingerprint=None,
                    )
        fresh_records = [rec for rec in current.values() if rec is not None]
        reviews = cache.distinct_reviews(fresh_records)
        covered = set()
        for group in candidates:
            fresh = [current[member.path] for member in group.members]
            if any(record is None for record in fresh):
                continue
            if all(
                reviews.is_distinct(left, right)
                for left, right in combinations(fresh, 2)
            ):
                covered.add(group.id)
        if not covered:
            return 0
        result.groups = [
            group
            for group in result.groups
            if group.kind != GroupKind.SIMILAR or group.id not in covered
        ]
        return len(covered)
    except (OSError, sqlite3.Error):
        return 0
    finally:
        try:
            cache.close()
        except Exception:
            pass


def detect_capabilities() -> dict:
    """Probe optional dependencies once; the UI gates scan options on this."""
    from ..human_detection import YUNET_MODEL_PATH

    try:
        import cv2  # noqa: F401

        opencv = True
    except Exception:
        opencv = False
    try:
        import moondream  # noqa: F401

        photon = True
    except Exception:
        photon = False
    return {
        "opencv": opencv,
        "yunet_model": YUNET_MODEL_PATH.is_file(),
        "photon": photon,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "semantic": semantic_available(),
        "decider": decider_available(),
        "people_score": people_scoring_available()[0],
    }


def scan_done_summary(result: ScanResult, scan_kwargs: dict) -> str:
    """Count what the scan looked for, and only that.

    A crop-only or faces-only scan should not report "0 exact, 0 similar" for
    checks that never ran.
    """
    parts: list[str] = []
    if scan_kwargs.get("exact") or scan_kwargs.get("similar") or scan_kwargs.get("semantic"):
        parts += [f"{result.exact_groups} exact", f"{result.similar_groups} similar"]
    if scan_kwargs.get("find_low_resolution"):
        parts.append(f"{result.low_resolution_files} low-resolution")
    if scan_kwargs.get("random_review_count"):
        parts.append(f"{result.random_review_files} random review")
    if scan_kwargs.get("find_no_humans"):
        parts.append(f"{result.no_human_files} non-human")
    if scan_kwargs.get("count_faces"):
        parts.append(f"{result.faces_files} with faces")
    if scan_kwargs.get("find_black_borders"):
        parts.append(f"{result.crop_files} to crop")
    return ", ".join(parts) or f"{len(result.files)} files"


def create_app(
    initial_result: ScanResult | None = None,
    review_session_path: str | Path | None = None,
    cache_path: str | Path | None = None,
) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    csrf_token = secrets.token_urlsafe(32)
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    app.config["DEDUPE_CSRF_TOKEN"] = csrf_token
    app.config["DEDUPE_CACHE_PATH"] = cache_path
    app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost", "[::1]"]
    capabilities = detect_capabilities()
    lock = threading.RLock()
    jobs = JobStore(review_session_path)
    from ..action_journal import recover_interrupted_actions

    recovered_actions = recover_interrupted_actions()
    # The server starts clean: the saved review is offered through the banner
    # (resume button), never auto-installed. A peek reads only the session's
    # identity, deferring the expensive per-file revalidation to an explicit
    # resume. An explicit initial_result (CLI --load / --ui handoff) is still
    # installed below and persisted over the saved session.
    loaded = peek_review_session(review_session_path)
    if initial_result is not None:
        loaded = ReviewSessionLoad(
            path=Path(review_session_path) if review_session_path else None
        )
    state: dict = {
        "result": None,
        "progress": ScanProgress(),
        "scanning": False,
        "acting": False,
        "last_error": None,
        "groups_version": 0,
        "scan_id": secrets.token_hex(12),
        "cancel_event": None,
        "allowed_reveal_paths": set(),
        "deleted_files": {},
        "streams": [],
        "review_session": loaded,
        "preview_tokens": {},
        "shutdown_timer": None,
        # Number of effectively-selected paths; maintained alongside
        # result.recompute_stats() so /api/status need not recompute per poll.
        "selected_count": 0,
        # Bumped on every in-place membership change (group streamed, members
        # removed) so the allowed-path set used by media routes can be cached.
        "paths_version": 0,
        "allowed_paths_cache": None,  # ((id(result), paths_version), frozenset)
        # Ids of groups streamed by the active scan, for O(1) replace checks.
        "streamed_group_index": {},
        # Debounced review-session persistence for large results.
        "persist_dirty": False,
        "persist_timer": None,
        # Last durable keep-decisions write failure (None when healthy). The
        # write must never fail a selection request, but it must be visible.
        "keep_decisions_error": None,
        # Per-candidate Trash undo entries dropped by the latest scan start;
        # surfaced once so the user knows in-app undo has ended for them.
        "trash_undo_cleared": 0,
        # Similar groups dissolved by a whole-group distinct review, keyed by
        # group id, so /api/similar/restore-group can undo the review.
        "dissolved_similar": {},
        # On-demand people-likelihood scoring for one group's members.
        "people_scoring": {
            "running": False,
            "scan_id": None,
            "group_id": None,
            "done": 0,
            "total": 0,
            "error": None,
        },
        # Wakes SSE generators when groups/progress/scan state change.
        "events": threading.Condition(lock),
    }
    app.extensions["dedupe_state"] = state

    def persist_result() -> bool:
        """Persist the active completed result, recording errors for status APIs."""
        with lock:
            state["persist_dirty"] = False
            timer = state.get("persist_timer")
            if timer is not None:
                timer.cancel()
                state["persist_timer"] = None
            result = state["result"]
            if result is None:
                return False
            try:
                metadata = save_review_session(
                    result, review_session_path, deleted_files=state["deleted_files"]
                )
                if result.job_id:
                    jobs.save_review(result, state["deleted_files"])
                state["review_session"] = ReviewSessionLoad(
                    result=result,
                    path=Path(metadata["path"]),
                    saved_at=metadata["saved_at"],
                )
                return True
            except (OSError, TypeError, ValueError) as exc:
                report = state["review_session"]
                report.error = str(exc)
                state["last_error"] = f"Could not save review: {exc}"
                return False

    def _flush_persist() -> None:
        with lock:
            state["persist_timer"] = None
            if not state["persist_dirty"]:
                return
        persist_result()

    def schedule_persist() -> None:
        """Persist small results synchronously; debounce multi-MB session writes.

        A selection toggle persists the whole result as JSON, which costs
        milliseconds for typical reviews but megabytes of churn per click on
        very large ones. Above the threshold, writes coalesce over a short
        window; actions, shutdown, and scan completion still flush eagerly.
        """
        with lock:
            result = state["result"]
            if result is None:
                return
            if len(result.groups) < PERSIST_DEBOUNCE_MIN_GROUPS:
                persist_result()
                return
            state["persist_dirty"] = True
            if state.get("persist_timer") is None:
                timer = threading.Timer(PERSIST_DEBOUNCE_SECONDS, _flush_persist)
                timer.daemon = True
                state["persist_timer"] = timer
                timer.start()

    def normalized_destination(action: str, destination) -> str | None:
        if action != "quarantine" or not destination:
            return None
        return str(Path(destination).expanduser().resolve(strict=False))

    def preview_manifest(action: str, scope, destination, action_result) -> tuple:
        eligible = tuple(sorted(item.path for item in action_result.items if item.ok))
        return (state["scan_id"], action, scope or "all", destination, eligible)

    def issue_preview_token(manifest: tuple) -> str:
        now = time.monotonic()
        state["preview_tokens"] = {
            token: value
            for token, value in state["preview_tokens"].items()
            if value[1] > now
        }
        token = secrets.token_urlsafe(24)
        state["preview_tokens"][token] = (manifest, now + PREVIEW_TOKEN_TTL_SECONDS)
        return token

    def consume_preview_token(token: str | None, manifest: tuple) -> str:
        """Spend a one-use token and say why it was refused: the client re-previews."""
        stored = state["preview_tokens"].pop(token, None) if token else None
        if stored is None:
            return "missing"
        if stored[1] <= time.monotonic():
            return "expired"
        if stored[0] != manifest:
            return "changed"
        return "valid"

    def group_payload(group) -> dict:
        payload = group.to_dict()
        for member, member_payload in zip(group.members, payload["members"], strict=True):
            # A score from an older model would sort against current ones.
            if not has_current_people_score(member):
                member_payload["people_score"] = None
        if group.kind == GroupKind.SETS:
            for member, member_payload in zip(group.members, payload["members"], strict=True):
                score = group.match_scores.get(member.path)
                member_payload["match_label"] = (
                    f"{score * 100:.0f}% like the first image" if score is not None else None
                )
        if group.kind == GroupKind.SIMILAR:
            keeper = next(
                (member for member in group.members if member.path == group.suggested_keep),
                None,
            )
            if keeper is not None:
                for member, member_payload in zip(
                    group.members, payload["members"], strict=True
                ):
                    if group.match_basis == "video_version":
                        member_payload["similarity_percent"] = None
                        member_payload["match_label"] = (
                            "Matching video excerpt" if group.match_details.get("relation") == "excerpt"
                            else "Same footage · review audio"
                        )
                        member_payload["orientation_label"] = None
                        continue
                    if group.match_basis == "semantic":
                        # Cosine of embeddings is not fingerprint agreement:
                        # label it apart and leave the percent empty so the
                        # board files the pair under "needs a look".
                        score = group.match_scores.get(member.path)
                        member_payload["similarity_percent"] = None
                        member_payload["orientation_label"] = None
                        member_payload["match_label"] = (
                            f"{score * 100:.0f}% look-alike" if score is not None else None
                        )
                        continue
                    member_payload["similarity_percent"] = similarity_percent(member, keeper)
                    orientation = (
                        match_orientation(member, keeper) if member is not keeper else None
                    )
                    member_payload["orientation_label"] = (
                        ORIENTATION_LABELS[orientation] if orientation else None
                    )
        if group.kind == GroupKind.CROP:
            cropped = group.match_details.get("cropped") or {}
            reviewed = set(group.reviewed_paths)
            for member, member_payload in zip(group.members, payload["members"], strict=True):
                entry = cropped.get(member.path)
                box = CropBox.decode(entry["box"]) if entry else suggested_crop(member)
                member_payload["crop"] = box.to_dict() if box else None
                member_payload["crop_state"] = (
                    "cropped" if entry else "skipped" if member.path in reviewed else "pending"
                )
                if entry:
                    member_payload["crop_undo_available"] = bool(entry.get("original"))
            payload["cropped_paths"] = [
                member.path for member in group.members if member.path in cropped
            ]
        agreements = [m["similarity_percent"] for m in payload["members"]
                      if m.get("similarity_percent") is not None
                      and m["path"] != group.suggested_keep]
        payload["cleanup"] = cleanup_opportunity(group, agreements)
        deleted = state["deleted_files"]
        payload["deleted_paths"] = [
            member.path for member in group.members if member.path in deleted
        ]
        return payload

    def allowed_paths_locked() -> frozenset:
        """Paths media routes may serve, cached per result/paths_version.

        Rebuilding this set used to cost O(all files + all members) under the
        global lock on every thumbnail/media request; membership only changes
        when paths_version bumps or the result object is replaced.
        """
        result: ScanResult | None = state["result"]
        key = (id(result), state["paths_version"])
        cached = state["allowed_paths_cache"]
        if cached is not None and cached[0] == key:
            return cached[1]
        allowed: set[str] = set()
        if result is not None:
            allowed.update(file.path for file in result.files)
            for group in result.groups:
                allowed.update(member.path for member in group.members)
        frozen = frozenset(allowed)
        state["allowed_paths_cache"] = (key, frozen)
        return frozen

    def is_scanned_file(path: Path, raw_path: str) -> bool:
        """Return whether a file belongs to the active local review session."""
        with lock:
            allowed = allowed_paths_locked()
        return raw_path in allowed or str(path.resolve()) in allowed

    def note_group_streamed_locked(group) -> None:
        """Fold one streamed group into the served stats (lock held).

        Selections are locked while a scan runs and fresh groups carry no
        review decisions, so per-group contributions are independent and the
        old per-group recompute_stats() (O(all members) per group, O(n^2) per
        scan) is unnecessary. Exact and similar groups never share member
        paths, and independent review groups start with empty selections, so
        per-group effective selections cannot double-count here. Post-scan
        mutations still recompute in full.
        """
        result: ScanResult | None = state["result"]
        if result is None:
            return
        if group.kind == GroupKind.EXACT:
            result.exact_groups += 1
        elif group.kind == GroupKind.SIMILAR:
            result.similar_groups += 1
        elif group.kind == GroupKind.NO_HUMANS:
            result.no_human_files += len(group.members)
        elif group.kind == GroupKind.LOW_RESOLUTION:
            result.low_resolution_files += len(group.members)
        elif group.kind == GroupKind.RANDOM_REVIEW:
            result.random_review_files += len(group.members)
        elif group.kind == GroupKind.FACES:
            result.faces_files += len(group.members)
        elif group.kind == GroupKind.CROP:
            result.crop_files += len(group.members)
        selected = effective_selected_paths([group])
        sizes = {member.path: member.size for member in group.members}
        result.reclaimable_bytes += sum(sizes.get(path, 0) for path in selected)
        state["selected_count"] += len(selected)

    def refresh_selected_count_locked() -> None:
        """Full refresh after post-scan selection/membership mutations."""
        result: ScanResult | None = state["result"]
        if result is not None:
            for group in result.groups:
                protected = {m.path for m in group.members if m.protected}
                group.selected_for_removal = [
                    p for p in group.selected_for_removal if p not in protected
                ]
        state["selected_count"] = (
            len(effective_selected_paths(result.groups)) if result is not None else 0
        )

    @app.before_request
    def cancel_pending_shutdown():
        """A reloaded/reopened page cancels a shutdown scheduled on pagehide."""
        if request.path == "/api/shutdown":
            return
        with lock:
            timer = state.get("shutdown_timer")
            if timer is not None:
                timer.cancel()
                state["shutdown_timer"] = None
        return

    @app.before_request
    def protect_mutating_api():
        mutating = request.path.startswith("/api/") and request.method not in (
            "GET",
            "HEAD",
            "OPTIONS",
        )
        reveal_side_effect = (
            request.path == "/api/reveal" and request.args.get("open") == "1"
        )
        if not (mutating or reveal_side_effect):
            return None
        if mutating and not request.is_json:
            return jsonify({"error": "application/json required"}), 415
        supplied = request.headers.get("X-Dedupe-Token", "")
        if not hmac.compare_digest(supplied, csrf_token):
            return jsonify({"error": "invalid local session token"}), 403
        origin = request.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                return jsonify({"error": "cross-origin request rejected"}), 403
        return None

    @app.after_request
    def local_security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        immutable = "immutable" in response.headers.get("Cache-Control", "")
        if request.path.startswith("/api/") and not immutable:
            response.headers["Cache-Control"] = "no-store"
        return response

    if initial_result is not None:
        with lock:
            # A results file from an earlier scan predates later distinct
            # reviews; drop what a fresh scan would suppress before installing
            # it, so marked groups do not resurface (and are not saved back).
            drop_marked_distinct_groups(initial_result, app.config["DEDUPE_CACHE_PATH"])
            ensure_all_files_groups(initial_result)
            state["result"] = initial_result
            initial_result.recompute_stats()
            state["progress"] = ScanProgress(
                phase="done",
                done=True,
                files_found=len(initial_result.files),
                groups_found=len(initial_result.groups),
                message="Loaded previous scan",
                elapsed_seconds=initial_result.diagnostics.total_duration_seconds,
            )
            refresh_selected_count_locked()
        persist_result()
    elif loaded.available:
        # Nothing to install at startup: the peeked session is banner
        # metadata only. The resume endpoint builds, heals, revalidates,
        # and installs the saved review when the user asks for it.
        pass

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=csrf_token)

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify({"jobs": jobs.list()})

    @app.post("/api/jobs")
    def api_save_job():
        data = request.get_json(silent=True) or {}
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "Cleanup jobs are locked during active work"}), 409
            try:
                saved = jobs.save(data.get("name"), data.get("settings"), data.get("id"))
            except (OSError, ValueError, TypeError) as exc:
                return jsonify({"error": str(exc)}), 400
        return jsonify(saved)

    @app.post("/api/jobs/delete")
    def api_delete_job():
        data = request.get_json(silent=True) or {}
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "Cleanup jobs are locked during active work"}), 409
            try:
                jobs.remove(data.get("id"))
                if state["result"] and state["result"].job_id == data.get("id"):
                    state["result"].job_id = None
                    state["result"].scan_settings.pop("job_id", None)
                    persist_result()
                else:
                    saved = load_review_session(review_session_path)
                    if saved.result and saved.result.job_id == data.get("id"):
                        saved.result.job_id = None
                        saved.result.scan_settings.pop("job_id", None)
                        save_review_session(saved.result, review_session_path, saved.deleted_files)
                        if state["result"] is None:
                            state["review_session"] = peek_review_session(review_session_path)
                if (state.get("last_scan_settings") or {}).get("job_id") == data.get("id"):
                    state["last_scan_settings"].pop("job_id", None)
            except (OSError, ValueError, TypeError) as exc:
                return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.get("/api/history")
    def api_history():
        from ..history import list_history

        with lock:
            if state["acting"]:
                return jsonify({"error": "Wait for the current file action to finish"}), 409
            return jsonify(list_history(
                cache_path=app.config["DEDUPE_CACHE_PATH"],
                limit=request.args.get("limit", 50, type=int),
                offset=request.args.get("offset", 0, type=int),
            ))

    @app.post("/api/history/reconsider")
    def api_history_reconsider():
        from ..history import reconsider_distinct_decision

        data = request.get_json(silent=True) or {}
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "History is locked during active work"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            try:
                outcome = reconsider_distinct_decision(
                    data.get("path_a"), data.get("path_b"),
                    cache_path=app.config["DEDUPE_CACHE_PATH"],
                )
            except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
                return jsonify({"error": str(exc)}), 400
        return jsonify(outcome)

    @app.get("/favicon.ico")
    def favicon():
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<rect width="64" height="64" rx="16" fill="#5b9dff"/>'
            '<g fill="none" stroke="#0a2940" stroke-width="5">'
            '<rect x="12" y="18" width="30" height="28" rx="6"/>'
            '<rect x="22" y="20" width="30" height="28" rx="6"/>'
            "</g></svg>"
        )
        return Response(svg, mimetype="image/svg+xml")

    def status_payload_locked() -> dict:
        """Build the /api/status payload (lock held; shared with the SSE stream).

        Stats fields are maintained incrementally while groups stream in and
        recomputed at every post-scan mutation site, so serving a poll is O(1)
        in result size instead of rewalking every group and member per call.
        """
        from ..parallel import DEFAULT_WORKERS_CAP, resolve_workers

        prog = state["progress"]
        result: ScanResult | None = state["result"]
        cpu = os.cpu_count() or 1
        payload = {
            "scanning": state["scanning"],
            "acting": state["acting"],
            "progress": prog.to_dict(),
            "has_result": result is not None,
            "web_api_version": WEB_API_VERSION,
            "groups_version": state["groups_version"],
            "scan_id": state["scan_id"],
            "error": state["last_error"],
            "streams": [dict(stream) for stream in state["streams"]],
            "review_session": state["review_session"].metadata(),
            "capabilities": capabilities,
            "keep_decisions_error": state["keep_decisions_error"],
            "trash_undo_cleared": state["trash_undo_cleared"],
            "recovered_actions": len(recovered_actions),
            "scan_settings": state.get("last_scan_settings") or (result.scan_settings if result else {}),
            "system": {
                "cpu_count": cpu,
                "auto_workers": resolve_workers(None),
                "max_workers": max(cpu, DEFAULT_WORKERS_CAP),
                "workers_cap": DEFAULT_WORKERS_CAP,
            },
        }
        if result is not None:
            payload["summary"] = {
                "roots": result.roots,
                "file_count": len(result.files),
                "group_count": len(result.groups),
                "exact_groups": result.exact_groups,
                "similar_groups": result.similar_groups,
                "no_human_files": result.no_human_files,
                "low_resolution_files": result.low_resolution_files,
                "random_review_files": result.random_review_files,
                "faces_files": result.faces_files,
                "crop_files": result.crop_files,
                "reclaimable_bytes": result.reclaimable_bytes,
                "reclaimable_human": format_bytes(result.reclaimable_bytes),
                "selected_count": state["selected_count"],
                "errors": list(result.errors[:20]),
                "errors_total": len(result.errors),
                "diagnostics": result.diagnostics.to_dict(),
            }
        return payload

    @app.get("/api/status")
    def api_status():
        with lock:
            return jsonify(status_payload_locked())

    @app.get("/api/events")
    def api_events():
        """Server-sent events: scan status, streamed groups, and resync hints.

        Replaces client-side polling during scans. Events:
          - ``status``: the /api/status payload (throttled while scanning,
            immediate on state transitions)
          - ``group``: one streamed group payload, in discovery order
          - ``reset``: the group list was replaced (scan finished, new scan,
            resume/discard); the client should refetch /api/groups
        A comment heartbeat keeps idle connections alive.
        """
        import json as _json

        def stream():
            result_token = None
            sent_groups = 0
            seen_groups_version = -1
            last_snapshot = None
            last_status_emit = 0.0
            last_any_emit = 0.0
            while True:
                events: list[tuple[str, dict]] = []
                with lock:
                    result: ScanResult | None = state["result"]
                    # id() ints are safe identity tokens here: a replaced
                    # result stays referenced by the review session, so its
                    # id cannot be recycled while a connection tracks it.
                    token = id(result) if result is not None else None
                    if token != result_token:
                        # Result object replaced (new scan, completion, resume,
                        # discard): per-connection append tracking is invalid.
                        result_token = token
                        sent_groups = 0
                        events.append(
                            (
                                "reset",
                                {
                                    "groups_version": state["groups_version"],
                                    "scan_id": state["scan_id"],
                                },
                            )
                        )
                        if not state["scanning"]:
                            # Completion/resume/discard: the client refetches
                            # the authoritative list on reset, so skip deltas.
                            seen_groups_version = state["groups_version"]
                        # While scanning, leave the seen version stale: groups
                        # appended since the replacement stream as deltas below.
                    if (
                        result is not None
                        and state["groups_version"] != seen_groups_version
                    ):
                        for group in result.groups[sent_groups:]:
                            events.append(("group", group_payload(group)))
                        sent_groups = len(result.groups)
                        seen_groups_version = state["groups_version"]

                    prog = state["progress"]
                    snapshot = (
                        state["scanning"],
                        state["acting"],
                        prog.phase,
                        prog.done,
                        prog.files_processed,
                        prog.groups_found,
                        state["last_error"],
                        state["groups_version"],
                        state["selected_count"],
                    )
                    now = time.monotonic()
                    transitioned = snapshot != last_snapshot
                    due = state["scanning"] and now - last_status_emit >= 0.25
                    if transitioned or due:
                        events.append(("status", status_payload_locked()))
                        last_snapshot = snapshot
                        last_status_emit = now
                    scanning = state["scanning"]

                if events:
                    lines = []
                    for name, payload in events:
                        lines.append(
                            f"event: {name}\ndata: {_json.dumps(payload)}\n\n"
                        )
                    last_any_emit = time.monotonic()
                    yield "".join(lines)
                elif time.monotonic() - last_any_emit >= 30.0:
                    last_any_emit = time.monotonic()
                    yield ": heartbeat\n\n"

                with lock:
                    # Coalesce per-file progress notifications into one wakeup
                    # cadence while scanning; sleep until notified when idle.
                    state["events"].wait(timeout=0.25 if scanning else 30.0)

        response = Response(stream(), mimetype="text/event-stream")
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.get("/api/review-session")
    def api_review_session():
        with lock:
            return jsonify(state["review_session"].metadata())

    @app.post("/api/review-session/resume")
    def api_review_session_resume():
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "review is locked during active work"}), 409
            if state["persist_dirty"] and not persist_result():
                return jsonify({"error": "Could not save the current review before switching"}), 400
            data = request.get_json(silent=True) or {}
            try:
                source = (jobs.session_path(data["job_id"])
                          if data.get("job_id") is not None else review_session_path)
                report = load_review_session(source)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            state["review_session"] = report
            if report.result is None:
                return jsonify(report.metadata()), 404
            # Heals sessions saved before a distinct review (e.g. poisoned by
            # an earlier --load): covered groups never reach the review list.
            drop_marked_distinct_groups(report.result, app.config["DEDUPE_CACHE_PATH"])
            ensure_all_files_groups(report.result)
            state["result"] = report.result
            state["last_scan_settings"] = report.result.scan_settings
            state["scan_id"] = secrets.token_hex(12)
            state["deleted_files"] = dict(report.deleted_files)
            state["groups_version"] += 1
            state["paths_version"] += 1
            refresh_selected_count_locked()
            state["progress"] = ScanProgress(
                phase="done",
                done=True,
                files_found=len(report.result.files),
                groups_found=len(report.result.groups),
                message="Resumed saved review",
            )
            payload = report.metadata()
            payload["scan_id"] = state["scan_id"]
            return jsonify(payload)

    @app.delete("/api/review-session")
    def api_review_session_discard():
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "review is locked during active work"}), 409
            try:
                removed = discard_review_session(review_session_path)
            except OSError as exc:
                # A filesystem failure, not a server bug — same status the
                # other file-touching endpoints use for OSError.
                return jsonify({"error": str(exc)}), 400
            old_path = state["review_session"].path
            state["review_session"] = ReviewSessionLoad(path=old_path)
            state["result"] = None
            state["deleted_files"] = {}
            state["scan_id"] = secrets.token_hex(12)
            state["groups_version"] += 1
            state["paths_version"] += 1
            state["selected_count"] = 0
            state["progress"] = ScanProgress()
        return jsonify({"ok": True, "discarded": removed})

    def int_arg(name: str, default: int | None = None) -> int | None:
        raw = request.args.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    @app.get("/api/groups")
    def api_groups():
        kind = request.args.get("kind")  # exact | similar | all
        # No limit → full list, so older clients and callers behave as before.
        limit = int_arg("limit")
        offset = max(0, int_arg("offset", 0) or 0)
        with lock:
            result: ScanResult | None = state["result"]
            if result is None:
                return jsonify(
                    {
                        "groups": [],
                        "total": 0,
                        "offset": offset,
                        "limit": limit,
                        "groups_version": state["groups_version"],
                    }
                )
            groups = result.groups
            if kind in (
                "exact",
                "similar",
                "no_humans",
                "low_resolution",
                "random_review",
                "faces",
                "all_files",
                "sets",
                "crop",
            ):
                groups = [g for g in groups if g.kind.value == kind]
            total = len(groups)
            if limit is not None:
                groups = groups[offset : offset + max(0, limit)]
            elif offset:
                groups = groups[offset:]
            return jsonify(
                {
                    "groups": [group_payload(g) for g in groups],
                    "total": total,
                    "offset": offset,
                    "limit": limit,
                    "groups_version": state["groups_version"],
                }
            )

    @app.get("/api/groups/<group_id>")
    def api_group(group_id: str):
        with lock:
            result: ScanResult | None = state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            for g in result.groups:
                if g.id == group_id:
                    return jsonify(group_payload(g))
        return jsonify({"error": "not found"}), 404

    def people_scoring_payload_locked() -> dict:
        return dict(state["people_scoring"])

    @app.get("/api/people-scores")
    def api_people_scores_status():
        with lock:
            return jsonify(people_scoring_payload_locked())

    @app.post("/api/people-scores")
    def api_people_scores_start():
        """Score one group's members for "least likely to show people" ordering.

        Scoring only reads files and writes advice (a 0–1 score per file) to
        the records, the hash cache, and the review session. It never selects
        or moves anything, so it is not an action and holds no action lock;
        it is refused during scans because the result is about to be replaced.
        """
        data = request.get_json(silent=True) or {}
        group_id = data.get("group_id")
        with lock:
            if not capabilities.get("people_score"):
                return jsonify({"error": people_scoring_available()[1]}), 400
            if state["scanning"]:
                return jsonify({"error": "people scoring waits until the scan finishes"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            group = next((g for g in result.groups if g.id == group_id), None)
            if group is None:
                return jsonify({"error": "not found"}), 404
            scoring = state["people_scoring"]
            if scoring["running"]:
                if scoring["group_id"] == group_id and scoring["scan_id"] == state["scan_id"]:
                    return jsonify(people_scoring_payload_locked())
                return jsonify({"error": "another folder is still being scored"}), 409
            pending = [m for m in group.members if not has_current_people_score(m)]
            scan_id = state["scan_id"]
            state["people_scoring"] = {
                "running": bool(pending),
                "scan_id": scan_id,
                "group_id": group_id,
                "done": 0,
                "total": len(pending),
                "error": None,
            }
            if not pending:
                return jsonify(people_scoring_payload_locked())

        def still_current() -> bool:
            with lock:
                return state["scan_id"] == scan_id and not state["scanning"]

        def progress(done: int, _total: int) -> None:
            with lock:
                if state["people_scoring"]["scan_id"] == scan_id:
                    state["people_scoring"]["done"] = done

        def work() -> None:
            error = None
            scored: list = []
            try:
                scored = score_people(
                    pending,
                    progress=progress,
                    cancelled=lambda: not still_current(),
                )
            except InterruptedError:
                error = "scoring stopped because the results changed"
            except Exception as exc:  # model failure: report, never crash the server
                error = f"people scoring failed: {exc}"
            if scored:
                cache = None
                try:
                    cache = HashCache(app.config["DEDUPE_CACHE_PATH"])
                    cache.store_all([record for record in scored if record.people_score is not None])
                except (OSError, sqlite3.Error):
                    pass  # scores still apply to this session; they just are not cached
                finally:
                    if cache is not None:
                        cache.close()
            with lock:
                if state["people_scoring"]["scan_id"] == scan_id:
                    state["people_scoring"]["running"] = False
                    state["people_scoring"]["error"] = error
                    state["people_scoring"]["done"] = state["people_scoring"]["total"]
            if error is None and still_current():
                schedule_persist()

        threading.Thread(target=work, name="dedupe-people-scores", daemon=True).start()
        with lock:
            return jsonify(people_scoring_payload_locked()), 202

    @app.post("/api/scan")
    def api_scan():
        data = request.get_json(silent=True) or {}
        job = None
        if data.get("job_id") is not None:
            try:
                job = jobs.get(data["job_id"])
            except (OSError, ValueError) as exc:
                return jsonify({"error": f"Could not load cleanup job: {exc}"}), 400
            data = {**job["settings"], **data}
        paths = data.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        paths = [p for p in paths if p and str(p).strip()]
        if not paths:
            return jsonify({"error": "paths required"}), 400

        protected_folders = data.get("protected_folders") or []
        if (not isinstance(protected_folders, list)
                or any(not isinstance(p, str) or not p.strip() for p in protected_folders)):
            return jsonify({"error": "protected_folders must be a list of folder paths"}), 400
        protected_folders = [str(Path(p).expanduser().resolve()) for p in protected_folders]
        if any(not Path(p).is_dir() for p in protected_folders):
            return jsonify({"error": "Each protected library must be an existing folder"}), 400
        if protected_folders and data.get("parallel_streams"):
            return jsonify({"error": "Protected libraries require comparing folders together"}), 400
        if data.get("new_only") and (job is None or "baseline" not in job):
            return jsonify({"error": "Run this saved job once before checking new arrivals"}), 400
        if data.get("new_only"):
            settings = {key: value for key, value in data.items()
                        if key not in {"job_id", "new_only", "scan_id", "retry"}}
            if settings != job["settings"]:
                return jsonify({"error": "Settings changed; run a full scan before checking new arrivals"}), 400

        preferred_folders = data.get("preferred_folders") or []
        if (not isinstance(preferred_folders, list)
                or any(not isinstance(p, str) or not p.strip() for p in preferred_folders)):
            return jsonify({"error": "preferred_folders must be a list of folder paths"}), 400
        human_backend = str(data.get("human_backend", "opencv")).strip().lower()
        if human_backend not in HUMAN_BACKENDS:
            return jsonify({"error": f"unknown human detector: {human_backend}"}), 400
        photon_model = str(data.get("photon_model", DEFAULT_PHOTON_MODEL)).strip()
        if not photon_model:
            photon_model = DEFAULT_PHOTON_MODEL

        resolved_roots = [str(Path(p).expanduser().resolve()) for p in paths]

        with lock:
            if state["scanning"]:
                return jsonify({"error": "scan already running"}), 409
            if state["acting"]:
                return jsonify({"error": "file action already running"}), 409
            if state["persist_dirty"] and not persist_result():
                return jsonify({"error": "Could not save the current review before scanning"}), 400
            if job and not data.get("new_only"):
                try:
                    jobs.save(job["name"], data, job["id"])
                    job = jobs.get(job["id"])
                except (OSError, ValueError) as exc:
                    return jsonify({"error": f"Could not save cleanup job: {exc}"}), 400
            state["last_scan_settings"] = dict(data)
            scan_id = secrets.token_hex(12)
            previous_result = state["result"]
            previous_deleted = dict(state["deleted_files"])
            cancel_event = threading.Event()
            state["scanning"] = True
            state["scan_id"] = scan_id
            state["cancel_event"] = cancel_event
            state["last_error"] = None
            state["deleted_files"] = {}
            # A new scan ends in-app undo for anything trashed earlier; the
            # status payload reports this once so it is not a silent loss.
            state["trash_undo_cleared"] = len(previous_deleted)
            state["progress"] = ScanProgress(phase="starting", message="Starting…")
            # Empty result so the UI can stream groups as they appear.
            state["result"] = ScanResult(
                roots=list(dict.fromkeys([*resolved_roots, *protected_folders])),
                files=[], groups=[], protected_folders=protected_folders,
            )
            state["preview_tokens"] = {}
            state["groups_version"] = state.get("groups_version", 0) + 1
            state["paths_version"] += 1
            state["selected_count"] = 0
            state["streamed_group_index"] = {}
            # Compare every selected root together by default. Independent streams
            # intentionally exclude cross-folder matches and must be opted into.
            parallel_streams = bool(data.get("parallel_streams", False))
            state["streams"] = (
                [
                    {
                        "index": i,
                        "root": root,
                        "phase": "starting",
                        "message": "Queued…",
                        "files_found": 0,
                        "files_processed": 0,
                        "groups_found": 0,
                        "done": False,
                    }
                    for i, root in enumerate(resolved_roots)
                ]
                if parallel_streams
                else []
            )

        def worker() -> None:
            try:

                def on_progress(prog: ScanProgress) -> None:
                    with lock:
                        if state["scan_id"] == scan_id:
                            state["progress"] = prog
                            state["events"].notify_all()

                def on_group(group) -> None:
                    with lock:
                        if data.get("new_only"):
                            return  # Publish only the focused result after inventory completes.
                        result: ScanResult | None = state["result"]
                        if result is None or state["scan_id"] != scan_id:
                            return
                        # Append in discovery order and bump the version; the
                        # per-group full sort + recompute_stats this replaced
                        # cost O(groups^2 log n) per scan. The client keeps its
                        # own sorted view, and the final result (which replaces
                        # this one at completion) is sorted by the engine.
                        index = state["streamed_group_index"]
                        existing = index.get(group.id)
                        if existing is not None:
                            # Shouldn't happen; fall back to a full recompute.
                            result.groups[existing] = group
                            result.recompute_stats()
                            refresh_selected_count_locked()
                        else:
                            index[group.id] = len(result.groups)
                            result.groups.append(group)
                            note_group_streamed_locked(group)
                        state["groups_version"] = state.get("groups_version", 0) + 1
                        state["paths_version"] += 1
                        prog = state["progress"]
                        prog.groups_found = len(result.groups)
                        state["events"].notify_all()

                def on_stream_progress(prog: ScanProgress) -> None:
                    with lock:
                        if state["scan_id"] != scan_id:
                            return
                        streams = state["streams"]
                        idx = prog.stream_index
                        if idx is None or idx >= len(streams):
                            return
                        streams[idx] = {
                            "index": idx,
                            "root": prog.root,
                            "phase": prog.phase,
                            "message": prog.message,
                            "files_found": prog.files_found,
                            "files_processed": prog.files_processed,
                            "groups_found": prog.groups_found,
                            "done": prog.done,
                        }
                        state["events"].notify_all()

                raw_workers = data.get("workers", None)
                if raw_workers in ("", None):
                    workers = None
                else:
                    try:
                        workers = int(raw_workers)
                    except (TypeError, ValueError):
                        workers = None
                    if workers is not None and workers <= 0:
                        workers = None

                raw_exclusions = data.get("exclusions") or []
                if isinstance(raw_exclusions, str):
                    raw_exclusions = raw_exclusions.split(",")

                scan_kwargs = {
                    "preferred_folders": preferred_folders,
                    "protected_folders": protected_folders,
                    "video_versions": bool(data.get("video_versions", False)),
                    "exact": bool(data.get("exact", True)),
                    "similar": bool(data.get("similar", True)),
                    "semantic": bool(data.get("semantic", False)),
                    "semantic_threshold": _cosine(data.get("semantic_threshold"), 0.88),
                    "semantic_duplicate_threshold": _cosine(
                        data.get("semantic_duplicate_threshold"), 0.97
                    ),
                    "decider": bool(data.get("semantic", False))
                    and bool(data.get("decider", False)),
                    "find_no_humans": bool(data.get("find_no_humans", False)),
                    "count_faces": bool(data.get("count_faces", False)),
                    "find_black_borders": bool(data.get("find_black_borders", False)),
                    "find_low_resolution": bool(data.get("find_low_resolution", True)),
                    "low_resolution_images": bool(data.get("low_resolution_images", True)),
                    "low_resolution_gifs": bool(data.get("low_resolution_gifs", True)),
                    "low_resolution_videos": bool(data.get("low_resolution_videos", True)),
                    "low_resolution_image_max_pixels": max(
                        1, int(data.get("low_resolution_image_max_pixels") or 1_000_000)
                    ),
                    "low_resolution_gif_max_pixels": max(
                        1, int(data.get("low_resolution_gif_max_pixels") or 1_000_000)
                    ),
                    "low_resolution_video_max_pixels": max(
                        1, int(data.get("low_resolution_video_max_pixels") or 1_000_000)
                    ),
                    "random_review_count": max(0, int(data.get("random_review_count", 50))),
                    "human_backend": human_backend,
                    "photon_model": photon_model,
                    "include_images": bool(data.get("include_images", True)),
                    "include_gifs": bool(data.get("include_gifs", True)),
                    "include_videos": bool(data.get("include_videos", True)),
                    "include_hidden": bool(data.get("include_hidden", False)),
                    "image_threshold": int(data.get("threshold", 6)),
                    "video_threshold": int(data.get("video_threshold", 8)),
                    "use_cache": bool(data.get("use_cache", True)),
                    "cache_path": app.config["DEDUPE_CACHE_PATH"],
                    "workers": workers,
                    "exclusions": [
                        str(pattern).strip()
                        for pattern in raw_exclusions
                        if str(pattern).strip()
                    ],
                    "cancelled": cancel_event.is_set,
                    "progress": on_progress,
                    "on_group": on_group,
                }
                if parallel_streams:
                    result = run_scans_parallel(
                        paths,
                        on_stream_progress=on_stream_progress,
                        **scan_kwargs,
                    )
                else:
                    result = run_scan(paths, **scan_kwargs)
                with lock:
                    if state["scan_id"] != scan_id:
                        return
                    if not result.roots and result.errors:
                        raise RuntimeError("; ".join(result.errors))
                    ensure_all_files_groups(result)
                    result.protected_folders = protected_folders
                    result.job_id = job["id"] if job else None
                    result.scan_settings = {
                        key: value for key, value in data.items()
                        if key not in {"scan_id", "retry"}
                    }
                    arrivals = None
                    if data.get("new_only"):
                        arrivals = focus_new_arrivals(result, job["baseline"])
                    state["result"] = result
                    state["groups_version"] = state.get("groups_version", 0) + 1
                    state["paths_version"] += 1
                    state["streamed_group_index"] = {}
                    refresh_selected_count_locked()
                    state["progress"] = replace(
                        state["progress"],
                        phase="done",
                        done=True,
                        files_found=len(result.files),
                        groups_found=len(result.groups),
                        message=(
                            (f"{arrivals} new or changed files — " if arrivals is not None else "Done — ")
                            + scan_done_summary(result, scan_kwargs)
                        ),
                        elapsed_seconds=result.diagnostics.total_duration_seconds,
                        eta_seconds=0.0,
                    )
                    if not persist_result():
                        raise OSError(state["last_error"])
                    if job:
                        jobs.finish_scan(result)
                    state["scanning"] = False
                    state["cancel_event"] = None
                    state["events"].notify_all()
            except Exception as exc:
                with lock:
                    if state["scan_id"] != scan_id:
                        return
                    was_cancelled = isinstance(exc, InterruptedError)
                    state["result"] = previous_result
                    state["deleted_files"] = previous_deleted
                    state["trash_undo_cleared"] = 0  # the undo map survived after all
                    state["scan_id"] = secrets.token_hex(12)
                    state["groups_version"] += 1
                    state["paths_version"] += 1
                    state["streamed_group_index"] = {}
                    refresh_selected_count_locked()
                    state["preview_tokens"] = {}
                    state["scanning"] = False
                    state["cancel_event"] = None
                    state["last_error"] = None if was_cancelled else str(exc)
                    state["progress"] = ScanProgress(
                        phase="cancelled" if was_cancelled else "error",
                        done=True,
                        error=None if was_cancelled else str(exc),
                        message="Scan cancelled" if was_cancelled else str(exc),
                    )
                    state["events"].notify_all()

        with lock:
            state["events"].notify_all()  # wake SSE clients: scanning=True
        threading.Thread(target=worker, daemon=True).start()
        return jsonify({"ok": True, "scan_id": scan_id})

    @app.post("/api/scan/cancel")
    def api_scan_cancel():
        data = request.get_json(silent=True) or {}
        with lock:
            event: threading.Event | None = state.get("cancel_event")
            if not state["scanning"] or event is None:
                return jsonify({"error": "no scan is running"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session"}), 409
            event.set()
            state["progress"].message = "Cancelling after current work item…"
        return jsonify({"ok": True})

    @app.post("/api/scan/check-exclusions")
    def api_scan_check_exclusions():
        """Report what each exclusion glob would match under the given roots.

        A glob that matches nothing is usually a typo; the scan would silently
        treat it as dead weight. The walk is bounded and read-only.
        """
        data = request.get_json(silent=True) or {}
        paths = data.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        paths = [str(p) for p in paths if p and str(p).strip()]
        if not paths:
            return jsonify({"error": "paths required"}), 400
        raw_exclusions = data.get("exclusions") or []
        if isinstance(raw_exclusions, str):
            raw_exclusions = raw_exclusions.split(",")
        exclusions = [str(p).strip() for p in raw_exclusions if str(p).strip()]
        if not exclusions:
            return jsonify({"error": "exclusions required"}), 400
        from ..scanner import preview_exclusions

        return jsonify(preview_exclusions(paths, exclusions))

    @app.post("/api/smart-select")
    def api_smart_select():
        data = request.get_json(silent=True) or {}
        rule_raw = data.get("rule", SmartRule.AUTOMATIC.value)
        group_id = data.get("group_id")
        group_ids = data.get("group_ids")
        if group_ids is not None and not isinstance(group_ids, list):
            return jsonify({"error": "group_ids must be a list"}), 400
        try:
            rule = SmartRule(rule_raw)
        except ValueError:
            return jsonify({"error": f"invalid rule: {rule_raw}"}), 400

        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "selections are locked during active work"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            if group_id:
                for g in result.groups:
                    if g.id == group_id:
                        apply_smart_select(g, rule)
                        result.recompute_stats()
                        refresh_selected_count_locked()
                        payload = group_payload(g)
                        schedule_persist()
                        return jsonify(payload)
                return jsonify({"error": "not found"}), 404
            if group_ids is None:
                scoped = result.groups
            else:
                wanted = {str(value) for value in group_ids}
                scoped = [g for g in result.groups if g.id in wanted]
            apply_smart_select_all(scoped, rule)
            result.recompute_stats()
            refresh_selected_count_locked()
            schedule_persist()
            return jsonify({"ok": True, "group_count": len(scoped)})

    @app.post("/api/selection")
    def api_selection():
        """Set selected_for_removal for a group (manual checkboxes)."""
        data = request.get_json(silent=True) or {}
        group_id = data.get("group_id")
        selected = list(data.get("selected") or [])
        if not group_id:
            return jsonify({"error": "group_id required"}), 400

        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "selections are locked during active work"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            for g in result.groups:
                if g.id == group_id:
                    if g.kind == GroupKind.CROP:
                        return jsonify({
                            "error": "crop suggestions are applied or skipped, not selected"
                        }), 400
                    member_paths = {m.path for m in g.members}
                    protected_paths = {m.path for m in g.members if m.protected}
                    decision_path = data.get("decision_path")
                    if decision_path is not None:
                        decision_path = str(decision_path)
                        if (
                            g.policy != ReviewPolicy.INDEPENDENT_CANDIDATES
                            or decision_path not in member_paths
                        ):
                            return jsonify({"error": "candidate decision is not in this group"}), 400
                        remove = data.get("decision_remove")
                        if remove and decision_path in protected_paths:
                            return jsonify({"error": "This file is in a protected library"}), 400
                        if not isinstance(remove, bool):
                            return jsonify({"error": "decision_remove must be a boolean"}), 400
                        # The newest arrow-key decision wins in every overlapping
                        # independent branch. Keep also clears duplicate picks;
                        # effective_selected_paths preserves that veto globally.
                        for candidate in result.groups:
                            candidate_paths = {member.path for member in candidate.members}
                            if decision_path not in candidate_paths:
                                continue
                            if candidate.policy == ReviewPolicy.INDEPENDENT_CANDIDATES:
                                candidate.reviewed_paths = list(
                                    dict.fromkeys([*candidate.reviewed_paths, decision_path])
                                )
                                selected_paths = set(candidate.selected_for_removal)
                                if remove:
                                    selected_paths.add(decision_path)
                                else:
                                    selected_paths.discard(decision_path)
                                candidate.selected_for_removal = [
                                    member.path
                                    for member in candidate.members
                                    if member.path in selected_paths
                                ]
                            elif not remove:
                                candidate.selected_for_removal = [
                                    path
                                    for path in candidate.selected_for_removal
                                    if path != decision_path
                                ]
                        result.recompute_stats()
                        refresh_selected_count_locked()
                        state["keep_decisions_error"] = sync_low_resolution_keeps(
                            result, {decision_path}
                        )
                        payload = group_payload(g)
                        schedule_persist()
                        return jsonify(payload)
                    picks = [p for p in selected if p in member_paths and p not in protected_paths]
                    # Duplicate groups retain one file; independent review candidates may remove all.
                    if (
                        g.policy == ReviewPolicy.KEEP_ONE
                        and len(picks) >= len(member_paths)
                        and member_paths
                    ):
                        keep = g.suggested_keep or next(iter(member_paths))
                        if keep in picks:
                            picks = [p for p in picks if p != keep]
                        else:
                            picks = picks[:-1]
                    g.selected_for_removal = picks
                    if g.policy == ReviewPolicy.INDEPENDENT_CANDIDATES:
                        if "reviewed" not in data:
                            return jsonify({"error": "reviewed paths required for candidate updates"}), 400
                        reviewed = list(data.get("reviewed") or [])
                        g.reviewed_paths = [
                            path for path in reviewed if path in member_paths
                        ]
                    if g.kind == GroupKind.LOW_RESOLUTION:
                        state["keep_decisions_error"] = sync_low_resolution_keeps(
                            result, member_paths
                        )
                    result.recompute_stats()
                    refresh_selected_count_locked()
                    payload = group_payload(g)
                    schedule_persist()
                    return jsonify(payload)
        return jsonify({"error": "not found"}), 404

    @app.post("/api/selection/bulk")
    def api_selection_bulk():
        """Apply one selection operation across many groups, server-side invariants only.

        The client sends the group ids it currently shows; every safety rule
        (keep one member of every duplicate group, never select a keeper, only
        real members) is re-derived here from server state.
        """
        data = request.get_json(silent=True) or {}
        operation = str(data.get("operation") or "").lower()
        if operation not in BULK_SELECT_OPERATIONS:
            return jsonify({"error": f"unknown bulk operation: {operation}"}), 400
        group_ids = data.get("group_ids")
        if group_ids is not None and not isinstance(group_ids, list):
            return jsonify({"error": "group_ids must be a list"}), 400
        raw_criteria = data.get("criteria") or {}
        if not isinstance(raw_criteria, dict):
            return jsonify({"error": "criteria must be an object"}), 400
        try:
            criteria = parse_bulk_criteria(raw_criteria)
        except (TypeError, ValueError) as exc:
            return jsonify({"error": f"invalid criteria: {exc}"}), 400

        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "selections are locked during active work"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            if group_ids is None:
                scoped = list(result.groups)
            else:
                wanted = {str(value) for value in group_ids}
                scoped = [g for g in result.groups if g.id in wanted]
            changed = 0
            touched_low_res_paths: set[str] = set()
            for group in scoped:
                picks = bulk_selection_picks(group, operation, criteria)
                if picks == list(group.selected_for_removal):
                    continue
                group.selected_for_removal = picks
                if group.policy == ReviewPolicy.INDEPENDENT_CANDIDATES:
                    # Selecting a candidate is also a review decision for it.
                    group.reviewed_paths = list(
                        dict.fromkeys([*group.reviewed_paths, *picks])
                    )
                if group.kind == GroupKind.LOW_RESOLUTION:
                    touched_low_res_paths.update(member.path for member in group.members)
                changed += 1
            result.recompute_stats()
            refresh_selected_count_locked()
            if touched_low_res_paths:
                sync_low_resolution_keeps(result, touched_low_res_paths)
            schedule_persist()
            return jsonify({
                "ok": True,
                "operation": operation,
                "group_count": len(scoped),
                "changed_count": changed,
                "selected_count": state["selected_count"],
            })

    @app.post("/api/review-candidate/delete")
    def api_delete_review_candidate():
        """Move one independent review candidate to the system Trash (Finder Trash on macOS).

        Serves both the Non-Human and Faces review flows. A dry-run still issues a
        preview token for callers that want the two-step sheet. One-click review
        sends ``dry_run=false`` with no token; the server still preflights, then
        executes in the same request.
        """
        data = request.get_json(silent=True) or {}
        group_id = data.get("group_id")
        path = data.get("path")
        dry_run = bool(data.get("dry_run", True))
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "file actions are locked during active work"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            group = next(
                (
                    candidate
                    for candidate in (result.groups if result else [])
                    if candidate.id == group_id
                    and candidate.kind.value in IMMEDIATE_DELETE_KINDS
                ),
                None,
            )
            if group is None or path not in {member.path for member in group.members}:
                return jsonify({"error": "review candidate not found"}), 404
            if any(m.path == path and m.protected for m in group.members):
                return jsonify({"error": "This file is in a protected library"}), 400
            if path in state["deleted_files"]:
                return jsonify(group_payload(group))
            state["acting"] = True
            original_selected = list(group.selected_for_removal)
            original_reviewed = list(group.reviewed_paths)
            action_group = replace(
                group,
                selected_for_removal=[path],
                reviewed_paths=[path],
            )
            roots = list(result.roots)
            # An explicit one-click trash must not be vetoed by a prior Keep on
            # this same candidate (reviewed + unselected after an earlier trash).
            safety_groups = [
                action_group if candidate.id == group.id else candidate
                for candidate in result.groups
            ]

        try:
            preview = apply_actions(
                [action_group],
                action="trash",
                dry_run=True,
                roots=roots,
                safety_groups=safety_groups,
            )
            manifest = (state["scan_id"], "trash", group.kind.value, None, tuple(sorted(
                item.path for item in preview.items if item.ok
            )))
            if dry_run:
                payload = preview.to_dict()
                with lock:
                    payload["preview_token"] = issue_preview_token(manifest)
                payload["preview_expires_in"] = PREVIEW_TOKEN_TTL_SECONDS
                return jsonify(payload)
            preview_token = data.get("preview_token")
            if preview_token:
                with lock:
                    verdict = consume_preview_token(preview_token, manifest)
                    if verdict != "valid":
                        return jsonify(stale_preview_payload(verdict)), 409
            else:
                eligible = next(
                    (item for item in preview.items if item.path == path and item.ok),
                    None,
                )
                if eligible is None:
                    # A reviewed-but-kept decision in any independent review
                    # vetoes the trash; name the review so the user knows
                    # where to revise it instead of facing a bare refusal.
                    veto = next(
                        (
                            candidate
                            for candidate in (result.groups if result else [])
                            if candidate.id != group.id
                            and candidate.policy == ReviewPolicy.INDEPENDENT_CANDIDATES
                            and path in {member.path for member in candidate.members}
                            and path
                            in set(candidate.reviewed_paths)
                            - set(candidate.selected_for_removal)
                        ),
                        None,
                    )
                    if veto is not None:
                        label = {
                            "low_resolution": "Low-res",
                            "random_review": "Random",
                            "no_humans": "Non-Human",
                            "faces": "Faces",
                            "all_files": "Files",
                            "sets": "Sets",
                        }[veto.kind.value]
                        return jsonify(
                            {
                                "error": f"Kept in the {label} review — "
                                "revise that decision before trashing it here"
                            }
                        ), 400
                    error = next(
                        (item.error for item in preview.items if item.path == path),
                        "This file is not safely eligible for deletion",
                    )
                    return jsonify({"error": error}), 400
            action_result = apply_actions(
                [action_group], action="trash", dry_run=False, roots=roots,
                safety_groups=safety_groups,
            )
            item = next((item for item in action_result.items if item.path == path), None)
            if item is None or not item.ok:
                error = item.error if item else "delete did not complete"
                return jsonify({"error": error}), 400
            with lock:
                state["deleted_files"][path] = item.destination
                group.selected_for_removal = [
                    selected for selected in original_selected if selected != path
                ]
                group.reviewed_paths = list(dict.fromkeys([*original_reviewed, path]))
                # The swipe deck can re-anchor and trash the suggested keeper;
                # re-pick so keeper protection never guards a trashed file.
                if group.suggested_keep == path:
                    live = [
                        member
                        for member in group.members
                        if member.path not in state["deleted_files"]
                    ]
                    if live:
                        from ..grouping import pick_suggested_keep

                        group.suggested_keep = pick_suggested_keep(live)
                if result is not None:
                    result.recompute_stats()
                    refresh_selected_count_locked()
                payload = group_payload(group)
                persist_result()
                return jsonify(payload)
        finally:
            with lock:
                state["acting"] = False

    @app.post("/api/review-candidate/undo")
    def api_undo_review_candidate():
        """Restore one trashed Non-Human or Faces review candidate to its original path."""
        data = request.get_json(silent=True) or {}
        group_id = data.get("group_id")
        path = data.get("path")
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "file actions are locked during active work"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            group = next(
                (
                    candidate
                    for candidate in (result.groups if result else [])
                    if candidate.id == group_id
                    and candidate.kind.value in IMMEDIATE_DELETE_KINDS
                ),
                None,
            )
            destination = state["deleted_files"].get(path)
            if group is None or destination is None:
                return jsonify({"error": "there is no deleted file to undo"}), 404
            state["acting"] = True

        try:
            original = Path(path)
            recoverable = Path(destination)
            if original.exists() or original.is_symlink():
                return jsonify({"error": "the original path is already occupied"}), 409
            if not recoverable.is_file():
                return jsonify({
                   "error": "the file is no longer in the Trash; restore it manually from Finder (macOS Trash)"
               }), 404
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(recoverable), str(original))
            restored = original.stat()
            with lock:
                state["deleted_files"].pop(path, None)
                current = next(
                    (member for member in group.members if member.path == path),
                    None,
                )
                if current is not None:
                    refreshed = replace(
                        current,
                        size=restored.st_size,
                        mtime=restored.st_mtime,
                        device=restored.st_dev,
                        inode=restored.st_ino,
                        mtime_ns=restored.st_mtime_ns,
                    )
                    group.members = [
                        refreshed if member.path == path else member
                        for member in group.members
                    ]
                    if result is not None:
                        result.files = [
                            refreshed if file.path == path else file
                            for file in result.files
                        ]
                group.reviewed_paths = [
                    reviewed for reviewed in group.reviewed_paths if reviewed != path
                ]
                group.selected_for_removal = [
                    selected for selected in group.selected_for_removal if selected != path
                ]
                if result is not None:
                    result.recompute_stats()
                    refresh_selected_count_locked()
                persist_result()
                return jsonify(group_payload(group))
        except OSError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            with lock:
                state["acting"] = False

    # --- Crop review: black-border suggestions applied one file at a time ---

    def crop_group_locked(group_id):
        result: ScanResult | None = state["result"]
        return next(
            (
                group
                for group in (result.groups if result else [])
                if group.id == group_id and group.kind == GroupKind.CROP
            ),
            None,
        )

    def update_records_locked(path: str, fields: dict) -> None:
        """Apply ``fields`` to every in-memory record of ``path`` (files and groups)."""
        result: ScanResult | None = state["result"]
        if result is None:
            return
        records = [*result.files, *(member for group in result.groups for member in group.members)]
        for record in records:
            if record.path == path:
                for name, value in fields.items():
                    setattr(record, name, value)

    def record_identity(record) -> dict:
        return {
            name: getattr(record, name)
            for name in (
                "size", "mtime", "mtime_ns", "ctime_ns", "device", "inode",
                "width", "height", "sha256", "partial_hash", "phash", "dhash",
                "tile_phashes", "orientation_phashes",
            )
        }

    def drop_from_exact_groups_locked(path: str) -> None:
        """A cropped file is no longer byte-identical to its former copies."""
        result: ScanResult | None = state["result"]
        if result is None:
            return
        kept = []
        for group in result.groups:
            if group.kind == GroupKind.EXACT and any(m.path == path for m in group.members):
                remaining = [member for member in group.members if member.path != path]
                if len(remaining) < 2:
                    continue
                group.members = remaining
                group.selected_for_removal = [p for p in group.selected_for_removal if p != path]
                if group.suggested_keep == path:
                    from ..grouping import pick_suggested_keep

                    group.suggested_keep = pick_suggested_keep(remaining)
            kept.append(group)
        result.groups = kept

    def store_border_verdict(record, verdict: str) -> None:
        """Remember a crop verdict for this exact file content across scans."""
        cache = None
        try:
            cache = HashCache(app.config["DEDUPE_CACHE_PATH"])
            cache.store_all([
                replace(record, border_crop=verdict, border_signature=BORDER_SIGNATURE)
            ])
        except (OSError, sqlite3.Error):
            pass  # the decision still applies to this review; it just is not cached
        finally:
            if cache is not None:
                cache.close()

    def write_crop_receipt(items: list) -> str | None:
        if not items:
            return None
        receipt = ActionResult(dry_run=False, action="crop", items=items)
        from datetime import UTC, datetime

        receipt.completed_at = datetime.now(UTC).isoformat()
        _write_action_log(receipt)
        return receipt.log_path

    def begin_crop_work(data: dict):
        """Validate a crop request and take the action lock; returns (group, error)."""
        if state["scanning"] or state["acting"]:
            return None, (jsonify({"error": "file actions are locked during active work"}), 409)
        if data.get("scan_id") != state["scan_id"]:
            return None, (jsonify({"error": "stale scan session; refresh results"}), 409)
        group = crop_group_locked(data.get("group_id"))
        if group is None:
            return None, (jsonify({"error": "crop review not found"}), 404)
        return group, None

    def crop_member(member, roots: list[str], batch: TrashBatch):
        """Crop one file on disk; returns (outcome, box, error). Lock not held."""
        box = suggested_crop(member)
        if box is None:
            return None, None, "there is no crop suggestion for this file"
        if member.protected:
            return None, box, "This file is in a protected library"
        error, _stat = validate_record_with_stat(member, roots)
        if error:
            return None, box, error
        try:
            outcome = crop_file_in_place(member, box, send_to_trash=batch.send)
        except Exception as exc:
            return None, box, f"could not crop: {exc}"
        return outcome, box, None

    def record_crop_locked(group, member, box, outcome) -> None:
        cropped = group.match_details.setdefault("cropped", {})
        cropped[member.path] = {
            "box": box.encode(),
            "original": outcome.original_in_trash,
            "before": record_identity(member),
            "after": {"size": outcome.size, "mtime_ns": outcome.stat.st_mtime_ns},
        }
        group.reviewed_paths = list(dict.fromkeys([*group.reviewed_paths, member.path]))
        after = outcome.stat
        update_records_locked(member.path, {
            "size": int(after.st_size),
            "mtime": after.st_mtime,
            "mtime_ns": after.st_mtime_ns,
            "ctime_ns": after.st_ctime_ns,
            "device": after.st_dev,
            "inode": after.st_ino,
            "width": outcome.width,
            "height": outcome.height,
            "sha256": None,
            "partial_hash": None,
            "phash": None,
            "dhash": None,
            "tile_phashes": None,
            "orientation_phashes": None,
            "border_crop": "",
            "border_signature": BORDER_SIGNATURE,
        })
        drop_from_exact_groups_locked(member.path)

    def finish_crop_locked(group) -> dict:
        result: ScanResult | None = state["result"]
        if result is not None:
            result.recompute_stats()
            refresh_selected_count_locked()
        state["paths_version"] += 1
        state["groups_version"] += 1
        payload = group_payload(group)
        persist_result()
        return payload

    @app.post("/api/crop/apply")
    def api_crop_apply():
        """Crop one image in place: the original goes to the system Trash."""
        data = request.get_json(silent=True) or {}
        path = data.get("path")
        with lock:
            group, refusal = begin_crop_work(data)
            if refusal:
                return refusal
            member = next((m for m in group.members if m.path == path), None)
            if member is None:
                return jsonify({"error": "crop candidate not found"}), 404
            if path in (group.match_details.get("cropped") or {}):
                return jsonify(group_payload(group))
            state["acting"] = True
            roots = list(state["result"].roots)
        try:
            outcome, box, error = crop_member(member, roots, TrashBatch())
            if error:
                return jsonify({"error": error}), 400
            log_path = write_crop_receipt([
                ActionItem(
                    path=member.path,
                    ok=True,
                    action="crop",
                    destination=outcome.original_in_trash,
                    group_id=group.id,
                    size=member.size,
                )
            ])
            with lock:
                record_crop_locked(group, member, box, outcome)
                payload = finish_crop_locked(group)
                payload["log_path"] = log_path
                return jsonify(payload)
        finally:
            with lock:
                state["acting"] = False

    @app.post("/api/crop/apply-all")
    def api_crop_apply_all():
        """Crop every image in the review that has no decision yet."""
        data = request.get_json(silent=True) or {}
        with lock:
            group, refusal = begin_crop_work(data)
            if refusal:
                return refusal
            decided = set(group.reviewed_paths) | set(group.match_details.get("cropped") or {})
            pending = [m for m in group.members if m.path not in decided and not m.protected]
            state["acting"] = True
            roots = list(state["result"].roots)
        try:
            batch = TrashBatch()
            items, failures, done = [], [], []
            for member in pending:
                outcome, box, error = crop_member(member, roots, batch)
                if error:
                    failures.append({"path": member.path, "error": error})
                    continue
                done.append((member, box, outcome))
                items.append(ActionItem(
                    path=member.path,
                    ok=True,
                    action="crop",
                    destination=outcome.original_in_trash,
                    group_id=group.id,
                    size=member.size,
                ))
            log_path = write_crop_receipt(items)
            with lock:
                for member, box, outcome in done:
                    record_crop_locked(group, member, box, outcome)
                payload = finish_crop_locked(group)
                payload["cropped_count"] = len(done)
                payload["failures"] = failures
                payload["log_path"] = log_path
                return jsonify(payload)
        finally:
            with lock:
                state["acting"] = False

    @app.post("/api/crop/skip")
    def api_crop_skip():
        """Decline (or reconsider) one crop suggestion; remembered across scans."""
        data = request.get_json(silent=True) or {}
        path = data.get("path")
        skip = data.get("skip", True)
        if not isinstance(skip, bool):
            return jsonify({"error": "skip must be a boolean"}), 400
        with lock:
            group, refusal = begin_crop_work(data)
            if refusal:
                return refusal
            member = next((m for m in group.members if m.path == path), None)
            if member is None:
                return jsonify({"error": "crop candidate not found"}), 404
            if path in (group.match_details.get("cropped") or {}):
                return jsonify({"error": "this file is already cropped; undo the crop instead"}), 400
            box = suggested_crop(member)
            if skip:
                group.reviewed_paths = list(dict.fromkeys([*group.reviewed_paths, path]))
            else:
                group.reviewed_paths = [p for p in group.reviewed_paths if p != path]
            snapshot = replace(member)
            payload = finish_crop_locked(group)
        if box is not None:
            store_border_verdict(snapshot, BORDER_DISMISSED if skip else box.encode())
        return jsonify(payload)

    @app.post("/api/crop/undo")
    def api_crop_undo():
        """Put the original back: the cropped version goes to the Trash."""
        data = request.get_json(silent=True) or {}
        path = data.get("path")
        with lock:
            group, refusal = begin_crop_work(data)
            if refusal:
                return refusal
            entry = (group.match_details.get("cropped") or {}).get(path)
            if entry is None:
                return jsonify({"error": "there is no crop to undo"}), 404
            state["acting"] = True
        try:
            original = Path(path)
            backup = Path(entry["original"]) if entry.get("original") else None
            if backup is None or not backup.is_file():
                return jsonify({
                    "error": "the original is no longer in the Trash; restore it manually from Finder"
                }), 404
            try:
                current = os.lstat(original)
            except OSError:
                return jsonify({"error": "the cropped file is no longer at its path"}), 409
            after = entry.get("after") or {}
            if (
                int(current.st_size) != int(after.get("size", -1))
                or int(current.st_mtime_ns) != int(after.get("mtime_ns", -1))
            ):
                return jsonify({
                    "error": "the cropped file changed after cropping; restore the original "
                    "manually from the Trash"
                }), 409
            restored = restore_original_in_place(
                original, backup, send_to_trash=TrashBatch().send
            )
            with lock:
                (group.match_details.get("cropped") or {}).pop(path, None)
                group.reviewed_paths = [p for p in group.reviewed_paths if p != path]
                before = dict(entry.get("before") or {})
                before.update({
                    "size": int(restored.st_size),
                    "mtime": restored.st_mtime,
                    "mtime_ns": restored.st_mtime_ns,
                    "ctime_ns": restored.st_ctime_ns,
                    "device": restored.st_dev,
                    "inode": restored.st_ino,
                    "border_crop": entry["box"],
                    "border_signature": BORDER_SIGNATURE,
                })
                update_records_locked(path, before)
                return jsonify(finish_crop_locked(group))
        except OSError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            with lock:
                state["acting"] = False

    @app.post("/api/non-human/mark-remaining-human")
    def api_mark_remaining_human():
        """Persist all undeleted non-human candidates as manually reviewed humans."""
        data = request.get_json(silent=True) or {}
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "reviews are locked during active work"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            deleted = set(state["deleted_files"])
            records = list(
                {
                    member.path: member
                    for group in result.groups
                    if group.kind.value == "no_humans"
                    for member in group.members
                    if member.path not in deleted
                }.values()
            )
            if not records:
                return jsonify({"ok": True, "marked_count": 0})
            state["acting"] = True

        prior = [
            (
                record,
                record.human_detection_status,
                record.human_detector,
                record.human_detection_signature,
            )
            for record in records
        ]
        cache = None
        try:
            for record in records:
                record.human_detection_status = MANUALLY_CONFIRMED_HUMAN_STATUS
                record.human_detector = "manual_review"
                # Manual review outranks detector versions. File-identity checks in
                # HashCache still invalidate the decision when the file changes.
                record.human_detection_signature = None
            cache = HashCache(app.config["DEDUPE_CACHE_PATH"])
            cache.store_all(records)

            marked_paths = {record.path for record in records}
            with lock:
                for group in result.groups:
                    if group.kind.value != "no_humans":
                        continue
                    group.members = [
                        member for member in group.members if member.path not in marked_paths
                    ]
                    group.selected_for_removal = [
                        path for path in group.selected_for_removal if path not in marked_paths
                    ]
                    group.reviewed_paths = [
                        path for path in group.reviewed_paths if path not in marked_paths
                    ]
                result.groups = [group for group in result.groups if group.members]
                result.recompute_stats()
                refresh_selected_count_locked()
                state["groups_version"] = state.get("groups_version", 0) + 1
                state["paths_version"] += 1
            persist_result()
            return jsonify({"ok": True, "marked_count": len(records)})
        except (OSError, sqlite3.Error) as exc:
            for record, status, detector, signature in prior:
                record.human_detection_status = status
                record.human_detector = detector
                record.human_detection_signature = signature
            return jsonify({"error": f"could not save manual reviews: {exc}"}), 400
        finally:
            if cache is not None:
                cache.close()
            with lock:
                state["acting"] = False

    @app.post("/api/similar/mark-distinct")
    def api_mark_similar_distinct():
        """Persist Similar files as pairwise distinct and remove them from review.

        With no ``path`` the whole group is marked pairwise distinct and
        dissolved, as before. With ``path`` only that member is decided: the
        ``anchor``/suggested-keeper pair is recorded as distinct (future scans
        never regroup those two files) and the member leaves the group; the
        group dissolves once fewer than two members remain. Dissolution
        records every pair among the review's participants, so members the
        swipe UI only ever compared against the anchor can never regroup
        with each other either.
        """
        data = request.get_json(silent=True) or {}
        group_id = data.get("group_id")
        member_path = data.get("path")
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "reviews are locked during active work"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            group = next(
                (
                    candidate
                    for candidate in (result.groups if result else [])
                    if candidate.id == group_id and candidate.kind.value == "similar"
                ),
                None,
            )
            if group is None:
                return jsonify({"error": "similar group not found"}), 404
            member = next(
                (m for m in group.members if m.path == member_path), None
            )
            if member_path is not None and member is None:
                return jsonify({"error": "member not found in group"}), 404
            anchor_path = data.get("anchor") or group.suggested_keep
            anchor = next(
                (m for m in group.members if m.path == anchor_path), None
            )
            if member is not None and anchor is None:
                return jsonify({"error": "anchor is not a member of this group"}), 400
            if member is not None and anchor.path == member.path:
                return jsonify({"error": "a file cannot be distinct from itself"}), 400
            records = [anchor, member] if member is not None else list(group.members)
            # Whole-group reviews can be undone (the Similar board's "Not
            # duplicates" → Undo); remember the group and where it sat.
            group_index = result.groups.index(group)
            review_paths = {m.path for m in group.members} | set(group.distinct_participants)
            state["acting"] = True

        cache = None
        try:
            cache = HashCache(app.config["DEDUPE_CACHE_PATH"])
            pairs_before = (
                cache.recorded_pairs_among(review_paths) if member is None else set()
            )
            pair_count = cache.mark_distinct(records)
            backfill: list = []
            with lock:
                if member is not None:
                    group.members = [
                        m for m in group.members if m.path != member.path
                    ]
                    group.selected_for_removal = [
                        path for path in group.selected_for_removal
                        if path != member.path
                    ]
                    group.reviewed_paths = [
                        path for path in group.reviewed_paths
                        if path != member.path
                    ]
                    group.distinct_participants = sorted(
                        {*group.distinct_participants, anchor.path, member.path}
                    )
                if member is None or len(group.members) < 2:
                    result.groups = [
                        candidate
                        for candidate in result.groups
                        if candidate.id != group_id
                    ]
                result.recompute_stats()
                refresh_selected_count_locked()
                state["groups_version"] = state.get("groups_version", 0) + 1
                state["paths_version"] += 1
                dissolved = all(
                    candidate.id != group_id for candidate in result.groups
                )
                payload = None if dissolved else group_payload(group)
                if dissolved and (member is not None or group.distinct_participants):
                    # The group is finished: suppress every pair among everyone
                    # who took part in the review — swipe participants plus the
                    # members just dismissed — including member-vs-member pairs
                    # the swipe UI never showed side by side.
                    involved = set(group.distinct_participants) | {
                        m.path for m in group.members
                    }
                    if len(involved) > 2:
                        by_path = {f.path: f for f in (result.files if result else [])}
                        backfill = [
                            by_path[path]
                            for path in sorted(involved)
                            if path in by_path
                        ]
            if len(backfill) > 2:
                pair_count = cache.mark_distinct(backfill)
            if member is None and dissolved:
                new_pairs = cache.recorded_pairs_among(review_paths) - pairs_before
                with lock:
                    dissolved_groups = state["dissolved_similar"]
                    dissolved_groups[group_id] = {
                        "group": group,
                        "index": group_index,
                        "new_pairs": sorted(new_pairs),
                        "scan_id": state["scan_id"],
                    }
                    # In-memory undo history only; keep it bounded.
                    while len(dissolved_groups) > DISSOLVED_SIMILAR_UNDO_LIMIT:
                        dissolved_groups.pop(next(iter(dissolved_groups)))
            persist_result()
            return jsonify({
                "ok": True,
                "pair_count": pair_count,
                "dissolved": dissolved,
                "group": payload,
            })
        except (OSError, sqlite3.Error) as exc:
            return jsonify({"error": f"could not save distinct review: {exc}"}), 400
        finally:
            if cache is not None:
                cache.close()
            with lock:
                state["acting"] = False

    @app.post("/api/similar/unmark-distinct")
    def api_unmark_similar_distinct():
        """Undo one pair-level distinct decision and return the member to review.

        Deletes the recorded pair and re-inserts the member into its group when
        the group is still under review. When the group already dissolved, the
        pair row is still dropped — the files can regroup on the next scan.
        """
        data = request.get_json(silent=True) or {}
        group_id = data.get("group_id")
        member_path = data.get("path")
        if not member_path:
            return jsonify({"error": "path is required"}), 400
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "reviews are locked during active work"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            group = next(
                (
                    candidate
                    for candidate in (result.groups if result else [])
                    if candidate.id == group_id and candidate.kind.value == "similar"
                ),
                None,
            )
            anchor_path = (
                data.get("anchor") or (group.suggested_keep if group else None)
            )
            if anchor_path is None:
                return jsonify({"error": "anchor is required for a dissolved group"}), 400
            record = next(
                (
                    file
                    for file in (result.files if result else [])
                    if file.path == member_path
                ),
                None,
            )
            if group is not None and record is None:
                return jsonify({"error": "file is not part of this scan"}), 404
            if group is not None and member_path in {
                member.path for member in group.members
            }:
                return jsonify(group_payload(group))
            state["acting"] = True

        cache = None
        try:
            cache = HashCache(app.config["DEDUPE_CACHE_PATH"])
            removed = cache.unmark_distinct_pair(
                anchor_path,
                member_path,
                result.files if result else None,
            )
            with lock:
                if group is not None:
                    group.members = [*group.members, record]
                    group.distinct_participants = [
                        path
                        for path in group.distinct_participants
                        if path != member_path
                    ]
                    result.recompute_stats()
                    refresh_selected_count_locked()
                    state["groups_version"] = state.get("groups_version", 0) + 1
                    state["paths_version"] += 1
                payload = group_payload(group) if group is not None else None
            if group is not None:
                persist_result()
            return jsonify({
                "ok": True,
                "removed_pairs": removed,
                "group": payload,
            })
        except (OSError, sqlite3.Error) as exc:
            return jsonify({"error": f"could not undo distinct review: {exc}"}), 400
        finally:
            if cache is not None:
                cache.close()
            with lock:
                state["acting"] = False

    @app.post("/api/similar/restore-group")
    def api_restore_similar_group():
        """Undo a whole-group distinct review: the group returns to review.

        Drops only the distinct pairs that review created (pairs decided
        earlier stay recorded) and re-inserts the group where it sat. Members
        that left the scan since (moved to Trash, quarantined) stay out; the
        undo is refused when fewer than two remain.
        """
        data = request.get_json(silent=True) or {}
        group_id = data.get("group_id")
        with lock:
            if state["scanning"] or state["acting"]:
                return jsonify({"error": "reviews are locked during active work"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            entry = state["dissolved_similar"].get(group_id)
            if result is None or entry is None or entry["scan_id"] != state["scan_id"]:
                return jsonify({"error": "this review can no longer be undone"}), 404
            if any(candidate.id == group_id for candidate in result.groups):
                state["dissolved_similar"].pop(group_id, None)
                return jsonify({"error": "this group is already under review"}), 409
            group = entry["group"]
            current = {file.path: file for file in result.files}
            deleted = state["deleted_files"]
            members = [
                current[member.path]
                for member in group.members
                if member.path in current and member.path not in deleted
            ]
            if len(members) < 2:
                state["dissolved_similar"].pop(group_id, None)
                return jsonify(
                    {"error": "too few of this group's files are still in the scan to restore it"}
                ), 409
            state["acting"] = True

        cache = None
        try:
            cache = HashCache(app.config["DEDUPE_CACHE_PATH"])
            removed = 0
            for path_a, path_b in entry["new_pairs"]:
                removed += cache.unmark_distinct_pair(path_a, path_b, result.files)
            with lock:
                member_paths = {member.path for member in members}
                group.members = members
                group.selected_for_removal = [
                    path for path in group.selected_for_removal if path in member_paths
                ]
                if group.suggested_keep not in member_paths:
                    group.suggested_keep = members[0].path
                if len(group.selected_for_removal) >= len(members):
                    # Keep-one groups always retain their keeper.
                    group.selected_for_removal = [
                        path for path in group.selected_for_removal
                        if path != group.suggested_keep
                    ]
                result.groups.insert(min(entry["index"], len(result.groups)), group)
                state["dissolved_similar"].pop(group_id, None)
                result.recompute_stats()
                refresh_selected_count_locked()
                state["groups_version"] = state.get("groups_version", 0) + 1
                state["paths_version"] += 1
                payload = group_payload(group)
            persist_result()
            return jsonify({"ok": True, "removed_pairs": removed, "group": payload})
        except (OSError, sqlite3.Error) as exc:
            return jsonify({"error": f"could not undo distinct review: {exc}"}), 400
        finally:
            if cache is not None:
                cache.close()
            with lock:
                state["acting"] = False

    @app.post("/api/action")
    def api_action():
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "trash").lower()
        dry_run = bool(data.get("dry_run", True))
        quarantine_dir = data.get("quarantine_dir")
        destination = normalized_destination(action, quarantine_dir)

        if action not in ("trash", "quarantine", "isolate"):
            return jsonify({"error": "action must be trash, quarantine, or isolate"}), 400
        if action == "quarantine" and not quarantine_dir and not dry_run:
            return jsonify({"error": "quarantine_dir required"}), 400

        with lock:
            if state["scanning"]:
                return jsonify({"error": "wait for the scan to finish or cancel it"}), 409
            if state["acting"]:
                return jsonify({"error": "another file action is already running"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            result: ScanResult | None = state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            state["acting"] = True
            groups = list(result.groups)
            roots = list(result.roots)

        try:
            kinds_raw = data.get("kinds") or data.get("isolate_kinds") or "all"
            if kinds_raw == "duplicates":
                kinds = {"exact", "similar"}
            elif kinds_raw == "review_suggestions":
                kinds = {"low_resolution", "random_review"}
            else:
                kinds = None if kinds_raw in ("all", "") else {kinds_raw}
            scoped_groups = (
                groups
                if kinds is None
                else [group for group in groups if group.kind.value in kinds]
            )
            selected_paths = effective_selected_paths(
                scoped_groups,
                protection_groups=groups,
            )
            selected = set(selected_paths)
            counted: set[str] = set()

            def count_kind(kind: str) -> int:
                paths = (
                    set(
                        effective_selected_paths(
                            [group for group in scoped_groups if group.kind.value == kind],
                            protection_groups=groups,
                        )
                    )
                    & selected
                ) - counted
                counted.update(paths)
                return len(paths)

            selection_counts = {
                "exact": count_kind("exact"),
                "similar": count_kind("similar"),
                "no_humans": count_kind("no_humans"),
                "unique_total": len(selected_paths),
            }
            low_resolution_count = count_kind("low_resolution")
            random_review_count = count_kind("random_review")
            faces_count = count_kind("faces")
            # Keep the established response shape for duplicate-only callers;
            # expose new categories whenever they contribute a selection.
            if low_resolution_count:
                selection_counts["low_resolution"] = low_resolution_count
            if random_review_count:
                selection_counts["random_review"] = random_review_count
            if faces_count:
                selection_counts["faces"] = faces_count
            if action == "isolate":
                mode = (data.get("isolate_mode") or "copy").lower()
                action_result = isolate_groups(
                    groups,
                    data.get("review_dir"),
                    mode=mode,
                    kinds=kinds,
                    dry_run=dry_run,
                    roots=roots,
                )
            else:
                review_kinds = {GroupKind.LOW_RESOLUTION, GroupKind.RANDOM_REVIEW}
                review_groups = [group for group in groups if group.kind in review_kinds]
                review_paths = (
                    set(
                        effective_selected_paths(
                            review_groups,
                            protection_groups=groups,
                        )
                    )
                    & selected
                    if action == "trash"
                    else set()
                )
                trash_paths = selected - review_paths
                special_quarantine = (
                    review_quarantine_dir(roots, review_paths) if review_paths else None
                )

                def groups_selecting(paths: set[str]):
                    return [
                        replace(
                            group,
                            selected_for_removal=[
                                path for path in group.selected_for_removal if path in paths
                            ],
                        )
                        for group in scoped_groups
                    ]

                def run_action_parts(part_dry_run: bool):
                    if action != "trash":
                        return [
                            apply_actions(
                                groups,
                                action=action,
                                quarantine_dir=destination,
                                dry_run=part_dry_run,
                                roots=roots,
                                kinds=kinds,
                                safety_groups=groups,
                            )
                        ]
                    partitions = []
                    if review_paths:
                        partitions.append(
                            (review_paths, "quarantine", special_quarantine)
                        )
                    if trash_paths:
                        partitions.append((trash_paths, "trash", None))
                    return [
                        apply_actions(
                            groups_selecting(paths),
                            action=part_action,
                            quarantine_dir=part_destination,
                            dry_run=part_dry_run,
                            roots=roots,
                            safety_groups=groups,
                            allow_cross_device=part_action == "quarantine",
                        )
                        for paths, part_action, part_destination in partitions
                    ]

                preview_results = run_action_parts(True)
                preview_result = ActionResult(dry_run=True, action=action)
                preview_result.items = [
                    item for partial in preview_results for item in partial.items
                ]
                manifest_destination = (
                    destination,
                    str(special_quarantine) if special_quarantine else None,
                    tuple(sorted(review_paths)),
                )
                manifest = preview_manifest(
                    action, kinds_raw, manifest_destination, preview_result
                )
                if dry_run:
                    action_result = preview_result
                    with lock:
                        preview_token = issue_preview_token(manifest)
                else:
                    with lock:
                        verdict = consume_preview_token(data.get("preview_token"), manifest)
                        if verdict != "valid":
                            return jsonify(stale_preview_payload(verdict)), 409
                    executed_results = run_action_parts(False)
                    action_result = ActionResult(dry_run=False, action=action)
                    action_result.items = [
                        item for partial in executed_results for item in partial.items
                    ]
                    action_result.log_path = next(
                        (partial.log_path for partial in executed_results if partial.log_path),
                        None,
                    )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        else:
            # If trash/quarantine executed, drop removed members from in-memory result.
            if not dry_run and action in ("trash", "quarantine"):
                removed = {item.path for item in action_result.items if item.ok}
                with lock:
                    result = state["result"]
                    if result is not None:
                        new_groups = []
                        for group in result.groups:
                            remaining = [
                                member
                                for member in group.members
                                if member.path not in removed
                            ]
                            minimum = (
                                1
                                if group.policy == ReviewPolicy.INDEPENDENT_CANDIDATES
                                else 2
                            )
                            if len(remaining) >= minimum:
                                group.members = remaining
                                group.selected_for_removal = [
                                    path
                                    for path in group.selected_for_removal
                                    if path not in removed
                                ]
                                group.reviewed_paths = [
                                    path
                                    for path in group.reviewed_paths
                                    if path not in removed
                                ]
                                if group.suggested_keep in removed and remaining:
                                    from ..grouping import pick_suggested_keep

                                    group.suggested_keep = pick_suggested_keep(remaining)
                                new_groups.append(group)
                            # Groups below their minimum size dissolve.
                        result.groups = new_groups
                        result.files = [
                            file for file in result.files if file.path not in removed
                        ]
                        result.recompute_stats()
                        refresh_selected_count_locked()
                        state["paths_version"] += 1
                if removed:
                    persist_result()

            with lock:
                if action_result.review_root:
                    state["allowed_reveal_paths"].add(
                        str(Path(action_result.review_root).resolve(strict=False))
                    )

            payload = action_result.to_dict()
            if action in ("trash", "quarantine"):
                payload["selection_counts"] = selection_counts
                if action == "trash" and special_quarantine:
                    payload["review_quarantine_dir"] = str(special_quarantine)
                    payload["review_quarantine_count"] = sum(
                        1
                        for item in action_result.items
                        if item.action == "quarantine" and item.ok
                    )
                    if not dry_run:
                        payload["log_paths"] = [
                            partial.log_path
                            for partial in executed_results
                            if partial.log_path
                        ]
                if dry_run:
                    payload["preview_token"] = preview_token
                    payload["preview_expires_in"] = PREVIEW_TOKEN_TTL_SECONDS
            return jsonify(payload)
        finally:
            with lock:
                state["acting"] = False

    @app.post("/api/action/undo")
    def api_action_undo():
        """Restore an executed trash/quarantine action from its receipt(s).

        Files return to their original paths on disk; the current review is
        not re-populated — restored files resurface on the next scan. The
        dry-run issues a preview token bound to the exact restorable set, and
        the execute preflights every receipt again before moving anything, so
        a blocked item refuses the whole restore across all receipts.
        """
        from ..receipts import ReceiptError

        data = request.get_json(silent=True) or {}
        receipts = data.get("receipts") or []
        if isinstance(receipts, str):
            receipts = [receipts]
        receipts = [str(reference) for reference in receipts]
        dry_run = bool(data.get("dry_run", True))
        if not receipts:
            return jsonify({"error": "receipts required"}), 400

        with lock:
            if state["scanning"]:
                return jsonify({"error": "wait for the scan to finish or cancel it"}), 409
            if state["acting"]:
                return jsonify({"error": "another file action is already running"}), 409
            if data.get("scan_id") != state["scan_id"]:
                return jsonify({"error": "stale scan session; refresh results"}), 409
            state["acting"] = True

        try:
            previews = [
                undo_action(reference, dry_run=True) for reference in receipts
            ]
            restorable = tuple(sorted(
                item.destination for preview in previews for item in preview.items
                if item.ok and item.destination
            ))
            manifest = (state["scan_id"], "undo", tuple(sorted(receipts)), None, restorable)
            if dry_run:
                result = ActionResult(dry_run=True, action="undo")
                result.items = [item for preview in previews for item in preview.items]
                payload = result.to_dict()
                with lock:
                    payload["preview_token"] = issue_preview_token(manifest)
                payload["preview_expires_in"] = PREVIEW_TOKEN_TTL_SECONDS
                return jsonify(payload)

            with lock:
                verdict = consume_preview_token(data.get("preview_token"), manifest)
                if verdict != "valid":
                    return jsonify(stale_preview_payload(verdict)), 409
            # Cross-receipt all-or-nothing: the manifest only matches the
            # current restorable set, but the files could have moved since the
            # dry run — re-verify before moving anything.
            blocked = [
                item
                for preview in previews
                for item in preview.items
                if not item.ok
            ]
            if blocked:
                first = blocked[0]
                return jsonify(
                    {
                        "error": f"cannot undo: {first.error} ({first.path}) — "
                        "nothing was restored",
                        "preview_stale": False,
                    }
                ), 400
            executed = [
                undo_action(reference, dry_run=False) for reference in receipts
            ]
            result = ActionResult(dry_run=False, action="undo")
            result.items = [item for run in executed for item in run.items]
            payload = result.to_dict()
            payload["log_paths"] = [run.log_path for run in executed if run.log_path]
            return jsonify(payload)
        except ReceiptError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            with lock:
                state["acting"] = False

    @app.post("/api/pick-folder")
    def api_pick_folder():
        """Open a native folder/file picker and return local filesystem paths."""
        data = request.get_json(silent=True) or {}
        kind = str(data.get("kind") or "folder").lower()
        if kind not in {"folder", "files"}:
            return jsonify({"error": "kind must be folder or files"}), 400
        payload, status = pick_native_paths(kind)
        return jsonify(payload), status

    @app.get("/api/thumbnail")
    def api_thumbnail():
        path = request.args.get("path", "")
        if not path:
            return jsonify({"error": "path required"}), 400
        p = Path(path)
        if not p.is_file():
            return jsonify({"error": "not found"}), 404

        # Only serve files that were part of the last scan (path traversal safety)
        if not is_scanned_file(p, path):
            return jsonify({"error": "not in scan"}), 403

        # Variants: "thumb" (grid cards), "preview" (lightbox, ≤2560px, cached),
        # "full" (original-resolution transcode for formats browsers cannot
        # render; browser-safe originals are served untouched as before).
        variant_arg = request.args.get("variant")
        if variant_arg in ("thumb", "preview", "full"):
            variant = variant_arg
        else:
            variant = "full" if request.args.get("full") == "1" else "thumb"
        if variant == "full" and not is_video(p) and is_browser_safe_image(p):
            return send_file(p, mimetype=media_mimetype(p), conditional=True)
        cached = cached_thumbnail(p, variant=variant)
        if cached is None:
            # Videos have no still to fall back to; images can serve the original.
            if is_video(p):
                return jsonify({"error": "no preview"}), 404
            return send_file(p, mimetype=media_mimetype(p))

        thumb_path, key = cached
        response = send_file(
            thumb_path,
            mimetype="image/jpeg",
            conditional=True,
            etag=key,
            last_modified=p.stat().st_mtime,
        )
        # The key already includes source mtime/size, so the body can never change.
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        return response

    @app.get("/api/media-details")
    def api_media_details():
        path = request.args.get("path", "")
        media_path = Path(path)
        if not path or not is_scanned_file(media_path, path):
            return jsonify({"error": "not in scan"}), 403
        try:
            return jsonify(comparison_details(media_path))
        except Exception:
            return jsonify({"error": "Metadata unavailable"}), 422

    @app.get("/api/media")
    def api_media():
        """Stream scanned media with byte ranges for native video seeking."""
        path = request.args.get("path", "")
        if not path:
            return jsonify({"error": "path required"}), 400
        media_path = Path(path)
        if not media_path.is_file():
            return jsonify({"error": "not found"}), 404
        if not is_scanned_file(media_path, path):
            return jsonify({"error": "not in scan"}), 403

        return send_file(
            media_path,
            mimetype=media_mimetype(media_path),
            conditional=True,
        )

    @app.get("/api/reveal")
    def api_reveal():
        """Return path so client can show it; on macOS we can also open Finder."""
        path = request.args.get("path", "")
        open_finder = request.args.get("open") == "1"
        if not path:
            return jsonify({"error": "path required"}), 400
        p = Path(path)
        resolved = str(p.expanduser().resolve(strict=False))
        with lock:
            result: ScanResult | None = state["result"]
            allowed = {file.path for file in result.files} if result else set()
            allowed.update(state["allowed_reveal_paths"])
        if resolved not in allowed and path not in allowed:
            return jsonify({"error": "path is not part of this Dedupe session"}), 403
        if open_finder and p.exists():
            import subprocess

            subprocess.Popen(["open", "-R", str(p)])
        return jsonify({"path": path, "exists": p.exists()})

    @app.post("/api/shutdown")
    def api_shutdown():
        """Stop the server shortly after the last browser tab closes.

        The page sends this on pagehide, which also fires on reloads and
        navigation, so wait a grace period; any request that arrives in the
        meantime (e.g. the reloaded page) cancels the shutdown.
        """

        def stop() -> None:
            # Flush any debounced review-session write before the process exits.
            _flush_persist()
            server = app.extensions.get("dedupe_server")
            if server is not None:
                # Unblocks serve_forever() in run_app, so the process exits
                # cleanly and the launcher can close its Terminal window.
                server.shutdown()
            else:
                # App is being served some other way; SIGINT is the best we
                # can do (equivalent to Ctrl+C).
                os.kill(os.getpid(), signal.SIGINT)

        with lock:
            timer = state.get("shutdown_timer")
            if timer is not None:
                timer.cancel()
            timer = threading.Timer(SHUTDOWN_GRACE_SECONDS, stop)
            timer.daemon = True
            state["shutdown_timer"] = timer
            timer.start()
        return jsonify({"ok": True})

    return app


def run_app(
    app: Flask,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    """Start the UI server on ``host``/``port``, rerouting if the port is busy.

    Binds before announcing anything, so the URL line is only ever printed
    for a server that actually started. When the designated port is taken
    the server moves to the next free port instead of failing — unless the
    blocker is itself a Dedupe UI, in which case starting a second UI would
    strand the blocker's review session on a different URL, so the start is
    refused with a deliberate message. Returns an exit code: 0 after a
    clean shutdown, 2 if the start was refused. KeyboardInterrupt (Ctrl+C)
    propagates to the caller.
    """
    from werkzeug.serving import make_server

    # Bind candidate ports ourselves (same SO_REUSEADDR semantics as
    # werkzeug) and hand the pre-bound socket fd to make_server, so the
    # probe and the server share a single bind — no race between a probe
    # and a second listener, and no werkzeug SystemExit on a busy port.
    # (make_server rather than app.run so /api/shutdown can stop the loop
    # via server.shutdown() when the tab closes.)
    bound: socket.socket | None = None
    refused_reason: str | None = None
    actual_port: int | None = None
    try:
        for candidate in _candidate_ports(port):
            bound = _bind_socket(host, candidate)
            if bound is not None:
                actual_port = bound.getsockname()[1]
                break
        if bound is None:
            refused_reason = f"no port available for the UI on {host}"
        elif actual_port != port and _is_dedupe_ui_running(host, port):
            refused_reason = (
                f"port {port} is in use by another Dedupe UI — "
                "two UIs cannot run at once; reuse it in the browser or "
                "stop the other process first"
            )
        if refused_reason is not None:
            print(f"error: {refused_reason}", file=sys.stderr)
            return 2
        assert actual_port is not None
        if actual_port != port:
            print(
                f"note: port {port} is in use by another program — "
                f"serving on {actual_port} instead",
                file=sys.stderr,
            )
        server = make_server(host, actual_port, app, threaded=True, fd=bound.fileno())
    except OSError as exc:
        print(
            f"error: could not start the UI server ({exc.strerror or exc})",
            file=sys.stderr,
        )
        return 2
    finally:
        if bound is not None:
            bound.close()
    app.extensions["dedupe_server"] = server
    url = f"http://{host}:{actual_port}/"
    print(f"Dedupe UI: {url}", flush=True)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print("Press CTRL+C to quit (closing the browser tab also stops the server)", flush=True)
    try:
        server.serve_forever()
    finally:
        app.extensions.pop("dedupe_server", None)
        server.server_close()
    return 0


def _candidate_ports(port: int) -> list[int | None]:
    """Designated port first, then walking up until one binds; None = OS-chosen.

    The walk-up stops at the top of the range: beyond it the OS is asked for
    a free port, which cannot collide. A designated port at or above the top
    falls straight to the OS-chosen candidate.
    """
    if port >= 65535:
        return [65535, None]
    return list(range(port, 65535)) + [None]


def _bind_socket(host: str, port: int | None) -> socket.socket | None:
    """Return a listening socket bound to host:port, or None if it is taken.

    ``None`` port asks the OS for a free port (binds as port 0). SO_REUSEADDR
    matches werkzeug's serving socket so a wildcard-bound listener coexists
    with this loopback bind on macOS (and a TIME_WAIT socket does not block it).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port if port is not None else 0))
        sock.listen()
    except OSError:
        sock.close()
        return None
    return sock


def _is_dedupe_ui_running(host: str, port: int) -> bool:
    """Say whether the process listening on host:port is a Dedupe UI.

    The UI reports its API version on /api/status; a foreign blocker (e.g.
    ``python -m http.server``) does not. Network errors count as "not a
    Dedupe UI" so a degraded blocker never blocks a reroute.
    """
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=2) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return False
    return isinstance(body, dict) and "web_api_version" in body
