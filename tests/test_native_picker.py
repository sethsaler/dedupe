"""Native picker payload handling, with the OS dialogs mocked out."""

from __future__ import annotations

import subprocess
from pathlib import Path

from dedupe.web import native_picker


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_macos_picker_returns_resolved_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(native_picker.platform, "system", lambda: "Darwin")
    target = tmp_path / "media"
    target.mkdir()
    proc = _FakeProc(stdout=f"{target}\n")

    def fake_run(command, **kwargs):
        assert command[0] == "/usr/bin/osascript"
        assert "choose folder" in command[2]
        return proc

    monkeypatch.setattr(native_picker.subprocess, "run", fake_run)

    payload, status = native_picker.pick_native_paths("folder")
    assert status == 200
    assert payload["paths"] == [str(target)]
    assert payload["cancelled"] is False


def test_macos_picker_cancel_is_not_an_error(monkeypatch) -> None:
    monkeypatch.setattr(native_picker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        native_picker.subprocess, "run", lambda *a, **k: _FakeProc(stdout="")
    )

    payload, status = native_picker.pick_native_paths("files")
    assert status == 200
    assert payload == {"cancelled": True, "path": None, "paths": []}


def test_linux_picker_falls_back_through_zenity_and_kdialog(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(native_picker.platform, "system", lambda: "Linux")
    target = tmp_path / "media"
    target.mkdir()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[0])
        if command[0] == "zenity":
            raise FileNotFoundError  # not installed
        return _FakeProc(stdout=f"{target}\n")

    monkeypatch.setattr(native_picker.subprocess, "run", fake_run)

    payload, status = native_picker.pick_native_paths("folder")
    assert status == 200
    assert payload["paths"] == [str(target)]
    assert calls == ["zenity", "kdialog"]


def test_linux_picker_reports_when_no_picker_is_installed(monkeypatch) -> None:
    monkeypatch.setattr(native_picker.platform, "system", lambda: "Linux")

    def fake_run(command, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(native_picker.subprocess, "run", fake_run)

    payload, status = native_picker.pick_native_paths("folder")
    assert status == 200
    assert payload["paths"] == []
    assert "paste paths instead" in payload["message"]


def test_picker_timeout_is_a_clean_504(monkeypatch) -> None:
    monkeypatch.setattr(native_picker.platform, "system", lambda: "Darwin")

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=300)

    monkeypatch.setattr(native_picker.subprocess, "run", fake_run)

    payload, status = native_picker.pick_native_paths("folder")
    assert status == 504
    assert "timed out" in payload["error"]


def test_picker_process_error_surfaces_stderr(monkeypatch) -> None:
    monkeypatch.setattr(native_picker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        native_picker.subprocess,
        "run",
        lambda *a, **k: _FakeProc(returncode=1, stderr="osascript exploded"),
    )

    payload, status = native_picker.pick_native_paths("folder")
    assert status == 500
    assert payload["error"] == "osascript exploded"
