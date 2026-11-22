"""Small, on-demand metadata summaries for the comparison view."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
from functools import lru_cache
from pathlib import Path

from .models import MediaType, classify_media

_HEIF_LOCK = threading.Lock()
_HEIF_REGISTERED = False

_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
_HDR_SIDE_DATA_HINTS = ("dovi", "dolby vision", "mastering display", "content light")


def ensure_heif_registered() -> None:
    """Register the HEIF/HEIC Pillow opener at most once across threads."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    with _HEIF_LOCK:
        if _HEIF_REGISTERED:
            return
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except Exception:
            pass
        _HEIF_REGISTERED = True


def _parse_positive_int(raw: object) -> int | None:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _parse_fps(stream: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not raw or raw == "0/0":
            continue
        text = str(raw)
        try:
            if "/" in text:
                num_str, den_str = text.split("/", 1)
                num, den = float(num_str), float(den_str)
                value = num / den if den > 0 else 0.0
            else:
                value = float(text)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if math.isfinite(value) and value > 0:
            return round(value, 3)
    return None


def _parse_rotation(stream: dict) -> int:
    rotation = 0
    for side in stream.get("side_data_list") or []:
        if isinstance(side, dict) and side.get("rotation") is not None:
            try:
                rotation = round(float(side["rotation"]))
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = round(float((stream.get("tags") or {}).get("rotate", 0)))
        except (TypeError, ValueError):
            rotation = 0
    return rotation % 360


def _is_hdr_stream(stream: dict) -> bool:
    transfer = str(stream.get("color_transfer") or "").strip().lower()
    primaries = str(stream.get("color_primaries") or "").strip().lower()
    if transfer in _HDR_TRANSFERS or primaries == "bt2020":
        return True
    for side in stream.get("side_data_list") or []:
        if not isinstance(side, dict):
            continue
        kind = str(side.get("side_data_type") or "").lower()
        if any(hint in kind for hint in _HDR_SIDE_DATA_HINTS):
            return True
    return False


def _find_tag_ci(tags: object, name: str) -> str | None:
    if not isinstance(tags, dict):
        return None
    target = name.lower()
    for key, value in tags.items():
        if str(key).lower() == target and value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _probe_video_details(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is not available")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            (
                "stream=codec_type,codec_name,channels,width,height,bit_rate,"
                "r_frame_rate,avg_frame_rate,pix_fmt,color_transfer,"
                "color_primaries,color_space"
                ":stream_tags=rotate,creation_time"
                ":stream_side_data=rotation,side_data_type"
                ":format=duration,bit_rate:format_tags=creation_time"
            ),
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    vstream = video or {}
    duration = fmt.get("duration")
    if duration is None:
        duration = vstream.get("duration")
    creation_time = (
        _find_tag_ci(fmt.get("tags"), "creation_time")
        or _find_tag_ci(vstream.get("tags"), "creation_time")
    )
    return {
        "audio": audio,
        "duration": duration,
        "video_codec": vstream.get("codec_name") or None,
        "bit_rate": _parse_positive_int(vstream.get("bit_rate") or fmt.get("bit_rate")),
        "fps": _parse_fps(vstream) if video else None,
        "pix_fmt": vstream.get("pix_fmt") or None,
        "hdr": _is_hdr_stream(vstream) if video else False,
        "rotation": _parse_rotation(vstream) if video else 0,
        "creation_time": creation_time,
    }


@lru_cache(maxsize=512)
def _comparison_details_cached(
    resolved_path: str, size: int, mtime_ns: int
) -> dict:
    del size, mtime_ns
    path = Path(resolved_path)
    media_type = classify_media(path)
    if media_type == MediaType.VIDEO:
        return _probe_video_details(path)
    if media_type not in (MediaType.IMAGE, MediaType.GIF):
        return {"format": None, "capture_date": None, "camera": ""}

    ensure_heif_registered()
    from PIL import Image

    with Image.open(path) as image:
        exif = image.getexif()
        capture = exif.get(36867)
        if not capture and exif.get(34665):
            capture = exif.get_ifd(34665).get(36867)
        return {
            "format": image.format,
            "capture_date": str(capture) if capture else None,
            "camera": " ".join(str(exif.get(tag, "")) for tag in (271, 272)).strip(),
        }


def comparison_details(path: str | Path) -> dict:
    """Return comparison-view metadata summary for ``path`` using a stat-keyed cache."""
    path_obj = Path(path)
    st = path_obj.stat()
    cached = _comparison_details_cached(
        str(path_obj.resolve()), int(st.st_size), int(st.st_mtime_ns)
    )
    copy = dict(cached)
    if "audio" in copy:
        copy["audio"] = [dict(stream) for stream in copy["audio"]]
    return copy


comparison_details.cache_clear = _comparison_details_cached.cache_clear  # type: ignore[attr-defined]
comparison_details.cache_info = _comparison_details_cached.cache_info  # type: ignore[attr-defined]
