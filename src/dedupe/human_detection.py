"""Pluggable local person-candidate detection for images, GIFs, and videos."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .models import FileRecord, MediaType
from .similar_video import MAX_FRAMES, _extract_frames, ffmpeg_available

ProgressCb = Callable[[str, int, int], None]

DEFAULT_CONFIDENCE = 0.25
DEFAULT_BACKEND = "opencv"
DEFAULT_PHOTON_MODEL = "moondream3.1-9B-A2B"
HUMAN_BACKENDS = ("opencv", "photon", "ensemble")
DETECT_MAX_SIDE = 960


class PersonDetector(Protocol):
    """Small interface shared by all local detector backends."""

    backend: str

    def score(self, rgb_frame) -> float: ...

    def close(self) -> None: ...


class _OpenCVPersonDetector:
    """CPU-only detector combining a frontal-face cascade with HOG people detection."""

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
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        self.face = (
            cv2.CascadeClassifier(str(cascade_path)) if cascade_path.is_file() else None
        )
        if self.face is not None and self.face.empty():
            self.face = None
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.backend = "opencv_face_hog" if self.face is not None else "opencv_hog"

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

        gray = self.cv2.cvtColor(frame, self.cv2.COLOR_RGB2GRAY)
        if self.face is not None:
            faces = self.face.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=4,
                minSize=(20, 20),
            )
            if len(faces):
                return 1.0

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
                    max_frames=MAX_FRAMES,
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
    record: FileRecord, detector: PersonDetector
) -> bool | None:
    """Analyze one record, update its evidence fields, and return the decision."""
    has_person, frames_analyzed, max_confidence = _media_person_evidence(
        record, detector
    )
    record.human_frames_analyzed = frames_analyzed
    record.human_max_confidence = max_confidence
    record.human_detector = detector.backend
    if has_person is False:
        record.human_detection_status = "no_person_detected"
    elif has_person is True:
        record.human_detection_status = "person_detected"
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
    """Return files where no person was found; unreadable files are excluded."""
    candidates = [
        record
        for record in records
        if record.media_type in (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO)
    ]
    if not candidates:
        return []

    detector = create_person_detector(
        backend,
        confidence=confidence,
        photon_model=photon_model,
    )
    no_humans: list[FileRecord] = []
    try:
        for index, record in enumerate(candidates, start=1):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            has_person = analyze_person_presence(record, detector)
            if has_person is False:
                no_humans.append(record)
            if progress:
                progress("human-detection", index, len(candidates))
    finally:
        detector.close()
    return no_humans
