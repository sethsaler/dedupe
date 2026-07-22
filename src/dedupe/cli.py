"""Command-line interface for dedupe."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .actions import (
    apply_actions,
    isolate_groups,
    summarize_scan,
    undo_quarantine,
)
from .engine import run_scan, run_scans_parallel
from .grouping import apply_smart_select_all
from .human_detection import DEFAULT_PHOTON_MODEL, HUMAN_BACKENDS
from .models import SmartRule


def application_version() -> str:
    """Return the installed distribution version, including in source checkouts."""
    try:
        return importlib.metadata.version("dedupe-media")
    except importlib.metadata.PackageNotFoundError:
        return __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dedupe",
        description="Find duplicate images, videos, and GIFs (Gemini-style).",
    )
    p.add_argument("--version", action="version", version=f"dedupe {application_version()}")
    sub = p.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan folders for duplicates")
    scan.add_argument("paths", nargs="+", help="Folders or files to scan")
    scan.add_argument("--no-exact", action="store_true", help="Skip exact duplicate detection")
    scan.add_argument("--no-similar", action="store_true", help="Skip similar detection")
    scan.add_argument(
        "--find-no-person",
        "--find-no-humans",
        dest="find_no_humans",
        action="store_true",
        help="Surface non-human media: images/videos where OpenCV detects no person",
    )
    scan.add_argument(
        "--human-backend",
        choices=HUMAN_BACKENDS,
        default="opencv",
        help="Person detector: opencv (default), photon, or ensemble",
    )
    scan.add_argument(
        "--photon-model",
        default=DEFAULT_PHOTON_MODEL,
        metavar="MODEL",
        help=(
            f"Local Moondream model for Photon (default: {DEFAULT_PHOTON_MODEL}; "
            "first use downloads model weights)"
        ),
    )
    scan.add_argument("--no-images", action="store_true")
    scan.add_argument("--no-gifs", action="store_true")
    scan.add_argument("--no-videos", action="store_true")
    scan.add_argument("--hidden", action="store_true", help="Include hidden files")
    scan.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Exclude a file/folder name or root-relative glob (repeatable)",
    )
    scan.add_argument("--threshold", type=int, default=6, help="Image pHash Hamming threshold")
    scan.add_argument("--video-threshold", type=int, default=8, help="Video fingerprint threshold")
    scan.add_argument(
        "--workers",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Parallel workers for hashing and OpenCV person detection "
            "(0 = auto, conservative; 1 = serial; each stage has a safety cap)"
        ),
    )
    scan.add_argument(
        "--parallel",
        action="store_true",
        help=(
            "Scan each folder as an independent, concurrent stream "
            "(no cross-folder dedup; groups only contain files from one folder)"
        ),
    )
    scan.add_argument(
        "--max-streams",
        type=int,
        default=0,
        metavar="N",
        help="Max folders to scan at once with --parallel (0 = auto)",
    )
    scan.add_argument(
        "--no-cache",
        action="store_true",
        help="Recompute hashes and person checks instead of reusing unchanged files",
    )
    scan.add_argument("--json", dest="json_out", metavar="FILE", help="Write results JSON")
    scan.add_argument(
        "--smart",
        choices=[r.value for r in SmartRule],
        default=SmartRule.AUTOMATIC.value,
    )
    scan.add_argument(
        "--action",
        choices=["none", "trash", "quarantine", "isolate"],
        default="none",
        help="Action after scan (default: report only). "
        "'isolate' copies each group into review folders for human inspection.",
    )
    scan.add_argument("--quarantine-dir", type=str, default=None)
    scan.add_argument(
        "--review-dir",
        type=str,
        default=None,
        help="Destination root for --action isolate "
        "(default: <scanned source>/_Dedupe Review — always inside the source tree)",
    )
    scan.add_argument(
        "--isolate-mode",
        choices=["copy", "hardlink", "symlink", "move"],
        default="copy",
        help="How isolate places files (default: copy — originals stay put)",
    )
    scan.add_argument(
        "--isolate-kinds",
        choices=["all", "exact", "similar", "no_humans"],
        default="all",
        help="Which group kinds to isolate (default: all)",
    )
    scan.add_argument("--dry-run", action="store_true", default=True)
    scan.add_argument("--execute", action="store_true", help="Actually perform --action")
    scan.add_argument("--ui", action="store_true", help="Open web UI after scan")
    scan.add_argument("--port", type=int, default=8765)

    ui = sub.add_parser("ui", help="Open the local web UI")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--load", metavar="JSON", help="Load a previous scan result")

    # Standalone: isolate from a previous results JSON without re-scanning
    isolate = sub.add_parser(
        "isolate",
        help="Isolate groups from a previous --json scan into review folders",
    )
    isolate.add_argument("json_file", help="results.json from `dedupe scan --json`")
    isolate.add_argument(
        "--review-dir",
        type=str,
        default=None,
        help="Destination root for review folders "
        "(default: <scan root from JSON>/_Dedupe Review)",
    )

    isolate.add_argument(
        "--isolate-mode",
        choices=["copy", "hardlink", "symlink", "move"],
        default="copy",
    )
    isolate.add_argument(
        "--isolate-kinds",
        choices=["all", "exact", "similar", "no_humans"],
        default="all",
    )
    isolate.add_argument("--dry-run", action="store_true")
    isolate.add_argument(
        "--execute",
        action="store_true",
        help="Actually create folders (default is dry-run unless --execute)",
    )

    undo = sub.add_parser(
        "undo",
        help="Restore an executed quarantine action from its JSON receipt",
    )
    undo.add_argument("action_log", help="Quarantine action receipt JSON")
    undo.add_argument(
        "--execute",
        action="store_true",
        help="Actually restore files (default is a dry-run preview)",
    )

    benchmark = sub.add_parser(
        "benchmark-humans",
        help="Compare person detectors against a labeled JSON manifest",
    )
    benchmark.add_argument(
        "manifest",
        help="JSON list of {path, has_person}; relative paths use the manifest folder",
    )
    benchmark.add_argument(
        "--backends",
        nargs="+",
        choices=HUMAN_BACKENDS,
        default=["opencv"],
        help=(
            "Backends to compare (default: opencv). Selecting photon or ensemble "
            "may download model weights on first use."
        ),
    )
    benchmark.add_argument(
        "--photon-model",
        default=DEFAULT_PHOTON_MODEL,
        metavar="MODEL",
    )
    benchmark.add_argument(
        "--json",
        dest="json_out",
        metavar="FILE",
        help="Write detailed predictions and metrics as JSON",
    )

    doctor = sub.add_parser("doctor", help="Check dependencies and writable application paths")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    similarity = sub.add_parser(
        "benchmark-similarity",
        help="Evaluate similarity detection against a labeled pair manifest",
    )
    similarity.add_argument("manifest", help="JSON list of labeled media pairs")
    similarity.add_argument("--json", dest="json_out", required=True, metavar="OUTPUT")
    similarity.add_argument("--threshold", type=int, default=6, help="Image threshold")
    similarity.add_argument("--video-threshold", type=int, default=8, help="Video threshold")
    similarity.add_argument("--workers", type=int, default=None, metavar="N")

    return p


def cmd_scan(args: argparse.Namespace) -> int:
    def on_progress(prog) -> None:
        pct = ""
        if prog.files_found:
            pct = f" [{prog.files_processed}/{prog.files_found}]"
        print(f"\r{prog.phase}{pct}: {prog.message}    ", end="", flush=True)
        if prog.done:
            print()

    scan_kwargs = dict(
        exact=not args.no_exact,
        similar=not args.no_similar,
        find_no_humans=args.find_no_humans,
        human_backend=args.human_backend,
        photon_model=args.photon_model,
        include_images=not args.no_images,
        include_gifs=not args.no_gifs,
        include_videos=not args.no_videos,
        include_hidden=args.hidden,
        image_threshold=args.threshold,
        video_threshold=args.video_threshold,
        use_cache=not args.no_cache,
        workers=args.workers,
        exclusions=args.exclude,
        progress=on_progress,
    )
    if args.parallel:
        result = run_scans_parallel(
            args.paths,
            max_streams=args.max_streams or None,
            **scan_kwargs,
        )
    else:
        result = run_scan(args.paths, **scan_kwargs)

    apply_smart_select_all(result.groups, SmartRule(args.smart))
    print(summarize_scan(result))
    diagnostics = result.diagnostics
    failed = sum(stage.failed for stage in diagnostics.stages.values())
    warnings = [
        warning
        for stage in diagnostics.stages.values()
        for warning in stage.warnings
    ]
    print(
        f"Diagnostics: {failed} failed across scan stages, "
        f"{diagnostics.cache_hits} cache hits, {diagnostics.total_duration_seconds:.2f}s"
    )
    for name, stage in diagnostics.stages.items():
        if stage.failed or stage.warnings:
            print(
                f"  {name}: {stage.failed} failed, {stage.skipped} skipped "
                f"({stage.unit})"
            )
    for warning in warnings[:10]:
        print(f"  warning: {warning}")
    if len(warnings) > 10:
        print(f"  … +{len(warnings) - 10} more warnings")

    if args.json_out:
        out = Path(args.json_out)
        out.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        print(f"Wrote {out}")

    if args.action != "none":
        dry = not args.execute
        if args.action == "quarantine":
            if not args.quarantine_dir:
                print("error: --quarantine-dir required for quarantine", file=sys.stderr)
                return 2
            action_result = apply_actions(
                result.groups,
                action="quarantine",
                quarantine_dir=args.quarantine_dir,
                dry_run=dry,
                roots=result.roots,
            )
        elif args.action == "trash":
            action_result = apply_actions(
                result.groups,
                action="trash",
                dry_run=dry,
                roots=result.roots,
            )
        elif args.action == "isolate":
            kinds = None if args.isolate_kinds == "all" else {args.isolate_kinds}
            action_result = isolate_groups(
                result.groups,
                args.review_dir,  # None → inside scanned source
                mode=args.isolate_mode,
                kinds=kinds,
                dry_run=dry,
                roots=result.roots,
            )
        else:
            print(f"error: unknown action {args.action}", file=sys.stderr)
            return 2

        mode = "DRY-RUN" if dry else "EXECUTED"
        print(
            f"{mode} {args.action}: {action_result.success_count} ok, "
            f"{action_result.fail_count} failed"
        )
        if action_result.review_root:
            print(f"Review root: {action_result.review_root}")
            print(f"Group folders: {len(action_result.group_dirs)}")
        if action_result.log_path:
            print(f"Log: {action_result.log_path}")

    if args.ui:
        from .web.app import create_app, run_app

        app = create_app(initial_result=result)
        run_app(app, port=args.port, open_browser=True)

    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .web.app import create_app, run_app
    from .models import ScanResult

    initial = None
    if args.load:
        data = json.loads(Path(args.load).read_text(encoding="utf-8"))
        initial = ScanResult.from_dict(data)
    app = create_app(initial_result=initial)
    run_app(app, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_isolate(args: argparse.Namespace) -> int:
    from .models import ScanResult

    data = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    result = ScanResult.from_dict(data)
    dry = not args.execute
    # argparse sets dry_run True if flag present; treat explicit --dry-run as force dry
    if getattr(args, "dry_run", False) and not args.execute:
        dry = True
    # Default for isolate subcommand: dry unless --execute
    if not args.execute:
        dry = True

    kinds = None if args.isolate_kinds == "all" else {args.isolate_kinds}
    action_result = isolate_groups(
        result.groups,
        args.review_dir,  # None → <scan root>/_Dedupe Review from JSON
        mode=args.isolate_mode,
        kinds=kinds,
        dry_run=dry,
        roots=result.roots,
    )
    mode = "DRY-RUN" if dry else "EXECUTED"
    print(
        f"{mode} isolate: {action_result.success_count} ok, "
        f"{action_result.fail_count} failed"
    )
    print(f"Review root: {action_result.review_root}")
    print(f"Group folders: {len(action_result.group_dirs)}")
    for d in action_result.group_dirs[:20]:
        print(f"  {d}")
    if len(action_result.group_dirs) > 20:
        print(f"  … +{len(action_result.group_dirs) - 20} more")
    if action_result.log_path:
        print(f"Log: {action_result.log_path}")
    return 0 if action_result.fail_count == 0 else 1


def cmd_undo(args: argparse.Namespace) -> int:
    result = undo_quarantine(args.action_log, dry_run=not args.execute)
    mode = "DRY-RUN" if result.dry_run else "EXECUTED"
    print(
        f"{mode} undo: {result.success_count} ok, {result.fail_count} failed"
    )
    if result.log_path:
        print(f"Receipt: {result.log_path}")
    return 0 if result.fail_count == 0 else 1


def cmd_benchmark_humans(args: argparse.Namespace) -> int:
    from .human_benchmark import format_benchmark_report, run_human_benchmark

    try:
        report = run_human_benchmark(
            args.manifest,
            backends=args.backends,
            photon_model=args.photon_model,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(format_benchmark_report(report))
    if args.json_out:
        out = Path(args.json_out).expanduser()
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {out}")
    return 1 if any(r.get("error") for r in report["results"].values()) else 0


def _executable_status(name: str) -> dict[str, object]:
    path = shutil.which(name)
    version = None
    if path:
        try:
            completed = subprocess.run(
                [path, "-version"], capture_output=True, text=True, timeout=5, check=False
            )
            first_line = (completed.stdout or completed.stderr).splitlines()
            version = first_line[0] if first_line else None
        except (OSError, subprocess.SubprocessError):
            pass
    return {"available": path is not None, "path": path, "version": version}


def _path_status(path: Path) -> dict[str, object]:
    """Check whether an application file can be created without touching that file."""
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        writable = os.access(parent, os.W_OK)
    except OSError:
        writable = False
    return {"path": str(path), "writable": writable}


def collect_doctor_report() -> dict[str, object]:
    """Collect local diagnostics without scanning media or loading Photon."""
    required_modules = {
        "PIL": "Pillow",
        "imagehash": "ImageHash",
        "pybktree": "pybktree",
        "send2trash": "Send2Trash",
        "flask": "Flask",
    }
    imports: dict[str, dict[str, object]] = {}
    for module, distribution in required_modules.items():
        try:
            importlib.import_module(module)
            try:
                version = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                version = None
            imports[module] = {
                "ok": True,
                "version": version,
            }
        except Exception as exc:
            imports[module] = {"ok": False, "error": str(exc)}

    from .cache import default_cache_path
    from .human_detection import YUNET_MODEL_PATH
    from .review_session import default_review_session_path

    try:
        cv2 = importlib.import_module("cv2")
        opencv = {
            "available": True,
            "version": getattr(cv2, "__version__", None),
            "yunet_model": str(YUNET_MODEL_PATH),
            "yunet_ready": YUNET_MODEL_PATH.is_file(),
        }
    except Exception as exc:
        opencv = {"available": False, "error": str(exc), "yunet_ready": False}

    paths = {
        "cache": _path_status(default_cache_path()),
        "state": _path_status(default_review_session_path()),
    }
    blockers = [f"cannot import {name}" for name, status in imports.items() if not status["ok"]]
    blockers.extend(
        f"{name} path is not writable" for name, status in paths.items() if not status["writable"]
    )
    return {
        "application": {"name": "dedupe", "version": application_version()},
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "platform": {"system": platform.system(), "release": platform.release()},
        "imports": imports,
        "ffmpeg": _executable_status("ffmpeg"),
        "ffprobe": _executable_status("ffprobe"),
        "opencv": opencv,
        "paths": paths,
        "core_ready": not blockers,
        "blockers": blockers,
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    report = collect_doctor_report()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        app = report["application"]
        python = report["python"]
        system = report["platform"]
        print(f"dedupe {app['version']}")
        print(f"Python {python['version']} ({python['executable']})")
        print(f"Platform: {system['system']} {system['release']}")
        for name, status in report["imports"].items():
            print(f"Import {name}: {'ok' if status['ok'] else 'MISSING'}")
        for name in ("ffmpeg", "ffprobe"):
            status = report[name]
            detail = status.get("version") or "not found"
            print(f"{name}: {detail}")
        cv = report["opencv"]
        print(
            f"OpenCV/YuNet (optional): "
            f"{'ready' if cv.get('available') and cv.get('yunet_ready') else 'not ready'}"
        )
        for name, status in report["paths"].items():
            print(f"{name.title()} path: {status['path']} ({'writable' if status['writable'] else 'NOT writable'})")
        print(f"Core operation: {'ready' if report['core_ready'] else 'BLOCKED'}")
    return 0 if report["core_ready"] else 1


def _format_similarity_report(report: dict[str, object]) -> str:
    def pct(value: object) -> str:
        return "n/a" if value is None else f"{float(value) * 100:.1f}%"

    lines = [f"Similarity benchmark: {report['evaluated']}/{report['total']} pairs evaluated"]
    lines.append(f"False positives: {report['false_positives']}")
    lines.extend(f"  {a} <> {b}" for a, b in report["false_positive_pairs"])
    lines.append(f"False negatives: {report['false_negatives']}")
    lines.extend(f"  {a} <> {b}" for a, b in report["false_negative_pairs"])
    lines.append(
        f"Precision: {pct(report['precision'])}  Recall: {pct(report['recall'])}  "
        f"Runtime: {report['elapsed_seconds']:.2f}s"
    )
    if report["errors"]:
        lines.append(f"Errors: {report['errors']}")
    return "\n".join(lines)


def cmd_benchmark_similarity(args: argparse.Namespace) -> int:
    from .similarity_benchmark import run_similarity_benchmark

    try:
        report = run_similarity_benchmark(
            args.manifest,
            image_threshold=args.threshold,
            video_threshold=args.video_threshold,
            workers=args.workers,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(_format_similarity_report(report))
    out = Path(args.json_out).expanduser()
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    return 1 if report["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "ui":
        return cmd_ui(args)
    if args.command == "isolate":
        return cmd_isolate(args)
    if args.command == "undo":
        return cmd_undo(args)
    if args.command == "benchmark-humans":
        return cmd_benchmark_humans(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "benchmark-similarity":
        return cmd_benchmark_similarity(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
