"""Entry point for the embedded Python backend in the macOS app."""

from __future__ import annotations

import multiprocessing
import os
import signal
import sys
import threading
import time
from pathlib import Path

_THREAD_LIMIT_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "MKL_NUM_THREADS",
)


def _configure_thread_limits() -> None:
    """Cap native BLAS/OpenMP pools before worker spawn or numpy/scipy imports."""
    for name in _THREAD_LIMIT_VARS:
        os.environ.setdefault(name, "1")


def _finder_safe_path() -> str:
    """Return a PATH that finds Homebrew/MacPorts/Nix/user tools when launched from Finder."""
    existing = os.environ.get("PATH", "")
    candidates = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/opt/local/bin",
        str(Path.home() / ".local" / "bin"),
        "/run/current-system/sw/bin",
        str(Path.home() / ".nix-profile" / "bin"),
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


def _start_parent_watchdog() -> None:
    """Tear down the backend process group if the native macOS shell exits unexpectedly."""
    raw_pid = os.environ.get("DEDUPE_PARENT_PID", "").strip()
    if not raw_pid:
        return
    try:
        parent_pid = int(raw_pid)
    except ValueError:
        return
    if parent_pid <= 1:
        return

    def _watch() -> None:
        while True:
            time.sleep(2.0)
            if os.getppid() != parent_pid:
                try:
                    os.killpg(os.getpgrp(), signal.SIGTERM)
                except OSError:
                    os._exit(0)
                return

    threading.Thread(target=_watch, name="dedupe-parent-watchdog", daemon=True).start()


def run() -> int:
    _configure_thread_limits()
    os.environ["PATH"] = _finder_safe_path()
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    if os.environ.get("DEDUPE_NATIVE_SHELL") == "1":
        # Own process group so the native shell can cleanly terminate child
        # ffmpeg / ProcessPoolExecutor workers alongside the server.
        try:
            os.setpgrp()
        except OSError:
            pass
        _start_parent_watchdog()
    from dedupe.cli import main

    return main()


if __name__ == "__main__":
    _configure_thread_limits()
    multiprocessing.freeze_support()
    sys.exit(run())
