"""Flask web UI for browsing and acting on duplicate groups."""

from __future__ import annotations

import hmac
import os
import secrets
import shutil
import signal
import sqlite3
import threading
import time
import webbrowser
from dataclasses import replace
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

from ..actions import ActionResult, apply_actions, format_bytes, isolate_groups, undo_action
from ..cache import HashCache
from ..engine import run_scan, run_scans_parallel
from ..grouping import (
    apply_smart_select,
    apply_smart_select_all,
    ensure_all_files_groups,
)
from ..human_detection import DEFAULT_PHOTON_MODEL, HUMAN_BACKENDS
from ..human_policy import MANUALLY_CONFIRMED_HUMAN_STATUS
from ..keep_decisions import update_keep_decisions
from ..models import (
    GroupKind,
    ReviewPolicy,
    ScanProgress,
    ScanResult,
    SmartRule,
    effective_selected_paths,
)
from ..review_session import (
    ReviewSessionLoad,
    discard_review_session,
    load_review_session,
    save_review_session,
)
from ..similar_video import aligned_position_indexes
from .media import cached_thumbnail, is_browser_safe_image, is_video, media_mimetype
from .native_picker import pick_native_paths

# Increment when adding/changing browser-facing API routes. The macOS launcher uses
# this to avoid pairing static files from the working tree with a stale Flask process.
WEB_API_VERSION = 21
PREVIEW_TOKEN_TTL_SECONDS = 600

#: Independent review flows whose cards support direct per-file Trash + undo.
INDEPENDENT_DELETE_KINDS = {"no_humans", "faces", "all_files"}
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


def similarity_percent(member, keeper) -> float | None:
    """Return perceptual-fingerprint bit agreement with a group's keeper."""
    comparisons: list[tuple[int, int]] = []
    if member.media_type.value == "video":
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
        left_tiles = _stored_hashes(member.tile_phashes, ("t2",))
        right_tiles = _stored_hashes(keeper.tile_phashes, ("t2",))
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
    if group.kind == GroupKind.ALL_FILES:
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
    }


