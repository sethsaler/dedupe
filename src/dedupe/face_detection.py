"""Count faces in images, GIFs, and videos with the bundled OpenCV YuNet model.

Detected faces are also classified with the bundled InsightFace genderage
model so files can be filtered by male/female face counts.
"""

from __future__ import annotations

import math
import subprocess
import tempfile
import threading
from collections.abc import Callable
from io import BytesIO
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
from .people_score import call_with_cancelled, verify_model_sha256
from .similar_video import (
    _extract_frames,
    _extract_seek_frame_ppm,
    _extract_seek_frames_ppm,
    _sample_timestamps,
    ffmpeg_available,
    probe_video,
)

ProgressCb = Callable[[str, int, int], None]

# v4: fix second-pass max-side check and derive gender counts from busiest frame.
FACE_COUNT_CACHE_VERSION = "face-count-v4"
# Face counting has no early exit (the count is the busiest sampled frame), so
# videos get a bounded sample of frames rather than a full decode.
FACE_MEDIA_TYPES = (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO)
FACE_VIDEO_MAX_FRAMES = 16
FACE_VIDEO_MIN_FRAMES = 4
# One sampled frame per ~5s of video, clamped to the 4–16 range above.
FACE_VIDEO_SECONDS_PER_FRAME = 5
# Sample video frames wide enough for YuNet's full-size detection pass.
FACE_VIDEO_FRAME_WIDTH = DETECT_MAX_SIDE

GENDERAGE_MODEL_PATH = (
    Path(__file__).parent / "assets" / "genderage_buffalo_l.onnx"
)
GENDERAGE_MODEL_SHA256 = (
    "4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb"
)
GENDERAGE_INPUT_SIZE = 96
# InsightFace loose crop: a square 1.5x the face box, centered on the face.
GENDERAGE_CROP_SCALE = 1.5


def face_detection_signature() -> str:
    """Identify counter inputs that must match before a cached count is reused."""
    return "|".join(
        (
            FACE_COUNT_CACHE_VERSION,
            "opencv_yunet",
            f"yunet={YUNET_MODEL_SHA256[:12]}",
            f"genderage={GENDERAGE_MODEL_SHA256[:12]}",
            f"face-confidence={YUNET_SCORE_THRESHOLD:g}",
        )
    )


