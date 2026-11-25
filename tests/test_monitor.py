"""Event filtering for monitored cleanup jobs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from dedupe.monitor import _JobEventHandler


def _handler(root: Path, **overrides) -> _JobEventHandler:
    options = {
        "include_images": True,
        "include_gifs": True,
        "include_videos": True,
        "include_hidden": False,
        "exclusions": [],
    }
    options.update(overrides)
    return _JobEventHandler(root, lambda: None, **options)


def _event(path: Path, *, directory: bool = False, dest_path: str = ""):
    return SimpleNamespace(
        src_path=str(path),
        dest_path=dest_path,
        is_directory=directory,
    )


def test_media_events_are_relevant_but_noise_is_filtered(tmp_path: Path) -> None:
    root = tmp_path / "media"
    handler = _handler(root, exclusions=["ignored*"])

    assert handler.relevant(_event(root / "inbox" / "photo.jpg"))
    assert handler.relevant(_event(root / "inbox", directory=True))
    assert handler.relevant(_event(root / "old.txt", dest_path=str(root / "renamed.mov")))
    assert not handler.relevant(_event(root / "notes.txt"))
    assert not handler.relevant(_event(root / ".hidden" / "photo.jpg"))
    assert not handler.relevant(_event(root / "ignored-photo.jpg"))


def test_media_type_toggles_filter_events_like_scan_inventory(tmp_path: Path) -> None:
    root = tmp_path / "media"
    handler = _handler(root, include_images=False, include_gifs=False)

    assert not handler.relevant(_event(root / "photo.jpg"))
    assert not handler.relevant(_event(root / "animation.gif"))
    assert handler.relevant(_event(root / "clip.mp4"))
