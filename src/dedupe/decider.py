"""Look-alike verification with Perplexity's Decider, served by Decider.app.

Decider.app (``ai.perplexity.decider``) runs ``pplx-decider-flash`` — a
Qwen3.5-4B vision model that answers multiple-choice questions in one forward
pass — and serves it on this Mac at ``/v1/decisions``. Dedupe asks it one
question per candidate pair: is the second image a copy of the first?

It is a pairwise judge, not an embedding model, so it cannot find candidates
in a library by itself. EmbeddingGemma (DigUp) still proposes the pairs; the
judge decides the ones OpenCV's geometric check cannot confirm. In
measurements on real photos, crops, grayscale, filters and recompression
scored 0.85-0.99, unrelated photos 0.00, mirrored copies 0.57-0.78, and burst
shots a second or two apart 0.85-0.94: the judge reads near-identical bursts
as the same picture, so they join Similar too (never preselected).

Protocol::

    -> POST /v1/decisions {"model": "...", "state": [content parts],
          "questions": {"copy": {"type": "choice", "criteria": {...}}}}
    <- {"answers": {"copy": {"choice": "same", "probabilities": {...}}}}
"""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Self

from .cache import HashCache, default_cache_path
from .models import FileRecord

URL_ENV = "DEDUPE_DECIDER_URL"
DEFAULT_URL = "http://127.0.0.1:8080"
# P(same picture) a pair needs to join Similar.
DEFAULT_MIN_PROBABILITY = 0.8
# Longest side sent to the model. Larger images cost more vision tokens
# without separating edits from other photos any better.
PREPARE_MAX_SIDE = 768
PROMPT_VERSION = "q1"
HEALTH_TIMEOUT = 2.0
REQUEST_TIMEOUT = 120.0
CRITERIA = {
    "same": (
        "The same photograph: one image is a copy of the other, possibly cropped, "
        "mirrored, resized, recompressed, or color/filter edited"
    ),
    "burst": (
        "The same scene or subject, but a separately taken photo "
        "(different moment, pose, or angle)"
    ),
    "different": "Different scenes or subjects",
}


def decider_url() -> str:
    return (os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")


def _get_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def decider_available(url: str | None = None) -> bool:
    """True when Decider.app is serving an image-capable model."""
    try:
        health = _get_json(f"{url or decider_url()}/health", HEALTH_TIMEOUT)
    except (OSError, ValueError):
        return False
    return health.get("status") == "ready" and bool(health.get("images"))


def _model_id(url: str) -> str:
    """The pinned model id (``pplx-decider-flash-v1.2``), not a ``latest`` alias."""
    listing = _get_json(f"{url}/v1/models", HEALTH_TIMEOUT)
    ids = [str(item.get("id", "")) for item in listing.get("data") or []]
    pinned = [i for i in ids if "/" not in i and "latest" not in i]
    if not (pinned or ids):
        raise RuntimeError("Decider lists no models")
    return (pinned or ids)[0]


def image_part(path: str) -> dict:
    """An upright first frame as a JPEG data-URL content part."""
    from PIL import Image, ImageOps

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass
    with Image.open(path) as img:
        img.seek(0)
        frame = ImageOps.exif_transpose(img).convert("RGB")
    frame.thumbnail((PREPARE_MAX_SIDE, PREPARE_MAX_SIDE))
    buffer = io.BytesIO()
    frame.save(buffer, "JPEG", quality=88)
    data = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


class VerdictCache:
    """P(same picture) by pair identity and model signature, in the hash cache file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.DatabaseError:
            pass
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decider_verdicts (
                pair TEXT PRIMARY KEY,
                identity TEXT NOT NULL,
                signature TEXT NOT NULL,
                probability REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def _key(a: FileRecord, b: FileRecord) -> tuple[str, str]:
        a, b = (a, b) if a.path < b.path else (b, a)
        return f"{a.path}\0{b.path}", f"{HashCache._identity(a)}\0{HashCache._identity(b)}"

    def get(self, a: FileRecord, b: FileRecord, signature: str) -> float | None:
        pair, identity = self._key(a, b)
        row = self._conn.execute(
            "SELECT identity, signature, probability FROM decider_verdicts WHERE pair = ?",
            (pair,),
        ).fetchone()
        if row and row[0] == identity and row[1] == signature:
            return float(row[2])
        return None

    def put(self, a: FileRecord, b: FileRecord, signature: str, probability: float) -> None:
        pair, identity = self._key(a, b)
        self._conn.execute(
            "INSERT OR REPLACE INTO decider_verdicts (pair, identity, signature, probability) "
            "VALUES (?, ?, ?, ?)",
            (pair, identity, signature, probability),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class DeciderJudge:
    """Asks Decider whether two images are one picture; closes its cache on exit.

    Calling the judge returns P(same picture), or None when the pair could not
    be judged. After the server stops answering, every later call returns None
    and ``failure`` says why, so a scan finishes on the fallback checks.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        use_cache: bool = True,
        cache_path: str | Path | None = None,
    ) -> None:
        self.url = url or decider_url()
        if not decider_available(self.url):
            raise RuntimeError(f"Decider is not running at {self.url}; open Decider.app")
        self.model = _model_id(self.url)
        self.signature = f"{self.model}|{PROMPT_VERSION}|s{PREPARE_MAX_SIDE}"
        self.cache = VerdictCache(cache_path) if use_cache else None
        self.failure: str | None = None
        self.judged = 0

    def _ask(self, left: dict, right: dict) -> float:
        body = {
            "model": self.model,
            "state": [
                {"type": "text", "text": "Image 1:"},
                left,
                {"type": "text", "text": "Image 2:"},
                right,
            ],
            "questions": {"copy": {"type": "choice", "criteria": CRITERIA}},
        }
        request = urllib.request.Request(
            f"{self.url}/v1/decisions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                reply = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                message = json.load(exc)["error"]["message"]
            except Exception:
                message = str(exc)
            raise RuntimeError(f"Decider: {message}") from exc
        return float(reply["answers"]["copy"]["probabilities"]["same"])

    def __call__(self, a: FileRecord, b: FileRecord) -> float | None:
        if self.failure is not None:
            return None
        if self.cache is not None:
            cached = self.cache.get(a, b, self.signature)
            if cached is not None:
                return cached
        try:
            left, right = image_part(a.path), image_part(b.path)
        except Exception:
            return None  # unreadable media: this pair only
        try:
            probability = self._ask(left, right)
        except Exception as exc:
            self.failure = f"Decider verification stopped: {exc}"
            return None
        self.judged += 1
        if self.cache is not None:
            self.cache.put(a, b, self.signature, probability)
        return probability

    def close(self) -> None:
        if self.cache is not None:
            self.cache.close()
            self.cache = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
