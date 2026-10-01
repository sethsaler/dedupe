"""Conservative docs-only classification must fail open to running CI."""

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "ci_changes", Path(__file__).resolve().parents[1] / "scripts" / "ci_changes.py"
)
changes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changes)


@pytest.mark.parametrize(
    "paths,required",
    [
        (["README.md", "docs/verification.md"], False),
        (["docs/product-description/ui/scan-setup.md"], False),
        (["src/dedupe/engine.py"], True),
        ([".github/workflows/ci.yml"], True),
        (["pyproject.toml"], True),
        (["tests/fixtures/example.md"], True),
        (["README.md", "src/deleted.py"], True),
        (["docs/unknown.md"], True),
        (["docs/product-description/fixture.png"], True),
        ([], True),
    ],
)
def test_only_known_documentation_is_skipped(paths, required):
    assert changes.checks_required(paths) is required


@pytest.mark.parametrize(
    "event,ref,base,diff,expected",
    [
        ("pull_request", "refs/pull/1/merge", "a" * 40, b"README.md\0", "false"),
        ("push", "refs/heads/main", "a" * 40, b"README.md\0", "false"),
        ("workflow_dispatch", "refs/heads/main", "a" * 40, b"README.md\0", "true"),
        ("push", "refs/tags/v1", "a" * 40, b"README.md\0", "true"),
        ("push", "refs/heads/main", "0" * 40, b"README.md\0", "true"),
        ("pull_request", "refs/pull/1/merge", "", b"README.md\0", "true"),
        ("pull_request", "refs/pull/1/merge", "a" * 40, b"", "true"),
        ("pull_request", "refs/pull/1/merge", "a" * 40, None, "true"),
    ],
)
def test_diff_handling(event, ref, base, diff, expected, monkeypatch, tmp_path):
    output = tmp_path / "output"
    for key, value in {
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REF": ref,
        "BASE_SHA": base,
        "HEAD_SHA": "b" * 40,
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(key, value)

    def fake_diff(args, **kwargs):
        assert "--no-renames" in args
        assert kwargs["check"] is True
        if diff is None:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout=diff)

    monkeypatch.setattr(changes.subprocess, "run", fake_diff)
    changes.main()
    assert output.read_text() == f"required={expected}\n"
