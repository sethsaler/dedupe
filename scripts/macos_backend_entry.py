"""Entry point for the embedded Python backend in the macOS app."""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path


def _finder_safe_path() -> str:
    """Return a PATH that finds Homebrew/user tools when launched from Finder."""
    existing = os.environ.get("PATH", "")
    candidates = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
        existing,
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    parts: list[str] = []
    for candidate in candidates:
        for part in candidate.split(os.pathsep):
            if part and part not in parts:
                parts.append(part)
    return os.pathsep.join(parts)


def run() -> int:
    os.environ["PATH"] = _finder_safe_path()
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    from dedupe.cli import main

    return main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(run())
