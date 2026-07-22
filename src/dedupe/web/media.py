"""Thumbnail generation and media type helpers for the web UI."""

from __future__ import annotations

import mimetypes
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".m2ts",
    ".wmv", ".flv", ".3gp",
}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def media_mimetype(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def image_thumbnail_bytes(path: Path, *, full: bool = False) -> bytes:
    from PIL import Image

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass

    max_edge = 1600 if full else 320
    quality = 88 if full else 80
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge))
        output = BytesIO()
        img.save(output, format="JPEG", quality=quality)
        return output.getvalue()


def video_thumbnail_bytes(path: Path) -> bytes | None:
    if not shutil.which("ffmpeg"):
        return None
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        output_path = Path(tmp.name)
    try:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i",
                str(path), "-frames:v", "1", "-vf", "scale=320:-1", "-y",
                str(output_path),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        data = output_path.read_bytes()
        return data if data else None
    except Exception:
        return None
    finally:
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