class _YuNetFaceCounter:
    """CPU-only face counter backed by the bundled YuNet and genderage models."""

    backend = "opencv_yunet"

    def __init__(self, load_genderage: bool = True) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "face counting requires OpenCV; install with `pip install -e '.[human]'`"
            ) from exc

        if hasattr(cv2, "setNumThreads"):
            cv2.setNumThreads(1)
        self.cv2 = cv2
        verify_model_sha256(
            YUNET_MODEL_PATH,
            YUNET_MODEL_SHA256,
            missing_error=(
                "the bundled OpenCV YuNet face model is missing; "
                "refusing to count faces without it"
            ),
            integrity_error=(
                "the bundled OpenCV YuNet face model failed its integrity check; "
                "refusing to count faces"
            ),
        )
        if load_genderage:
            verify_model_sha256(
                GENDERAGE_MODEL_PATH,
                GENDERAGE_MODEL_SHA256,
                missing_error=(
                    "the bundled InsightFace genderage model is missing; "
                    "refusing to count faces without it"
                ),
                integrity_error=(
                    "the bundled InsightFace genderage model failed its integrity check; "
                    "refusing to count faces"
                ),
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
            self.gender = (
                cv2.dnn.readNetFromONNX(str(GENDERAGE_MODEL_PATH))
                if load_genderage
                else None
            )
        except (AttributeError, cv2.error) as exc:
            raise RuntimeError(
                "OpenCV YuNet face detection could not start; "
                "install OpenCV 4.5.4 or newer"
            ) from exc

    def _detect_best(self, bgr_frame):
        """Detect faces at both scales; keep the busiest pass.

        Small faces resolve better at full size, close-up faces at the
        smaller pass — the same two scales the person detector uses.
        Returns the faces and the (possibly resized) image they came from.
        """
        best_faces = None
        best_candidate = bgr_frame
        height, width = bgr_frame.shape[:2]
        # The 480px pass exists for close-up faces. When the long side is
        # already <=480, that pass does not downscale and repeats the same size.
        skip_second_pass = max(width, height) <= YUNET_SECOND_PASS_MAX_SIDE
        checked_sizes: set[tuple[int, int]] = set()
        for max_side in (DETECT_MAX_SIDE, YUNET_SECOND_PASS_MAX_SIDE):
            if max_side == YUNET_SECOND_PASS_MAX_SIDE and skip_second_pass:
                continue
            height, width = bgr_frame.shape[:2]
            longest = max(width, height)
            if longest > max_side:
                scale = max_side / longest
                candidate = self.cv2.resize(
                    bgr_frame,
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    interpolation=self.cv2.INTER_AREA,
                )
            else:
                candidate = bgr_frame
            candidate_height, candidate_width = candidate.shape[:2]
            size = (candidate_width, candidate_height)
            if size in checked_sizes:
                continue
            checked_sizes.add(size)
            self.face.setInputSize(size)
            _result, faces = self.face.detect(candidate)
            if faces is not None and (best_faces is None or len(faces) > len(best_faces)):
                best_faces = faces
                best_candidate = candidate
        return best_faces, best_candidate

    def _classify_faces(self, bgr_image, faces) -> tuple[int, int]:
        """Classify each detected face; return (male_count, female_count)."""
        if getattr(self, "gender", None) is None:
            return (0, 0)
        import numpy as np

        males = 0
        females = 0
        size = GENDERAGE_INPUT_SIZE
        for face in faces:
            x, y, face_width, face_height = (float(value) for value in face[:4])
            if (
                not (math.isfinite(face_width) and math.isfinite(face_height))
                or face_width <= 0.0
                or face_height <= 0.0
            ):
                continue
            center_x = x + face_width / 2
            center_y = y + face_height / 2
            scale = size / (max(face_width, face_height) * GENDERAGE_CROP_SCALE)
            matrix = np.array(
                [
                    [scale, 0, size / 2 - center_x * scale],
                    [0, scale, size / 2 - center_y * scale],
                ],
                dtype=np.float32,
            )
            crop = self.cv2.warpAffine(
                bgr_image, matrix, (size, size), borderValue=0
            )
            # genderage normalizes pixels inside its graph (_minusscalar0/
            # _mulscalar0); normalizing again collapses every input toward
            # ~60% female, so the crop goes in as raw pixels.
            blob = self.cv2.dnn.blobFromImage(
                crop, scalefactor=1.0, size=(size, size), swapRB=True
            )
            self.gender.setInput(blob)
            logits = self.gender.forward().reshape(-1)  # [female, male, age/100]
            if logits[1] > logits[0]:
                males += 1
            else:
                females += 1
        return males, females

    def analyze_frame(self, rgb_frame) -> tuple[int, int, int]:
        """Return (total, male, female) face counts for one frame."""
        frame = self.cv2.cvtColor(rgb_frame, self.cv2.COLOR_RGB2BGR)
        faces, candidate = self._detect_best(frame)
        if faces is None or len(faces) == 0:
            return (0, 0, 0)
        males, females = self._classify_faces(candidate, faces)
        return (len(faces), males, females)


def _face_video_max_frames(duration: float | None) -> int:
    """Scale the video sample budget with duration, clamped to 4–16 frames."""
    if duration is None or duration <= 0:
        return FACE_VIDEO_MAX_FRAMES
    return min(
        FACE_VIDEO_MAX_FRAMES,
        max(
            FACE_VIDEO_MIN_FRAMES,
            math.ceil(duration / FACE_VIDEO_SECONDS_PER_FRAME),
        ),
    )


def _video_face_frames(
    record: FileRecord,
    counter: _YuNetFaceCounter,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> list[tuple[int, int, int]]:
    """Analyze faces across sampled video frames.

    Person detection can stop at the first positive frame; face counting needs
    the busiest frame, so every sampled frame must decode successfully.
    """
    import numpy as np
    from PIL import Image

    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    if not ffmpeg_available():
        raise RuntimeError("face counting in videos requires ffmpeg on PATH")

    duration = record.duration
    if duration is None or duration <= 0:
        duration, width, height = call_with_cancelled(
            probe_video, record.path, cancelled=cancelled
        )
        record.duration = duration
        record.width = width or record.width
        record.height = height or record.height

    frames: list[tuple[int, int, int]] = []

    def analyze_ppm(ppm: bytes) -> None:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        with Image.open(BytesIO(ppm)) as image:
            frames.append(counter.analyze_frame(np.asarray(image.convert("RGB"))))

    if duration is not None and duration > 0:
        timestamps = _sample_timestamps(duration, _face_video_max_frames(duration))
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        # One ffmpeg process for all sampled frames instead of one per frame.
        try:
            ppms = call_with_cancelled(
                _extract_seek_frames_ppm,
                record.path,
                timestamps,
                frame_width=FACE_VIDEO_FRAME_WIDTH,
                cancelled=cancelled,
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            ppms = []
        if len(ppms) != len(timestamps):
            # Per-frame fallback for inputs the batched pass cannot satisfy.
            # Face counting fails closed on any missing/timed-out frame, so the
            # first timeout/failure aborts immediately instead of looping 16x.
            ppms = []
            for timestamp in timestamps:
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                try:
                    ppm = call_with_cancelled(
                        _extract_seek_frame_ppm,
                        record.path,
                        timestamp,
                        frame_width=FACE_VIDEO_FRAME_WIDTH,
                        cancelled=cancelled,
                    )
                except (subprocess.TimeoutExpired, TimeoutError) as exc:
                    raise RuntimeError(f"frame decode timed out at {timestamp:.2f}s") from exc
                if ppm is None:
                    raise RuntimeError(f"frame decode failed at {timestamp:.2f}s")
                ppms.append(ppm)
        for ppm in ppms:
            analyze_ppm(ppm)
        return frames

    # Rare fallback for containers without a probeable duration.
    with tempfile.TemporaryDirectory(prefix="dedupe-face-video-") as tmp:
        frame_paths = call_with_cancelled(
            _extract_frames,
            record.path,
            Path(tmp),
            max_frames=FACE_VIDEO_MAX_FRAMES,
            frame_width=FACE_VIDEO_FRAME_WIDTH,
            require_complete=True,
            cancelled=cancelled,
        )
        if not frame_paths:
            raise RuntimeError("no video frames could be sampled")
        for frame_path in frame_paths:
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            with Image.open(frame_path) as image:
                frames.append(
                    counter.analyze_frame(np.asarray(image.convert("RGB")))
                )
    return frames


def analyze_face_count(
    record: FileRecord,
    counter: _YuNetFaceCounter,
    *,
    cache_signature: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> int | None:
    """Count and classify faces for one record, update its fields, return the count."""
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    # A re-analysis must never keep a stale trusted count if decoding fails.
    record.face_count = None
    record.male_face_count = None
    record.female_face_count = None
    record.face_detection_signature = None
    record.face_detector = counter.backend
    if record.media_type not in FACE_MEDIA_TYPES:
        return None
    try:
        if record.media_type == MediaType.VIDEO:
            frames = _video_face_frames(record, counter, cancelled=cancelled)
        else:
            frames = []
            for frame in _pil_frames(Path(record.path)):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                frames.append(counter.analyze_frame(frame))
    except InterruptedError:
        raise
    except Exception as exc:
        record.error = f"face counting failed: {exc}"
        return None
    if not frames:
        record.error = "face counting failed: no frames decoded"
        return None
    best_total, best_males, best_females = max(
        frames, key=lambda frame: (frame[0], frame[2], frame[1])
    )
    record.face_count = best_total
    record.male_face_count = best_males
    record.female_face_count = best_females
    record.face_detection_signature = cache_signature
    return record.face_count


def count_faces_in_files(
    records: list[FileRecord],
    *,
    workers: int | None = None,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[FileRecord]:
    """Count faces in every image, GIF, and video, reusing trusted cached counts."""
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
            analyze_face_count(
                record, counter, cache_signature=signature, cancelled=cancelled
            )

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
            analyze_face_count(
                record, counter, cache_signature=signature, cancelled=cancelled
            )
            if progress:
                progress("face-detection", index, len(candidates))

    return candidates


def protect_no_person_candidates(
    records: list[FileRecord],
    *,
    workers: int | None = None,
    progress: Callable | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Keep any file whose YuNet pass finds a face, especially a female face.

    Used as a second opinion on Photon / ensemble no-person decisions. A
    counted face overrides ``no_person_detected`` so the file never enters
    Non-Human review. Missing OpenCV or a model failure leaves the original
    verdict in place (fail closed for this extra check only).
    """
    pending = [
        record
        for record in records
        if record.human_detection_status == "no_person_detected"
    ]
    if not pending:
        return
    try:
        count_faces_in_files(
            pending,
            workers=workers,
            progress=progress,
            cancelled=cancelled,
        )
    except RuntimeError:
        return
    for record in pending:
        if (record.female_face_count or 0) >= 1 or (record.face_count or 0) >= 1:
            record.human_detection_status = "person_detected"
