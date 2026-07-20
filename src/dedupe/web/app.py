"""Flask web UI for browsing and acting on duplicate groups."""

from __future__ import annotations

import hmac
import mimetypes
import secrets
import shutil
import threading
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

from ..actions import apply_actions, format_bytes, isolate_groups
from ..engine import run_scan
from ..grouping import apply_smart_select, apply_smart_select_all
from ..human_detection import DEFAULT_PHOTON_MODEL, HUMAN_BACKENDS
from ..models import ScanProgress, ScanResult, SmartRule, effective_selected_paths


def _macos_picker_script(kind: str) -> str:
    chooser = (
        'choose folder with prompt "Choose folders to scan" '
        "with multiple selections allowed"
        if kind == "folder"
        else 'choose file with prompt "Choose media files to scan" '
        "with multiple selections allowed"
    )
    return (
        'use framework "AppKit"\n'
        "use scripting additions\n"
        "try\n"
        "  current application's NSApplication's sharedApplication()'s "
        "activateIgnoringOtherApps:true\n"
        f"  set selectedItems to {chooser}\n"
        '  set output to ""\n'
        "  repeat with selectedItem in selectedItems\n"
        "    set output to output & POSIX path of selectedItem & linefeed\n"
        "  end repeat\n"
        "  return output\n"
        "on error number -128\n"
        '  return ""\n'
        "end try"
    )


