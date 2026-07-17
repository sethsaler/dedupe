"""Flask web UI for browsing and acting on duplicate groups."""

from __future__ import annotations

import mimetypes
import threading
import webbrowser
from pathlib import Path

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
from ..models import ScanProgress, ScanResult, SmartRule

# Shared scan state for background jobs
_lock = threading.Lock()
_state: dict = {
    "result": None,  # ScanResult | None
    "progress": ScanProgress(),
    "scanning": False,
    "last_error": None,
    "groups_version": 0,  # bumps when groups list changes (for streaming UI)
}


def create_app(initial_result: ScanResult | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["SECRET_KEY"] = "dedupe-local-only"

    if initial_result is not None:
        with _lock:
            _state["result"] = initial_result
            _state["progress"] = ScanProgress(
                phase="done",
                done=True,
                files_found=len(initial_result.files),
                groups_found=len(initial_result.groups),
                message="Loaded previous scan",
            )

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def api_status():
        import os

        from ..parallel import DEFAULT_WORKERS_CAP, resolve_workers

        with _lock:
            prog = _state["progress"]
            result: ScanResult | None = _state["result"]
            cpu = os.cpu_count() or 1
            payload = {
                "scanning": _state["scanning"],
                "progress": prog.to_dict(),
                "has_result": result is not None,
                "groups_version": _state["groups_version"],
                "error": _state["last_error"],
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
                    "reclaimable_bytes": result.reclaimable_bytes,
                    "reclaimable_human": format_bytes(result.reclaimable_bytes),
                }
            return jsonify(payload)

    @app.get("/api/groups")
    def api_groups():
        kind = request.args.get("kind")  # exact | similar | all
        with _lock:
            result: ScanResult | None = _state["result"]
            if result is None:
                return jsonify({"groups": []})
            groups = result.groups
            if kind in ("exact", "similar"):
                groups = [g for g in groups if g.kind.value == kind]
            return jsonify({"groups": [g.to_dict() for g in groups]})

    @app.get("/api/groups/<group_id>")
    def api_group(group_id: str):
        with _lock:
            result: ScanResult | None = _state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            for g in result.groups:
                if g.id == group_id:
                    return jsonify(g.to_dict())
        return jsonify({"error": "not found"}), 404

    @app.post("/api/scan")
    def api_scan():
        data = request.get_json(force=True, silent=True) or {}
        paths = data.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        paths = [p for p in paths if p and str(p).strip()]
        if not paths:
            return jsonify({"error": "paths required"}), 400

        resolved_roots = [str(Path(p).expanduser().resolve()) for p in paths]

        with _lock:
            if _state["scanning"]:
                return jsonify({"error": "scan already running"}), 409
            _state["scanning"] = True
            _state["last_error"] = None
            _state["progress"] = ScanProgress(phase="starting", message="Starting…")
            # Empty result so the UI can stream groups as they appear.
            _state["result"] = ScanResult(roots=resolved_roots, files=[], groups=[])
            _state["groups_version"] = _state.get("groups_version", 0) + 1

        def worker() -> None:
            try:

                def on_progress(prog: ScanProgress) -> None:
                    with _lock:
                        _state["progress"] = prog

                def on_group(group) -> None:
                    with _lock:
                        result: ScanResult | None = _state["result"]
                        if result is None:
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
                        _state["groups_version"] = _state.get("groups_version", 0) + 1
                        prog = _state["progress"]
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

                result = run_scan(
                    paths,
                    exact=bool(data.get("exact", True)),
                    similar=bool(data.get("similar", True)),
                    include_images=bool(data.get("include_images", True)),
                    include_gifs=bool(data.get("include_gifs", True)),
                    include_videos=bool(data.get("include_videos", True)),
                    include_hidden=bool(data.get("include_hidden", False)),
                    image_threshold=int(data.get("threshold", 6)),
                    video_threshold=int(data.get("video_threshold", 8)),
                    use_cache=bool(data.get("use_cache", True)),
                    workers=workers,
                    progress=on_progress,
                    on_group=on_group,
                )
                with _lock:
                    _state["result"] = result
                    _state["scanning"] = False
                    _state["groups_version"] = _state.get("groups_version", 0) + 1
                    _state["progress"] = ScanProgress(
                        phase="done",
                        done=True,
                        files_found=len(result.files),
                        groups_found=len(result.groups),
                        message=(
                            f"Done — {result.exact_groups} exact, "
                            f"{result.similar_groups} similar"
                        ),
                    )
            except Exception as exc:
                with _lock:
                    _state["scanning"] = False
                    _state["last_error"] = str(exc)
                    _state["progress"] = ScanProgress(
                        phase="error",
                        done=True,
                        error=str(exc),
                        message=str(exc),
                    )

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({"ok": True})

    @app.post("/api/smart-select")
    def api_smart_select():
        data = request.get_json(force=True, silent=True) or {}
        rule_raw = data.get("rule", SmartRule.AUTOMATIC.value)
        group_id = data.get("group_id")
        try:
            rule = SmartRule(rule_raw)
        except ValueError:
            return jsonify({"error": f"invalid rule: {rule_raw}"}), 400

        with _lock:
            result: ScanResult | None = _state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            if group_id:
                for g in result.groups:
                    if g.id == group_id:
                        apply_smart_select(g, rule)
                        return jsonify(g.to_dict())
                return jsonify({"error": "not found"}), 404
            apply_smart_select_all(result.groups, rule)
            result.recompute_stats()
            return jsonify({"ok": True, "group_count": len(result.groups)})

    @app.post("/api/selection")
    def api_selection():
        """Set selected_for_removal for a group (manual checkboxes)."""
        data = request.get_json(force=True, silent=True) or {}
        group_id = data.get("group_id")
        selected = list(data.get("selected") or [])
        if not group_id:
            return jsonify({"error": "group_id required"}), 400

        with _lock:
            result: ScanResult | None = _state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            for g in result.groups:
                if g.id == group_id:
                    member_paths = {m.path for m in g.members}
                    picks = [p for p in selected if p in member_paths]
                    # Enforce keep-at-least-one
                    if len(picks) >= len(member_paths) and member_paths:
                        keep = g.suggested_keep or next(iter(member_paths))
                        if keep in picks:
                            picks = [p for p in picks if p != keep]
                        else:
                            picks = picks[:-1]
                    g.selected_for_removal = picks
                    return jsonify(g.to_dict())
        return jsonify({"error": "not found"}), 404

    @app.post("/api/action")
    def api_action():
        data = request.get_json(force=True, silent=True) or {}
        action = (data.get("action") or "trash").lower()
        dry_run = bool(data.get("dry_run", True))
        quarantine_dir = data.get("quarantine_dir")

        if action not in ("trash", "quarantine", "isolate"):
            return jsonify({"error": "action must be trash, quarantine, or isolate"}), 400
        if action == "quarantine" and not quarantine_dir and not dry_run:
            return jsonify({"error": "quarantine_dir required"}), 400

        with _lock:
            result: ScanResult | None = _state["result"]
            if result is None:
                return jsonify({"error": "no scan"}), 404
            groups = list(result.groups)
            roots = list(result.roots)

        try:
            if action == "isolate":
                mode = (data.get("isolate_mode") or "copy").lower()
                kinds_raw = data.get("isolate_kinds") or "all"
                kinds = None if kinds_raw == "all" else {kinds_raw}
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
                )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

        # If trash/quarantine executed, drop removed members from in-memory result
        if not dry_run and action in ("trash", "quarantine"):
            removed = {i.path for i in action_result.items if i.ok}
            with _lock:
                result = _state["result"]
                if result is not None:
                    new_groups = []
                    for g in result.groups:
                        remaining = [m for m in g.members if m.path not in removed]
                        if len(remaining) >= 2:
                            g.members = remaining
                            g.selected_for_removal = [
                                p for p in g.selected_for_removal if p not in removed
                            ]
                            if g.suggested_keep in removed:
                                from ..grouping import pick_suggested_keep

                                g.suggested_keep = pick_suggested_keep(remaining)
                            new_groups.append(g)
                        # groups with <2 members dissolve
                    result.groups = new_groups
                    result.files = [f for f in result.files if f.path not in removed]
                    result.recompute_stats()

        return jsonify(action_result.to_dict())

    @app.post("/api/pick-folder")
    def api_pick_folder():
        """Open a native folder picker (macOS/Linux). Returns selected path or cancelled."""
        import platform
        import subprocess

        system = platform.system()
        try:
            if system == "Darwin":
                script = (
                    'try\n'
                    '  set theFolder to choose folder with prompt "Choose a folder to scan"\n'
                    '  return POSIX path of theFolder\n'
                    'on error number -128\n'
                    '  return ""\n'
                    'end try'
                )
                proc = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                path = (proc.stdout or "").strip()
                if not path:
                    return jsonify({"cancelled": True, "path": None})
                # AppleScript POSIX path ends with /
                path = path.rstrip("/")
                return jsonify({"path": path, "cancelled": False})

            if system == "Linux":
                for cmd in (
                    ["zenity", "--file-selection", "--directory", "--title=Choose a folder to scan"],
                    ["kdialog", "--getexistingdirectory", ".", "--title", "Choose a folder to scan"],
                ):
                    try:
                        proc = subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            timeout=300,
                            check=False,
                        )
                        if proc.returncode == 0:
                            path = (proc.stdout or "").strip()
                            if path:
                                return jsonify({"path": path, "cancelled": False})
                            return jsonify({"cancelled": True, "path": None})
                    except FileNotFoundError:
                        continue
                return jsonify({
                    "cancelled": False,
                    "path": None,
                    "message": "No folder dialog available — paste a path instead",
                })

            return jsonify({
                "cancelled": False,
                "path": None,
                "message": "Folder picker not supported on this OS — paste a path instead",
            })
        except subprocess.TimeoutExpired:
            return jsonify({"cancelled": True, "path": None})
        except Exception as exc:
            return jsonify({"error": str(exc), "path": None}), 500

    @app.get("/api/thumbnail")
    def api_thumbnail():
        path = request.args.get("path", "")
        if not path:
            return jsonify({"error": "path required"}), 400
        p = Path(path)
        if not p.is_file():
            return jsonify({"error": "not found"}), 404

        # Only serve files that were part of the last scan (path traversal safety)
        with _lock:
            result: ScanResult | None = _state["result"]
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
