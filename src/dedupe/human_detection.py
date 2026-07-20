"""Pluggable local person-candidate detection for images, GIFs, and videos."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .human_policy import (
    CACHEABLE_HUMAN_STATUSES,
    HUMAN_DETECTION_CACHE_VERSION,
    MANUALLY_CONFIRMED_HUMAN_STATUS,
)
from .models import FileRecord, MediaType
from .similar_video import _extract_frames, ffmpeg_available

ProgressCb = Callable[[str, int, int], None]

DEFAULT_CONFIDENCE = 0.25
DEFAULT_BACKEND = "opencv"
DEFAULT_PHOTON_MODEL = "moondream3.1-9B-A2B"
HUMAN_BACKENDS = ("opencv", "photon", "ensemble")
DETECT_MAX_SIDE = 960
YUNET_SECOND_PASS_MAX_SIDE = 480
YUNET_SCORE_THRESHOLD = 0.55
YUNET_NMS_THRESHOLD = 0.3
YUNET_TOP_K = 5000
YUNET_MODEL_PATH = (
    Path(__file__).parent / "assets" / "face_detection_yunet_2023mar.onnx"
)
YUNET_MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
HUMAN_VIDEO_MAX_FRAMES = 16
HUMAN_VIDEO_FRAME_WIDTH = 640


class PersonDetector(Protocol):
    """Small interface shared by all local detector backends."""

    backend: str

    def score(self, rgb_frame) -> float: ...

    def close(self) -> None: ...


class _OpenCVPersonDetector:
    """CPU-only detector combining YuNet faces with HOG full-body detection."""

    def __init__(self, confidence: float) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "person-candidate detection requires OpenCV; "
                "install with `pip install -e '.[human]'`"
            ) from exc

        self.cv2 = cv2
        self.confidence = max(0.0, float(confidence))
        if not YUNET_MODEL_PATH.is_file():
            raise RuntimeError(
                "the bundled OpenCV YuNet face model is missing; refusing to "
                "classify files as non-human with full-body detection alone"
            )
        with YUNET_MODEL_PATH.open("rb") as model_file:
            model_sha256 = hashlib.file_digest(model_file, "sha256").hexdigest()
        if model_sha256 != YUNET_MODEL_SHA256:
            raise RuntimeError(
                "the bundled OpenCV YuNet face model failed its integrity check; "
                "refusing to classify files as non-human"
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
                "OpenCV YuNet face detection could not start; install OpenCV 4.5.4 "
                "or newer. No media was classified as non-human."
            ) from exc
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.backend = "opencv_yunet_hog"

    def _face_score(self, rgb_frame) -> float:
        """Run two face scales so both small and close-up faces are conservative hits."""
        best = 0.0
        frame = rgb_frame
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
            _result, faces = self.face.detect(
                self.cv2.cvtColor(candidate, self.cv2.COLOR_RGB2BGR)
            )
            if faces is not None and len(faces):
                best = max(best, max(float(face[-1]) for face in faces))
                if best >= YUNET_SCORE_THRESHOLD:
                    return best
        return best

    def score(self, rgb_frame) -> float:
        import numpy as np

        frame = np.ascontiguousarray(rgb_frame)
        height, width = frame.shape[:2]
        longest = max(width, height)
        if longest > DETECT_MAX_SIDE:
            scale = DETECT_MAX_SIDE / longest
            frame = self.cv2.resize(
                frame,
                (max(1, round(width * scale)), max(1, round(height * scale))),
                interpolation=self.cv2.INTER_AREA,
            )

        face_score = self._face_score(frame)
        if face_score > 0:
            return face_score

        # HOG needs a moderately sized frame and targets upright full-body people.
        if frame.shape[0] < 128 or frame.shape[1] < 64:
            return 0.0
        _boxes, weights = self.hog.detectMultiScale(
            frame,
            winStride=(8, 8),
            padding=(8, 8),
            scale=1.05,
        )
        if weights is None or len(weights) == 0:
            return 0.0
        score = max(float(weight) for weight in weights)
        return score if score >= self.confidence else 0.0

    def close(self) -> None:
        return None


class _PhotonPersonDetector:
    """Open-vocabulary person detection via Moondream's local Photon runtime."""

    def __init__(self, model_name: str) -> None:
        try:
            import moondream as md
        except ImportError as exc:
            raise RuntimeError(
                "Photon detection requires the Moondream SDK; "
                "install with `pip install -e '.[photon]'`"
            ) from exc

        self.model_name = model_name.strip() or DEFAULT_PHOTON_MODEL
        try:
            self.model = md.vl(local=True, model=self.model_name)
        except Exception as exc:
            raise RuntimeError(
                f"could not start Photon model {self.model_name!r}: {exc}"
            ) from exc
        self.backend = f"photon:{self.model_name}"

    def score(self, rgb_frame) -> float:
        from PIL import Image

        image = Image.fromarray(rgb_frame).convert("RGB")
        # Photon detect() currently returns boxes rather than calibrated scores.
        # A face-only crop still counts as human evidence, so query both targets.
        for target in ("person", "face"):
            result = self.model.detect(image, target)
            if isinstance(result, dict) and result.get("objects"):
                return 1.0
        return 0.0

    def close(self) -> None:
        close = getattr(self.model, "close", None)
        if callable(close):
            close()