def create_app(initial_result: ScanResult | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    csrf_token = secrets.token_urlsafe(32)
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    app.config["DEDUPE_CSRF_TOKEN"] = csrf_token
    app.config["DEDUPE_RECOVERY_DIR"] = str(
        Path.home() / ".cache" / "dedupe" / "recovery"
    )
    app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost", "[::1]"]
    lock = threading.RLock()
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
    }
    app.extensions["dedupe_state"] = state

    def group_payload(group) -> dict:
        payload = group.to_dict()
        deleted = state["deleted_files"]
        payload["deleted_paths"] = [
            member.path for member in group.members if member.path in deleted
        ]
        return payload

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
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    if initial_result is not None:
        with lock:
            state["result"] = initial_result
            state["progress"] = ScanProgress(
                phase="done",
                done=True,
                files_found=len(initial_result.files),
                groups_found=len(initial_result.groups),
                message="Loaded previous scan",
            )

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

    @app.get("/api/status")
    def api_status():
        import os

        from ..parallel import DEFAULT_WORKERS_CAP, resolve_workers

        with lock:
            prog = state["progress"]
            result: ScanResult | None = state["result"]
            cpu = os.cpu_count() or 1
            payload = {
                "scanning": state["scanning"],
                "acting": state["acting"],
                "progress": prog.to_dict(),
                "has_result": result is not None,
                "groups_version": state["groups_version"],
                "scan_id": state["scan_id"],
                "error": state["last_error"],
                "system": {
                    "cpu_count": cpu,
                    "auto_workers": resolve_workers(None),
                    "max_workers": max(cpu, DEFAULT_WORKERS_CAP),
                    "workers_cap": DEFAULT_WORKERS_CAP,
                },
            }
            if result is not None:
                result.recompute_stats()
                payload["summary"] = {
                    "roots": result.roots,
                    "file_count": len(result.files),
                    "group_count": len(result.groups),
                    "exact_groups": result.exact_groups,
                    "similar_groups": result.similar_groups,
                    "no_human_files": result.no_human_files,
                    "reclaimable_bytes": result.reclaimable_bytes,
                    "reclaimable_human": format_bytes(result.reclaimable_bytes),
                    "selected_count": len(effective_selected_paths(result.groups)),
                    "errors": list(result.errors[:20]),
                }
            return jsonify(payload)

    @app.get("/api/groups")
    def api_groups():
        kind = request.args.get("kind")  # exact | similar | all
        with lock:
            result: ScanResult | None = state["result"]
            if result is None:
                return jsonify({"groups": []})
            groups = result.groups
            if kind in ("exact", "similar", "no_humans"):
                groups = [g for g in groups if g.kind.value == kind]
            return jsonify({"groups": [group_payload(g) for g in groups]})

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
            cancel_event = threading.Event()
            state["scanning"] = True
            state["scan_id"] = scan_id
            state["cancel_event"] = cancel_event
            state["last_error"] = None
            state["deleted_files"] = {}
            state["progress"] = ScanProgress(phase="starting", message="Starting…")
            # Empty result so the UI can stream groups as they appear.
            state["result"] = ScanResult(roots=resolved_roots, files=[], groups=[])
            state["groups_version"] = state.get("groups_version", 0) + 1

        def worker() -> None:
            try:

                def on_progress(prog: ScanProgress) -> None:
                    with lock:
                        if state["scan_id"] == scan_id:
                            state["progress"] = prog

                def on_group(group) -> None:
                    with lock:
                        result: ScanResult | None = state["result"]
                        if result is None or state["scan_id"] != scan_id:
                            return
                        # Replace if same id already streamed (shouldn't happen), else append
                        existing = next(
                            (i for i, g in enumerate(result.groups) if g.id == group.id),
                            None,
                        )
                        if existing is not None:
                            result.groups[existing] = group
                        else:
                            result.groups.append(group)
                        result.groups.sort(
                            key=lambda g: g.reclaimable_bytes, reverse=True
                        )
                        result.recompute_stats()
                        state["groups_version"] = state.get("groups_version", 0) + 1
                        prog = state["progress"]
                        prog.groups_found = len(result.groups)

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

                result = run_scan(
                    paths,
                    exact=bool(data.get("exact", True)),
                    similar=bool(data.get("similar", True)),
                    find_no_humans=bool(data.get("find_no_humans", False)),
                    human_backend=human_backend,
                    photon_model=photon_model,
                    include_images=bool(data.get("include_images", True)),
                    include_gifs=bool(data.get("include_gifs", True)),
                    include_videos=bool(data.get("include_videos", True)),
                    include_hidden=bool(data.get("include_hidden", False)),
                    image_threshold=int(data.get("threshold", 6)),
                    video_threshold=int(data.get("video_threshold", 8)),
                    use_cache=bool(data.get("use_cache", True)),
                    workers=workers,
                    exclusions=[
                        str(pattern).strip()
                        for pattern in raw_exclusions
                        if str(pattern).strip()
                    ],
                    cancelled=cancel_event.is_set,
                    progress=on_progress,
                    on_group=on_group,
                )
                with lock:
                    if state["scan_id"] != scan_id:
                        return
                    state["result"] = result
                    state["scanning"] = False
                    state["cancel_event"] = None
                    state["groups_version"] = state.get("groups_version", 0) + 1
                    state["progress"] = ScanProgress(
                        phase="done",
                        done=True,
                        files_found=len(result.files),
                        groups_found=len(result.groups),
                        message=(
                            f"Done — {result.exact_groups} exact, "
                            f"{result.similar_groups} similar, "
                            f"{result.no_human_files} non-human"
                        ),
                    )
            except Exception as exc:
                with lock:
                    if state["scan_id"] != scan_id:
                        return
                    was_cancelled = isinstance(exc, InterruptedError)
                    state["scanning"] = False
                    state["cancel_event"] = None
                    state["last_error"] = None if was_cancelled else str(exc)
                    state["progress"] = ScanProgress(
                        phase="cancelled" if was_cancelled else "error",
                        done=True,
                        error=None if was_cancelled else str(exc),
                        message="Scan cancelled" if was_cancelled else str(exc),
                    )

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

    @app.post("/api/smart-select")
    def api_smart_select():
        data = request.get_json(silent=True) or {}
        rule_raw = data.get("rule", SmartRule.AUTOMATIC.value)
        group_id = data.get("group_id")
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
                        return jsonify(group_payload(g))
                return jsonify({"error": "not found"}), 404
            apply_smart_select_all(result.groups, rule)
            result.recompute_stats()
            return jsonify({"ok": True, "group_count": len(result.groups)})

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
                    picks = [p for p in selected if p in member_paths]
                    # Duplicate groups retain one file; no-human candidate groups may remove all.
                    if (
                        g.kind.value != "no_humans"
                        and len(picks) >= len(member_paths)
                        and member_paths
                    ):
                        keep = g.suggested_keep or next(iter(member_paths))
                        if keep in picks:
                            picks = [p for p in picks if p != keep]
                        else:
                            picks = picks[:-1]
                    g.selected_for_removal = picks
                    if g.kind.value == "no_humans":
                        reviewed = list(data.get("reviewed") or picks)
                        g.reviewed_paths = [
                            path for path in reviewed if path in member_paths
                        ]
                    return jsonify(group_payload(g))
        return jsonify({"error": "not found"}), 404

    @app.post("/api/non-human/delete")
    def api_delete_non_human():
        """Remove one non-human candidate while retaining a recoverable copy."""
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
                    if candidate.id == group_id and candidate.kind.value == "no_humans"
                ),
                None,
            )
            if group is None or path not in {member.path for member in group.members}:
                return jsonify({"error": "non-human candidate not found"}), 404
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

        try:
            recovery_dir = Path(app.config["DEDUPE_RECOVERY_DIR"]) / state["scan_id"]
            action_result = apply_actions(
                [action_group],
                action="quarantine",
                quarantine_dir=recovery_dir,
                dry_run=False,
                roots=roots,
            )
            item = next((item for item in action_result.items if item.path == path), None)
            if item is None or not item.ok or not item.destination:
                error = item.error if item else "delete did not complete"
                return jsonify({"error": error}), 400
            with lock:
                state["deleted_files"][path] = item.destination
                group.selected_for_removal = [
                    selected for selected in original_selected if selected != path
                ]
                group.reviewed_paths = list(dict.fromkeys([*original_reviewed, path]))
                return jsonify(group_payload(group))
        finally:
            with lock:
                state["acting"] = False

    @app.post("/api/non-human/undo")
    def api_undo_non_human():
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
                    if candidate.id == group_id and candidate.kind.value == "no_humans"
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
                return jsonify({"error": "the recoverable file no longer exists"}), 404
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(recoverable), str(original))
            with lock:
                state["deleted_files"].pop(path, None)
                return jsonify(group_payload(group))
        except OSError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            with lock:
                state["acting"] = False

    @app.post("/api/action")
    def api_action():
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "trash").lower()
        dry_run = bool(data.get("dry_run", True))
        quarantine_dir = data.get("quarantine_dir")

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
            kinds = None if kinds_raw in ("all", "") else {kinds_raw}
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
                action_result = apply_actions(
                    groups,
                    action=action,
                    quarantine_dir=quarantine_dir,
                    dry_run=dry_run,
                    roots=roots,
                    kinds=kinds,
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
                            minimum = 1 if group.kind.value == "no_humans" else 2
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

            with lock:
                if action_result.review_root:
                    state["allowed_reveal_paths"].add(
                        str(Path(action_result.review_root).resolve(strict=False))
                    )

            return jsonify(action_result.to_dict())
        finally:
            with lock:
                state["acting"] = False

    @app.post("/api/pick-folder")
    def api_pick_folder():
        """Open a native folder/file picker and return local filesystem paths."""
        import platform
        import subprocess

        data = request.get_json(silent=True) or {}
        kind = str(data.get("kind") or "folder").lower()
        if kind not in {"folder", "files"}:
            return jsonify({"error": "kind must be folder or files"}), 400

        def response_for_process(proc) -> tuple[Response, int] | Response:
            if proc.returncode != 0:
                detail = (proc.stderr or "").strip()
                return (
                    jsonify({
                        "error": detail or "The native file picker could not open",
                        "paths": [],
                    }),
                    500,
                )
            paths = [
                str(Path(line.strip()).expanduser().resolve(strict=False))
                for line in (proc.stdout or "").splitlines()
                if line.strip()
            ]
            if not paths:
                return jsonify({"cancelled": True, "path": None, "paths": []})
            return jsonify({
                "path": paths[0],
                "paths": paths,
                "cancelled": False,
            })

        system = platform.system()
        try:
            if system == "Darwin":
                script = _macos_picker_script(kind)
                proc = subprocess.run(
                    ["/usr/bin/osascript", "-e", script],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                return response_for_process(proc)

            if system == "Linux":
                commands = (
                    (
                        [
                            "zenity",
                            "--file-selection",
                            "--directory",
                            "--multiple",
                            "--separator=\n",
                            "--title=Choose folders to scan",
                        ],
                        [
                            "kdialog",
                            "--getexistingdirectory",
                            ".",
                            "--multiple",
                            "--separate-output",
                            "--title",
                            "Choose folders to scan",
                        ],
                    )
                    if kind == "folder"
                    else (
                        [
                            "zenity",
                            "--file-selection",
                            "--multiple",
                            "--separator=\n",
                            "--title=Choose media files to scan",
                        ],
                        [
                            "kdialog",
                            "--getopenfilename",
                            ".",
                            "Media files (*)",
                            "--multiple",
                            "--separate-output",
                            "--title",
                            "Choose media files to scan",
                        ],
                    )
                )
                for cmd in commands:
                    try:
                        proc = subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            timeout=300,
                            check=False,
                        )
                        if proc.returncode == 0:
                            return response_for_process(proc)
                        if proc.returncode == 1 and not (proc.stdout or "").strip():
                            return jsonify({
                                "cancelled": True,
                                "path": None,
                                "paths": [],
                            })
                        detail = (proc.stderr or "").strip()
                        if detail:
                            return jsonify({"error": detail, "paths": []}), 500
                    except FileNotFoundError:
                        continue
                return jsonify({
                    "cancelled": False,
                    "path": None,
                    "paths": [],
                    "message": "No native picker is installed — paste paths instead",
                })

            return jsonify({
                "cancelled": False,
                "path": None,
                "paths": [],
                "message": "Native picker not supported on this OS — paste paths instead",
            })
        except subprocess.TimeoutExpired:
            return jsonify({"error": "The native picker timed out", "paths": []}), 504
        except Exception as exc:
            return jsonify({"error": str(exc), "paths": []}), 500

    @app.get("/api/thumbnail")
    def api_thumbnail():
        path = request.args.get("path", "")
        if not path:
            return jsonify({"error": "path required"}), 400
        p = Path(path)
        if not p.is_file():
            return jsonify({"error": "not found"}), 404

        # Only serve files that were part of the last scan (path traversal safety)
        with lock:
            result: ScanResult | None = state["result"]
            allowed = {f.path for f in result.files} if result else set()
            if result and result.groups:
                for g in result.groups:
                    for m in g.members:
                        allowed.add(m.path)
        if str(p.resolve()) not in allowed and path not in allowed:
            # also allow resolve match
            resolved = str(p.resolve())
            if resolved not in allowed:
                return jsonify({"error": "not in scan"}), 403

        ext = p.suffix.lower()
        # Videos: try a cached frame if we can't stream
        if ext in {
            ".mp4",
            ".mov",
            ".m4v",
            ".avi",
            ".mkv",
            ".webm",
            ".mts",
            ".m2ts",
            ".wmv",
            ".flv",
            ".3gp",
        }:
            try:
                thumb = _video_thumb_bytes(p)
                if thumb:
                    return Response(thumb, mimetype="image/jpeg")
            except Exception:
                pass
            return jsonify({"error": "no preview"}), 404

        # Images / GIFs via Pillow resize
        # ?full=1 → larger lightbox preview
        full = request.args.get("full") == "1"
        max_edge = 1600 if full else 320
        quality = 88 if full else 80
        try:
            from io import BytesIO

            from PIL import Image

            try:
                from pillow_heif import register_heif_opener

                register_heif_opener()
            except Exception:
                pass

            with Image.open(p) as img:
                img = img.convert("RGB")
                img.thumbnail((max_edge, max_edge))
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                return Response(buf.getvalue(), mimetype="image/jpeg")
        except Exception:
            mime, _ = mimetypes.guess_type(str(p))
            return send_file(p, mimetype=mime or "application/octet-stream")

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

    return app


def _video_thumb_bytes(path: Path) -> bytes | None:
    import subprocess
    import tempfile

    if not shutil_which("ffmpeg"):
        return None
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        out = tmp.name
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "1",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:-1",
                "-y",
                out,
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        data = Path(out).read_bytes()
        return data if data else None
    except Exception:
        return None
    finally:
        try:
            Path(out).unlink(missing_ok=True)
        except Exception:
            pass


def shutil_which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)


def run_app(
    app: Flask,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    url = f"http://{host}:{port}/"
    print(f"Dedupe UI: {url}")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
