"""Keep the local verification entrypoint fail-closed and aligned with CI."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify.py"
SPEC = importlib.util.spec_from_file_location("verify", SCRIPT)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


@pytest.mark.parametrize("suite", ["lint", "unit", "browser", "package", "macos"])
def test_selects_requested_suite(suite, monkeypatch):
    calls = []
    for name in ("lint", "unit", "browser", "package", "macos_bundle"):
        monkeypatch.setattr(verify, name, lambda name=name: calls.append(name))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), suite])
    verify.main()
    assert calls == ["macos_bundle" if suite == "macos" else suite]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_full_checks_prerequisites_before_all_suites(platform, monkeypatch):
    calls = []
    for name in ("check_full_prerequisites", "lint", "unit", "browser", "package", "macos_bundle"):
        monkeypatch.setattr(verify, name, lambda name=name: calls.append(name))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(sys, "platform", platform)
    verify.main()
    expected = ["check_full_prerequisites", "lint", "unit", "browser", "package"]
    assert calls == expected + (["macos_bundle"] if platform == "darwin" else [])


def test_missing_full_prerequisite_fails_before_checks(monkeypatch):
    monkeypatch.setattr(verify.shutil, "which", lambda _tool: None)
    with pytest.raises(SystemExit, match="ffmpeg, ffprobe"):
        verify.check_full_prerequisites()


def test_unit_and_browser_markers_are_explicit(monkeypatch):
    calls = []
    monkeypatch.setattr(verify, "run", lambda *args: calls.append(args))
    verify.unit()
    verify.browser()
    assert calls == [
        (sys.executable, "-m", "pytest", "-q", "-m", "not e2e"),
        (sys.executable, "-m", "pytest", "-q", "-m", "e2e"),
    ]


def test_run_propagates_a_failed_check(monkeypatch):
    def fail(*args, **kwargs):
        assert kwargs["check"] is True
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(verify.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        verify.run("failing-check")


def test_package_uses_fresh_wheel_and_isolated_smoke_test(monkeypatch):
    calls = []
    monkeypatch.setenv("PYTHONPATH", "/must/not/import/the/checkout")

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        if "--outdir" in args:
            dist = Path(args[-1])
            dist.mkdir()
            (dist / "dedupe_media-0.1.0-py3-none-any.whl").touch()

    monkeypatch.setattr(verify, "run", fake_run)
    verify.package()
    assert len(calls) == 6
    for _args, kwargs in calls[2:]:
        assert "PYTHONPATH" not in kwargs["env"]
        assert kwargs["cwd"] != verify.ROOT
    assert calls[-2][0][-1] == "--help"
    assert calls[-1][0][-2:] == ("doctor", "--json")
    assert not calls[-1][1]["cwd"].exists()
