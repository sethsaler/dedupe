"""Small, on-demand metadata summaries for the comparison view."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .models import MediaType, classify_media


def comparison_details(path: Path) -> dict:
    if classify_media(path) == MediaType.VIDEO:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,channels,width,height:format=duration",
             "-of", "json", str(path)], capture_output=True, text=True,
            timeout=15, check=True,
        )
        data = json.loads(result.stdout)
        audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        return {"audio": audio, "duration": data.get("format", {}).get("duration")}
    from PIL import Image

    with Image.open(path) as image:
        exif = image.getexif()
        capture = exif.get(36867)
        if not capture and exif.get(34665):
            capture = exif.get_ifd(34665).get(36867)
        return {"format": image.format, "capture_date": str(capture) if capture else None,
                "camera": " ".join(str(exif.get(tag, "")) for tag in (271, 272)).strip()}
