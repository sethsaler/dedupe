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
    monkeypatch.setattr("dedupe.receipts.default_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr("dedupe.cache.default_cache_path", lambda: tmp_path / "hashes.sqlite3")
    monkeypatch.setattr("dedupe.semantic.default_cache_path", lambda: tmp_path / "hashes.sqlite3")
    monkeypatch.setattr("dedupe.decider.default_cache_path", lambda: tmp_path / "hashes.sqlite3")