class _EnsemblePersonDetector:
    """Fast OpenCV positive pass followed by Photon for uncertain frames."""

    def __init__(
        self,
        confidence: float,
        model_name: str,
        *,
        opencv: PersonDetector | None = None,
        photon: PersonDetector | None = None,
    ) -> None:
        self.opencv = opencv or _OpenCVPersonDetector(confidence)
        try:
            self.photon = photon or _PhotonPersonDetector(model_name)
        except Exception:
            self.opencv.close()
            raise
        self.backend = f"ensemble:{self.opencv.backend}+{self.photon.backend}"

    def score(self, rgb_frame) -> float:
        score = self.opencv.score(rgb_frame)
        return score if score > 0 else self.photon.score(rgb_frame)

    def close(self) -> None:
        try:
            self.opencv.close()
        finally:
            self.photon.close()


def create_person_detector(
    backend: str = DEFAULT_BACKEND,
    *,
    confidence: float = DEFAULT_CONFIDENCE,
    photon_model: str = DEFAULT_PHOTON_MODEL,
) -> PersonDetector:
    """Create a detector without loading optional backends until selected."""
    normalized = backend.strip().lower()
    if normalized == "opencv":
        return _OpenCVPersonDetector(confidence)
    if normalized == "photon":
        return _PhotonPersonDetector(photon_model)
    if normalized == "ensemble":
        return _EnsemblePersonDetector(confidence, photon_model)
    choices = ", ".join(HUMAN_BACKENDS)
    raise ValueError(f"unknown human detector backend {backend!r}; choose {choices}")


def human_detection_signature(
    backend: str = DEFAULT_BACKEND,
    *,
    confidence: float = DEFAULT_CONFIDENCE,
    photon_model: str = DEFAULT_PHOTON_MODEL,
) -> str:
    """Identify detector inputs that must match before a result can be reused."""
    normalized = backend.strip().lower()
    parts = [HUMAN_DETECTION_CACHE_VERSION, normalized]
    if normalized in {"opencv", "ensemble"}:
        parts.append(f"confidence={max(0.0, float(confidence)):g}")
        parts.append(f"yunet={YUNET_MODEL_SHA256[:12]}")
        parts.append(f"face-confidence={YUNET_SCORE_THRESHOLD:g}")
    if normalized in {"photon", "ensemble"}:
        parts.append(f"model={photon_model.strip() or DEFAULT_PHOTON_MODEL}")
    return "|".join(parts)


def _create_detector(confidence: float) -> PersonDetector:
    """Backward-compatible factory for the original OpenCV-only path."""
    return create_person_detector("opencv", confidence=confidence)


