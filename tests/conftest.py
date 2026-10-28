"""Shared test isolation."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_user_state(tmp_path: Path, monkeypatch) -> None:
    """Never let a test read or overwrite the user's durable state.

    Covers the review session and the low-resolution keep-decisions store,
    both of which resolve their default location through XDG_STATE_HOME.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