def create_app(
    initial_result: ScanResult | None = None,
    review_session_path: str | Path | None = None,
) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    csrf_token = secrets.token_urlsafe(32)
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    app.config["DEDUPE_CSRF_TOKEN"] = csrf_token
    app.config["DEDUPE_CACHE_PATH"] = None
    app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost", "[::1]"]
    capabilities = detect_capabilities()
    lock = threading.RLock()
    loaded = ReviewSessionLoad(path=Path(review_session_path) if review_session_path else None)
    if initial_result is None:
        loaded = load_review_session(review_session_path)
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
        if group.kind == GroupKind.SIMILAR:
            keeper = next(
                (member for member in group.members if member.path == group.suggested_keep),
                None,
            )
            if keeper is not None:
                for member, member_payload in zip(
                    group.members, payload["members"], strict=True
                ):
                    member_payload["similarity_percent"] = similarity_percent(member, keeper)
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
        selected = effective_selected_paths([group])
        sizes = {member.path: member.size for member in group.members}
        result.reclaimable_bytes += sum(sizes.get(path, 0) for path in selected)
        state["selected_count"] += len(selected)

    def refresh_selected_count_locked() -> None:
        """Full refresh after post-scan selection/membership mutations."""
        result: ScanResult | None = state["result"]
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
    elif loaded.result is not None:
        with lock:
            ensure_all_files_groups(loaded.result)
            state["result"] = loaded.result
            loaded.result.recompute_stats()
            state["deleted_files"] = dict(loaded.deleted_files)
            state["scan_id"] = secrets.token_hex(12)
            state["progress"] = ScanProgress(
                phase="done",
                done=True,
                files_found=len(loaded.result.files),
                groups_found=len(loaded.result.groups),
                message="Resumed saved review",
                elapsed_seconds=loaded.result.diagnostics.total_duration_seconds,
            )
            refresh_selected_count_locked()

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=csrf_token)

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
            report = load_review_session(review_session_path)
            state["review_session"] = report
            if report.result is None:
                return jsonify(report.metadata()), 404
            ensure_all_files_groups(report.result)
            state["result"] = report.result
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

    @app.post("/api/scan")
    def api_scan():
        data = request.get_json(silent=True) or {}
        paths = data.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        paths = [p for p in paths if p and str(p).strip()]
        if not paths:
            return jsonify({"error": "paths required"}), 400

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
            state["result"] = ScanResult(roots=resolved_roots, files=[], groups=[])
            state["preview_tokens"] = {}
            state["groups_version"] = state.get("groups_version", 0) + 1
            state["paths_version"] += 1
            state["selected_count"] = 0
            state["streamed_group_index"] = {}
            # Default to parallel per-folder streams when more than one folder is
            # given; each folder becomes its own concurrent scan (no cross-folder dedup).
            parallel_default = len(resolved_roots) > 1
            parallel_streams = bool(data.get("parallel_streams", parallel_default))
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
                    "exact": bool(data.get("exact", True)),
                    "similar": bool(data.get("similar", True)),
                    "find_no_humans": bool(data.get("find_no_humans", False)),
                    "count_faces": bool(data.get("count_faces", False)),
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
                            f"Done — {result.exact_groups} exact, "
                            f"{result.similar_groups} similar, "
                            f"{result.low_resolution_files} low-resolution, "
                            f"{result.random_review_files} random review, "
                            f"{result.no_human_files} non-human"
                        ),
                        elapsed_seconds=result.diagnostics.total_duration_seconds,
                        eta_seconds=0.0,
                    )
                    persist_result()
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
                    member_paths = {m.path for m in g.members}
                    decision_path = data.get("decision_path")
                    if decision_path is not None:
                        decision_path = str(decision_path)
                        if (
                            g.policy != ReviewPolicy.INDEPENDENT_CANDIDATES
                            or decision_path not in member_paths
                        ):
                            return jsonify({"error": "candidate decision is not in this group"}), 400
                        remove = data.get("decision_remove")
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
                    picks = [p for p in selected if p in member_paths]
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
                    and candidate.kind.value in INDEPENDENT_DELETE_KINDS
                ),
                None,
            )
            if group is None or path not in {member.path for member in group.members}:
                return jsonify({"error": "review candidate not found"}), 404
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
                    and candidate.kind.value in INDEPENDENT_DELETE_KINDS
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
        """Persist one Similar group as pairwise distinct and remove it from review."""
        data = request.get_json(silent=True) or {}
        group_id = data.get("group_id")
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
            records = list(group.members)
            state["acting"] = True

        cache = None
        try:
            cache = HashCache(app.config["DEDUPE_CACHE_PATH"])
            pair_count = cache.mark_distinct(records)
            with lock:
                result.groups = [candidate for candidate in result.groups if candidate.id != group_id]
                result.recompute_stats()
                refresh_selected_count_locked()
                state["groups_version"] = state.get("groups_version", 0) + 1
                state["paths_version"] += 1
            persist_result()
            return jsonify({"ok": True, "pair_count": pair_count})
        except (OSError, sqlite3.Error) as exc:
            return jsonify({"error": f"could not save distinct review: {exc}"}), 400
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
) -> None:
    from werkzeug.serving import make_server

    url = f"http://{host}:{port}/"
    print(f"Dedupe UI: {url}")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # make_server (rather than app.run) so /api/shutdown can stop the loop via
    # server.shutdown() and let the process exit cleanly when the tab closes.
    server = make_server(host, port, app, threaded=True)
    app.extensions["dedupe_server"] = server
    print("Press CTRL+C to quit (closing the browser tab also stops the server)")
    try:
        server.serve_forever()
    finally:
        app.extensions.pop("dedupe_server", None)
        server.server_close()
