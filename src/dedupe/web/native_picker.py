"""Native macOS and Linux file picker integration."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path


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


def _process_result(proc: subprocess.CompletedProcess[str]) -> tuple[dict, int]:
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        return {
            "error": detail or "The native file picker could not open",
            "paths": [],
        }, 500
    paths = [
        str(Path(line.strip()).expanduser().resolve(strict=False))
        for line in (proc.stdout or "").splitlines()
        if line.strip()
    ]
    if not paths:
        return {"cancelled": True, "path": None, "paths": []}, 200
    return {"path": paths[0], "paths": paths, "cancelled": False}, 200


def pick_native_paths(kind: str) -> tuple[dict, int]:
    """Open the platform picker and return its API-ready payload and status."""
    try:
        if platform.system() == "Darwin":
            proc = subprocess.run(
                ["/usr/bin/osascript", "-e", _macos_picker_script(kind)],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            return _process_result(proc)

        if platform.system() == "Linux":
            commands = (
                (
                    ["zenity", "--file-selection", "--directory", "--multiple", "--separator=\n", "--title=Choose folders to scan"],
                    ["kdialog", "--getexistingdirectory", ".", "--multiple", "--separate-output", "--title", "Choose folders to scan"],
                )
                if kind == "folder"
                else (
                    ["zenity", "--file-selection", "--multiple", "--separator=\n", "--title=Choose media files to scan"],
                    ["kdialog", "--getopenfilename", ".", "Media files (*)", "--multiple", "--separate-output", "--title", "Choose media files to scan"],
                )
            )
            for command in commands:
                try:
                    proc = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=300,
                        check=False,
                    )
                    if proc.returncode == 0:
                        return _process_result(proc)
                    if proc.returncode == 1 and not (proc.stdout or "").strip():
                        return {"cancelled": True, "path": None, "paths": []}, 200
                    detail = (proc.stderr or "").strip()
                    if detail:
                        return {"error": detail, "paths": []}, 500
                except FileNotFoundError:
                    continue
            return {
                "cancelled": False,
                "path": None,
                "paths": [],
                "message": "No native picker is installed — paste paths instead",
            }, 200

        return {
            "cancelled": False,
            "path": None,
            "paths": [],
            "message": "Native picker not supported on this OS — paste paths instead",
        }, 200
    except subprocess.TimeoutExpired:
        return {"error": "The native picker timed out", "paths": []}, 504
    except Exception as exc:
        return {"error": str(exc), "paths": []}, 500
