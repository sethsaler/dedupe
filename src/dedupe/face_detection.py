"""Count faces in images and GIFs with the bundled OpenCV YuNet model."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from pathlib import Path

from .human_detection import (
    DETECT_MAX_SIDE,
    YUNET_MODEL_PATH,
    YUNET_MODEL_SHA256,
    YUNET_NMS_THRESHOLD,
    YUNET_SCORE_THRESHOLD,
    YUNET_SECOND_PASS_MAX_SIDE,
    YUNET_TOP_K,
    _pil_frames,
)
from .models import FileRecord, MediaType
from .parallel import DEFAULT_HUMAN_WORKERS_CAP, map_parallel, resolve_workers

ProgressCb = Callable[[str, int, int], None]

FACE_COUNT_CACHE_VERSION = "face-count-v1"
# Videos are excluded on purpose: counting faces needs every sampled frame
# decoded (no early exit), which is disproportionately expensive for video.
FACE_MEDIA_TYPES = (MediaType.IMAGE, MediaType.GIF)


def face_detection_signature() -> str:
    """Identify counter inputs that must match before a cached count is reused."""
    return "|".join(
        (
            FACE_COUNT_CACHE_VERSION,
            "opencv_yunet",
            f"yunet={YUNET_MODEL_SHA256[:12]}",
            f"face-confidence={YUNET_SCORE_THRESHOLD:g}",
        )
    )


class _YuNetFaceCounter:
    """CPU-only face counter backed by the bundled YuNet model."""

    backend = "opencv_yunet"

    def __init__(self) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "face counting requires OpenCV; install with `pip install -e '.[human]'`"
            ) from exc

        self.cv2 = cv2
        if not YUNET_MODEL_PATH.is_file():
            raise RuntimeError(
                "the bundled OpenCV YuNet face model is missing; "
                "refusing to count faces without it"
            )
        with YUNET_MODEL_PATH.open("rb") as model_file:
            model_sha256 = hashlib.file_digest(model_file, "sha256").hexdigest()
        if model_sha256 != YUNET_MODEL_SHA256:
            raise RuntimeError(
                "the bundled OpenCV YuNet face model failed its integrity check; "
                "refusing to count faces"
            )
        try:
            self.face = cv2.FaceDetectorYN.create(
                str(YUNET_MODEL_PATH),
                "",
                (320, 320),
                YUNET_SCORE_THRESHOLD,
                YUNET_NMS_THRESHOLD,
                YUNET_TOP_K,
            )
        except (AttributeError, cv2.error) as exc:
            raise RuntimeError(
                "OpenCV YuNet face detection could not start; "
                "install OpenCV 4.5.4 or newer"
            ) from exc

    def count(self, rgb_frame) -> int:
        """Return the largest face count seen across the two detection scales."""
        # Small faces resolve better at full size, close-up faces at the
        # smaller pass — the same two scales the person detector uses.
        frame = self.cv2.cvtColor(rgb_frame, self.cv2.COLOR_RGB2BGR)
        best = 0
        checked_sizes: set[tuple[int, int]] = set()
        for max_side in (DETECT_MAX_SIDE, YUNET_SECOND_PASS_MAX_SIDE):
            height, width = frame.shape[:2]
            longest = max(width, height)
            if longest > max_side:
                scale = max_side / longest
                candidate = self.cv2.resize(
                    frame,
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    interpolation=self.cv2.INTER_AREA,
                )
            else:
                candidate = frame
            candidate_height, candidate_width = candidate.shape[:2]
            size = (candidate_width, candidate_height)
            if size in checked_sizes:
                continue
            checked_sizes.add(size)
            self.face.setInputSize(size)
            _result, faces = self.face.detect(candidate)
            if faces is not None:
                best = max(best, len(faces))
        return best


def analyze_face_count(
    record: FileRecord,
    counter: _YuNetFaceCounter,
    *,
    cache_signature: str | None = None,
) -> int | None:
    """Count faces for one record, update its fields, and return the count."""
    # A re-analysis must never keep a stale trusted count if decoding fails.
    record.face_count = None
    record.face_detection_signature = None
    record.face_detector = counter.backend
    if record.media_type not in FACE_MEDIA_TYPES:
        return None
    try:
        counts = [counter.count(frame) for frame in _pil_frames(Path(record.path))]
    except Exception as exc:
        record.error = f"face counting failed: {exc}"
        return None
    if not counts:
        record.error = "face counting failed: no frames decoded"
        return None
    record.face_count = max(counts)
    record.face_detection_signature = cache_signature
    return record.face_count


def count_faces_in_files(
    records: list[FileRecord],
    *,
    workers: int | None = None,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[FileRecord]:
    """Count faces in every image/GIF, reusing trusted cached counts."""
    candidates = [
        record for record in records if record.media_type in FACE_MEDIA_TYPES
    ]
    if not candidates:
        return []

    signature = face_detection_signature()

    def has_cached_count(record: FileRecord) -> bool:
        return (
            record.face_count is not None
            and record.face_detection_signature == signature
        )

    cached = [record for record in candidates if has_cached_count(record)]
    pending = [record for record in candidates if not has_cached_count(record)]

    if progress and cached:
        progress("face-detection", len(cached), len(candidates))

    face_workers = resolve_workers(workers, cap=DEFAULT_HUMAN_WORKERS_CAP)

    if pending and face_workers > 1 and len(pending) > 1:
        # YuNet mutates its input size and cannot be shared across threads.
        local = threading.local()

        def analyze(record: FileRecord) -> None:
            counter = getattr(local, "counter", None)
            if counter is None:
                counter = _YuNetFaceCounter()
                local.counter = counter
            analyze_face_count(record, counter, cache_signature=signature)

        def parallel_progress(done: int, _total: int) -> None:
            if progress:
                progress("face-detection", len(cached) + done, len(candidates))

        map_parallel(
            analyze,
            pending,
            workers=face_workers,
            backend="thread",
            progress=parallel_progress,
            progress_every=1,
            cancelled=cancelled,
        )
    elif pending:
        counter = _YuNetFaceCounter()
        for index, record in enumerate(pending, start=len(cached) + 1):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            analyze_face_count(record, counter, cache_signature=signature)
            if progress:
                progress("face-detection", index, len(candidates))

    return candidates