def _pil_frames(path: Path):
    """Yield RGB arrays for a still image or representative GIF frames."""
    import numpy as np
    from PIL import Image, ImageOps

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass

    with Image.open(path) as image:
        frame_count = int(getattr(image, "n_frames", 1))
        indexes = sorted({0, frame_count // 2, max(0, frame_count - 1)})
        for index in indexes:
            image.seek(index)
            frame = ImageOps.exif_transpose(image) if frame_count == 1 else image
            rgb = frame.convert("RGB")
            rgb.thumbnail((1280, 1280))
            yield np.asarray(rgb)


def _media_person_evidence(
    record: FileRecord, detector: PersonDetector
) -> tuple[bool | None, int, float]:
    """Return (has_person, frames_analyzed, maximum detector score)."""
    frames_analyzed = 0
    max_confidence = 0.0
    try:
        if record.media_type in (MediaType.IMAGE, MediaType.GIF):
            for frame in _pil_frames(Path(record.path)):
                frames_analyzed += 1
                max_confidence = max(max_confidence, detector.score(frame))
                if max_confidence > 0:
                    return True, frames_analyzed, max_confidence
            return (
                (False, frames_analyzed, max_confidence)
                if frames_analyzed
                else (None, 0, 0.0)
            )

        if record.media_type == MediaType.VIDEO:
            if not ffmpeg_available():
                return None, 0, 0.0
            import numpy as np
            from PIL import Image

            with tempfile.TemporaryDirectory(prefix="dedupe-human-video-") as tmp:
                frames = _extract_frames(
                    record.path,
                    Path(tmp),
                    max_frames=HUMAN_VIDEO_MAX_FRAMES,
                    frame_width=HUMAN_VIDEO_FRAME_WIDTH,
                    require_complete=True,
                )
                if not frames:
                    return None, 0, 0.0
                for path in frames:
                    with Image.open(path) as image:
                        frames_analyzed += 1
                        max_confidence = max(
                            max_confidence,
                            detector.score(np.asarray(image.convert("RGB"))),
                        )
                        if max_confidence > 0:
                            return True, frames_analyzed, max_confidence
                return False, frames_analyzed, max_confidence
    except Exception as exc:
        record.error = f"person-candidate detection failed: {exc}"
        record.human_detection_status = "analysis_failed"
        return None, frames_analyzed, max_confidence
    return None, frames_analyzed, max_confidence


def analyze_person_presence(
    record: FileRecord,
    detector: PersonDetector,
    *,
    cache_signature: str | None = None,
) -> bool | None:
    """Analyze one record, update its evidence fields, and return the decision."""
    # A re-analysis must never retain a stale trusted result if decoding fails.
    record.human_detection_status = None
    record.human_detection_signature = None
    has_person, frames_analyzed, max_confidence = _media_person_evidence(
        record, detector
    )
    record.human_frames_analyzed = frames_analyzed
    record.human_max_confidence = max_confidence
    record.human_detector = detector.backend
    if has_person is False:
        record.human_detection_status = "no_person_detected"
        record.human_detection_signature = cache_signature
    elif has_person is True:
        record.human_detection_status = "person_detected"
        record.human_detection_signature = cache_signature
    elif record.human_detection_status is None:
        record.human_detection_status = "analysis_failed"
    return has_person


def find_no_human_files(
    records: list[FileRecord],
    *,
    confidence: float = DEFAULT_CONFIDENCE,
    backend: str = DEFAULT_BACKEND,
    photon_model: str = DEFAULT_PHOTON_MODEL,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[FileRecord]:
    """Return files where no person was found, reusing trusted prior checks."""
    candidates = [
        record
        for record in records
        if record.media_type in (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO)
    ]
    if not candidates:
        return []

    signature = human_detection_signature(
        backend,
        confidence=confidence,
        photon_model=photon_model,
    )

    def has_cached_decision(record: FileRecord) -> bool:
        return (
            record.human_detection_status == MANUALLY_CONFIRMED_HUMAN_STATUS
            or (
                record.human_detection_signature == signature
                and record.human_detection_status in CACHEABLE_HUMAN_STATUSES
            )
        )

    cached = [record for record in candidates if has_cached_decision(record)]
    pending = [record for record in candidates if not has_cached_decision(record)]

    if progress and cached:
        progress("human-detection", len(cached), len(candidates))

    if pending:
        detector = create_person_detector(
            backend,
            confidence=confidence,
            photon_model=photon_model,
        )
        try:
            for index, record in enumerate(pending, start=len(cached) + 1):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                analyze_person_presence(
                    record,
                    detector,
                    cache_signature=signature,
                )
                if progress:
                    progress("human-detection", index, len(candidates))
        finally:
            detector.close()

    return [
        record
        for record in candidates
        if record.human_detection_signature == signature
        and record.human_detection_status == "no_person_detected"
    ]
