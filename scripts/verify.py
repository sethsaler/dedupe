#!/usr/bin/env python3
"""Run CI checks locally without publishing artifacts or signing releases."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True)


def check_full_prerequisites() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    missing += [
        module
        for module in ("pytest", "ruff", "build", "cv2", "playwright")
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        raise SystemExit(
            "Full verification requires: "
            + ", ".join(missing)
            + ". See docs/verification.md for installation instructions."
        )
    # Check the browser before starting the suite, rather than silently skipping it.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        browser.close()


def lint() -> None:
    run(
        sys.executable,
        "-m",
        "ruff",
        "check",
        "src",
        "tests",
        "scripts/verify.py",
        "scripts/ci_changes.py",
    )


def unit() -> None:
    run(sys.executable, "-m", "pytest", "-q", "-m", "not e2e")


def browser() -> None:
    run(sys.executable, "-m", "pytest", "-q", "-m", "e2e")


def package() -> None:
    # A fresh output directory avoids accidentally validating an old wheel.
    with tempfile.TemporaryDirectory(prefix="dedupe-verify-") as temporary:
        work = Path(temporary)
        distributions = work / "dist"
        run(sys.executable, "-m", "build", "--outdir", str(distributions))
        wheels = list(distributions.glob("*.whl"))
        if len(wheels) != 1:
            raise SystemExit(f"Expected one freshly built wheel, found {len(wheels)}")
        venv = work / "venv"
        run(sys.executable, "-m", "venv", str(venv))
        executables = venv / ("Scripts" if os.name == "nt" else "bin")
        python = str(executables / ("python.exe" if os.name == "nt" else "python"))
        cli = str(executables / ("dedupe.exe" if os.name == "nt" else "dedupe"))
        # Ensure the smoke test imports the installed wheel, never the checkout.
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        home = work / "home"
        home.mkdir()
        env["HOME"] = str(home)
        env["XDG_CACHE_HOME"] = str(work / "cache")
        env["XDG_STATE_HOME"] = str(work / "state")
        run(python, "-m", "pip", "install", str(wheels[0]), cwd=work, env=env)
        run(python, "-m", "pip", "check", cwd=work, env=env)
        run(cli, "--help", cwd=work, env=env)
        run(cli, "doctor", "--json", cwd=work, env=env)


def macos_bundle() -> None:
    with tempfile.TemporaryDirectory(prefix="dedupe-macos-verify-") as temporary:
        run("bash", "scripts/build-macos-app.sh", "--output", str(Path(temporary) / "Dedupe.app"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "suite",
        nargs="?",
        default="full",
        choices=("full", "lint", "unit", "browser", "package", "macos"),
    )
    args = parser.parse_args()
    suites = {"lint": lint, "unit": unit, "browser": browser, "package": package}
    if args.suite == "full":
        check_full_prerequisites()
        for check in suites.values():
            check()
        if sys.platform == "darwin":
            macos_bundle()
        else:
            print("macOS-only behavior and app packaging require a separate run on macOS.")
    elif args.suite == "macos":
        macos_bundle()
    else:
        suites[args.suite]()


if __name__ == "__main__":
    main()
