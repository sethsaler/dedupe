"""Pluggable local person-candidate detection for images, GIFs, and videos."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Protocol

from .human_policy import (
    CACHEABLE_HUMAN_STATUSES,
    HUMAN_DETECTION_CACHE_VERSION,
    MANUALLY_CONFIRMED_HUMAN_STATUS,
    may_enter_no_person_review,
)
from .image_frames import animation_frame_count
from .models import FileRecord, MediaType
from .parallel import DEFAULT_HUMAN_WORKERS_CAP, map_parallel, resolve_workers
from .people_score import (
    MAX_CONSECUTIVE_VIDEO_TIMEOUTS,
    PEOPLE_MODEL_SHA256,
    PeopleScorer,
    call_with_cancelled,
    verify_model_sha256,
)
from .similar_video import (
    _extract_frames,
    _extract_seek_frame_ppm,
    _extract_seek_frames_ppm,
    _sample_timestamps,
    ffmpeg_available,
    probe_video,
)

ProgressCb = Callable[[str, int, int, str | None], None]

DEFAULT_CONFIDENCE = 0.25
DEFAULT_BACKEND = "opencv"
DEFAULT_PHOTON_MODEL = "moondream3.1-9B-A2B"
HUMAN_BACKENDS = ("opencv", "photon", "ensemble")

#: Models that have started successfully on this machine. The Moondream SDK
#: downloads ~10 GB on first use, inside its own call, with no progress hook —
#: the marker lets the scan narrate that wait only when it is actually coming.
PHOTON_READY_FILENAME = "photon-ready.json"


def _photon_ready_path() -> Path:
    return Path.home() / ".cache" / "dedupe" / PHOTON_READY_FILENAME


def photon_model_ready(model_name: str) -> bool:
    """True when this Photon model has started successfully here before."""
    try:
        data = json.loads(_photon_ready_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return model_name in set(data.get("models") or [])


def mark_photon_model_ready(model_name: str) -> None:
    path = _photon_ready_path()
    try:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            models = set(data.get("models") or [])
        except (OSError, ValueError):
            models = set()
        models.add(model_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"models": sorted(models)}), encoding="utf-8")
    except OSError:
        pass
DETECT_MAX_SIDE = 960
# Preserve detail for regional face checks; each inference still caps at 960px.
DETECT_INPUT_MAX_SIDE = DETECT_MAX_SIDE * 2
YUNET_SECOND_PASS_MAX_SIDE = 480
YUNET_CLOSE_UP_MAX_SIDE = 320
YUNET_SCORE_THRESHOLD = 0.55
# Person *presence* is recall-first: a missed woman is a deletion candidate.
# Face *counting* keeps the stricter 0.55 threshold.
YUNET_PRESENCE_SCORE_THRESHOLD = 0.35
YUNET_PRESENCE_SCALES = (
    DETECT_MAX_SIDE,
    YUNET_SECOND_PASS_MAX_SIDE,
    YUNET_CLOSE_UP_MAX_SIDE,
)
YUNET_NMS_THRESHOLD = 0.3
YUNET_TOP_K = 5000
PHOTON_DETECT_TARGETS = ("woman", "girl", "person", "face")
YUNET_MODEL_PATH = (
    Path(__file__).parent / "assets" / "face_detection_yunet_2023mar.onnx"
)
YUNET_MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
HUMAN_VIDEO_MAX_FRAMES = 16
HUMAN_VIDEO_FRAME_WIDTH = 640
# Frames are extracted in chunks of this many timestamps per ffmpeg process:
# one process per frame costs up to 16 startups, but a single 16-frame pass
# would decode frames past a positive hit — this stage early-exits on the
# first person found, so chunks bound the wasted decode work.
HUMAN_VIDEO_EXTRACT_CHUNK = 4


class PersonDetector(Protocol):
    """Small interface shared by all local detector backends."""

    backend: str

    def score(self, rgb_frame) -> float: ...

    def close(self) -> None: ...


class _OpenCVPersonDetector:
    """CPU-only detector combining YuNet faces with YOLOX-tiny person detection."""

    def __init__(self, confidence: float) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "person-candidate detection requires OpenCV; "
                "install with `pip install -e '.[human]'`"
            ) from exc

        if hasattr(cv2, "setNumThreads"):
            cv2.setNumThreads(1)
        self.cv2 = cv2
        self.confidence = max(0.0, float(confidence))
        verify_model_sha256(
            YUNET_MODEL_PATH,
            YUNET_MODEL_SHA256,
            missing_error=(
                "the bundled OpenCV YuNet face model is missing; refusing to "
                "classify files as non-human with full-body detection alone"
            ),
            integrity_error=(
                "the bundled OpenCV YuNet face model failed its integrity check; "
                "refusing to classify files as non-human"
            ),
        )
        try:
            self.face = cv2.FaceDetectorYN.create(
                str(YUNET_MODEL_PATH),
                "",
                (320, 320),
                YUNET_PRESENCE_SCORE_THRESHOLD,
                YUNET_NMS_THRESHOLD,
                YUNET_TOP_K,
            )
        except (AttributeError, cv2.error) as exc:
            raise RuntimeError(
                "OpenCV YuNet face detection could not start; install OpenCV 4.5.4 "
                "or newer. No media was classified as non-human."
            ) from exc
        self.people: PeopleScorer | None = PeopleScorer()
        self.backend = "opencv_yunet_yolox"

    def _yunet_best(self, bgr_frame) -> float:
        """Score faces at several scales; any presence-threshold hit is enough."""
        best = 0.0
        checked_sizes: set[tuple[int, int]] = set()
        for max_side in YUNET_PRESENCE_SCALES:
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
            if faces is not None and len(faces):
                best = max(best, max(float(face[-1]) for face in faces))
                if best >= YUNET_PRESENCE_SCORE_THRESHOLD:
                    return best
        return best

    def _yunet_tiles(self, bgr_frame) -> float:
        """Look for small or off-center faces the full-frame passes missed."""
        height, width = bgr_frame.shape[:2]
        if height < 160 or width < 160:
            return 0.0
        overlap_y = max(1, height // 5)
        overlap_x = max(1, width // 5)
        tile_h = min(height, (height + overlap_y) // 2)
        tile_w = min(width, (width + overlap_x) // 2)
        best = 0.0
        for top in (0, height - tile_h):
            for left in (0, width - tile_w):
                crop = bgr_frame[top : top + tile_h, left : left + tile_w]
                best = max(best, self._yunet_best(crop))
                if best >= YUNET_PRESENCE_SCORE_THRESHOLD:
                    return best
        return best

    def _face_score(self, rgb_frame) -> float:
        """Upright scales, then a mirrored pass, then overlapping tiles."""
        # Color conversion is linear, so convert once before deriving the
        # detection scales rather than converting each resized candidate.
        frame = self.cv2.cvtColor(rgb_frame, self.cv2.COLOR_RGB2BGR)
        best = self._yunet_best(frame)
        if best >= YUNET_PRESENCE_SCORE_THRESHOLD:
            return best
        flipped = self.cv2.flip(frame, 1)
        best = max(best, self._yunet_best(flipped))
        if best >= YUNET_PRESENCE_SCORE_THRESHOLD:
            return best
        return max(best, self._yunet_tiles(frame))

    def _body_score(self, rgb_frame) -> float:
        """YOLOX-tiny full-body person confidence."""
        people = getattr(self, "people", None)
        if people is None or rgb_frame.shape[0] == 0 or rgb_frame.shape[1] == 0:
            return 0.0
        score = people.score_rgb(rgb_frame)
        return score if score >= self.confidence else 0.0

    def score(self, rgb_frame) -> float:
        import numpy as np

        frame = np.ascontiguousarray(rgb_frame)
        # Tile the retained input, not an already downsampled full-frame view.
        # Otherwise a distant face may lose the pixels that make it detectable.
        face_score = self._face_score(frame)
        if face_score > 0:
            return face_score
        return self._body_score(frame)

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
        # Query woman/girl first so body-only or styled photos of women are kept
        # even when the generic "person" label misses.
        for target in PHOTON_DETECT_TARGETS:
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
    # Decoder detail and animation coverage affect no-person decisions.
    parts.append("frame-decode=v3")
    parts.append(f"input-max-side={DETECT_INPUT_MAX_SIDE}")
    parts.append(f"animation-frames={HUMAN_VIDEO_MAX_FRAMES}")
    if normalized in {"opencv", "ensemble"}:
        parts.append(f"confidence={max(0.0, float(confidence)):g}")
        parts.append(f"yunet={YUNET_MODEL_SHA256[:12]}")
        parts.append(f"yolox={PEOPLE_MODEL_SHA256[:12]}")
        parts.append(f"face-confidence={YUNET_PRESENCE_SCORE_THRESHOLD:g}")
        parts.append("face-flip=1")
        parts.append("face-tiles=2x2")
    if normalized in {"photon", "ensemble"}:
        parts.append(f"model={photon_model.strip() or DEFAULT_PHOTON_MODEL}")
    return "|".join(parts)


def _pil_frames(path: Path, *, max_side: int = DETECT_MAX_SIDE, max_frames: int = 3):
    """Yield RGB arrays for a still image or representative GIF frames."""
    import numpy as np
    from PIL import Image, ImageOps

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass

    with Image.open(path) as image:
        # JPEG decodes straight at a reduced DCT scale with draft() — much
        # faster on high-resolution photos. Retain the requested detail for
        # regional detection; other formats ignore the hint. Apply EXIF next.
        image.draft("RGB", (max_side, max_side))
        frame_count = animation_frame_count(image)
        count = min(frame_count, max_frames)
        indexes = (
            [0] if count <= 1
            else sorted({round(i * (frame_count - 1) / (count - 1)) for i in range(count)})
        )
        if max_frames == 3:
            # Preserve the face-counter's existing first/middle/last sampling.
            indexes = sorted({0, frame_count // 2, max(0, frame_count - 1)})
        for index in indexes:
            image.seek(index)
            frame = ImageOps.exif_transpose(image) if frame_count == 1 else image
            rgb = frame.convert("RGB")
            rgb.thumbnail((max_side, max_side))
            yield np.asarray(rgb)


def _person_sample_timestamps(duration: float) -> list[float]:
    """Use the normal sample set, but inspect likely/representative frames first."""
    timestamps = _sample_timestamps(duration, HUMAN_VIDEO_MAX_FRAMES)
    indexes = [len(timestamps) // 2, 0, len(timestamps) - 1]
    indexes.extend(index for index in range(len(timestamps)) if index not in indexes)
    return [timestamps[index] for index in indexes]


def _media_person_evidence(
    record: FileRecord,
    detector: PersonDetector,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[bool | None, int, float]:
    """Return (has_person, frames_analyzed, maximum detector score)."""
    frames_analyzed = 0
    max_confidence = 0.0
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    try:
        if record.media_type in (MediaType.IMAGE, MediaType.GIF):
            for frame in _pil_frames(
                Path(record.path), max_side=DETECT_INPUT_MAX_SIDE,
                max_frames=HUMAN_VIDEO_MAX_FRAMES,
            ):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
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

            duration = record.duration
            if duration is None or duration <= 0:
                duration, width, height = call_with_cancelled(
                    probe_video, record.path, cancelled=cancelled
                )
                record.duration = duration
                record.width = width or record.width
                record.height = height or record.height

            if duration is not None and duration > 0:
                # Seek and score incrementally. A positive frame stops all
                # later decodes; a no-person decision still requires every
                # requested frame to decode successfully. Frames decode in
                # chunks of HUMAN_VIDEO_EXTRACT_CHUNK per ffmpeg process, so
                # a positive chunk still skips the remaining processes.
                # Note: unlike face counting, the frame budget stays flat at
                # HUMAN_VIDEO_MAX_FRAMES regardless of duration — presence is
                # recall-first and early-exits on positives, so sparse
                # short-clip sampling would only add miss risk.
                timestamps = _person_sample_timestamps(duration)
                consecutive_timeouts = 0
                for start in range(0, len(timestamps), HUMAN_VIDEO_EXTRACT_CHUNK):
                    if cancelled and cancelled():
                        raise InterruptedError("scan cancelled")
                    chunk = timestamps[start : start + HUMAN_VIDEO_EXTRACT_CHUNK]
                    try:
                        ppms = call_with_cancelled(
                            _extract_seek_frames_ppm,
                            record.path,
                            chunk,
                            frame_width=HUMAN_VIDEO_FRAME_WIDTH,
                            cancelled=cancelled,
                        )
                    except (subprocess.TimeoutExpired, TimeoutError):
                        consecutive_timeouts += 1
                        if consecutive_timeouts >= MAX_CONSECUTIVE_VIDEO_TIMEOUTS:
                            return None, frames_analyzed, max_confidence
                        ppms = []
                    if len(ppms) != len(chunk):
                        # Per-seek fallback when the batched pass fails.
                        ppms = []
                        for timestamp in chunk:
                            if cancelled and cancelled():
                                raise InterruptedError("scan cancelled")
                            try:
                                ppm = call_with_cancelled(
                                    _extract_seek_frame_ppm,
                                    record.path,
                                    timestamp,
                                    frame_width=HUMAN_VIDEO_FRAME_WIDTH,
                                    cancelled=cancelled,
                                )
                            except (subprocess.TimeoutExpired, TimeoutError):
                                return None, frames_analyzed, max_confidence
                            if ppm is None:
                                return None, frames_analyzed, max_confidence
                            ppms.append(ppm)
                    consecutive_timeouts = 0
                    for ppm in ppms:
                        if cancelled and cancelled():
                            raise InterruptedError("scan cancelled")
                        with Image.open(BytesIO(ppm)) as image:
                            frames_analyzed += 1
                            max_confidence = max(
                                max_confidence,
                                detector.score(np.asarray(image.convert("RGB"))),
                            )
                        if max_confidence > 0:
                            return True, frames_analyzed, max_confidence
                return False, frames_analyzed, max_confidence

            # Rare fallback for containers without a probeable duration.
            with tempfile.TemporaryDirectory(prefix="dedupe-human-video-") as tmp:
                frames = call_with_cancelled(
                    _extract_frames,
                    record.path,
                    Path(tmp),
                    max_frames=HUMAN_VIDEO_MAX_FRAMES,
                    frame_width=HUMAN_VIDEO_FRAME_WIDTH,
                    require_complete=True,
                    cancelled=cancelled,
                )
                if not frames:
                    return None, 0, 0.0
                for path in frames:
                    if cancelled and cancelled():
                        raise InterruptedError("scan cancelled")
                    with Image.open(path) as image:
                        frames_analyzed += 1
                        max_confidence = max(
                            max_confidence,
                            detector.score(np.asarray(image.convert("RGB"))),
                        )
                        if max_confidence > 0:
                            return True, frames_analyzed, max_confidence
                return False, frames_analyzed, max_confidence
    except InterruptedError:
        raise
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
    cancelled: Callable[[], bool] | None = None,
) -> bool | None:
    """Analyze one record, update its evidence fields, and return the decision."""
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    # A re-analysis must never retain a stale trusted result if decoding fails.
    record.human_detection_status = None
    record.human_detection_signature = None
    has_person, frames_analyzed, max_confidence = _media_person_evidence(
        record, detector, cancelled=cancelled
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
    workers: int | None = None,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
    confirm_with_faces: bool = True,
) -> list[FileRecord]:
    """Return files where no person was found, reusing trusted prior checks.

    ``confirm_with_faces=False`` skips the YuNet second-opinion pass on
    Photon/ensemble no-person candidates. Callers set it when a full face
    count runs in the same scan anyway (the face veto is enforced at group
    build time via ``may_enter_no_person_review``), so the same files are
    not analyzed for faces twice.
    """
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

    normalized_backend = backend.strip().lower()
    human_workers = resolve_workers(workers, cap=DEFAULT_HUMAN_WORKERS_CAP)

    def _run_opencv_stage(
        items: list[FileRecord],
        *,
        for_ensemble: bool,
        on_done: Callable[[int, int], None] | None = None,
    ) -> list[FileRecord]:
        """Run OpenCV presence detection (threaded when beneficial) and return uncertain items."""
        local = threading.local()
        detectors: list[PersonDetector] = []
        detectors_lock = threading.Lock()
        uncertain: list[FileRecord] = []
        uncertain_lock = threading.Lock()

        def get_detector() -> PersonDetector:
            detector = getattr(local, "detector", None)
            if detector is None:
                detector = create_person_detector(
                    "opencv",
                    confidence=confidence,
                    photon_model=photon_model,
                )
                local.detector = detector
                with detectors_lock:
                    detectors.append(detector)
            return detector

        def analyze(record: FileRecord) -> None:
            detector = get_detector()
            has_person = analyze_person_presence(
                record,
                detector,
                cache_signature=None if for_ensemble else signature,
                cancelled=cancelled,
            )
            if for_ensemble:
                if has_person is True:
                    record.human_detector = f"ensemble:{detector.backend}"
                    record.human_detection_signature = signature
                elif has_person is False:
                    # Leave status/signature unset until Photon confirms no person.
                    record.human_detection_status = None
                    record.human_detection_signature = None
                    with uncertain_lock:
                        uncertain.append(record)

        try:
            map_parallel(
                analyze,
                items,
                workers=human_workers,
                backend="thread",
                progress=on_done,
                progress_every=1,
                cancelled=cancelled,
            )
        finally:
            for detector in detectors:
                detector.close()
        return uncertain

    if pending and normalized_backend == "opencv":
        # OpenCV detector objects mutate their input size and cannot be shared
        # across threads. Keep one detector per worker thread instead.
        def opencv_progress(done: int, _total: int) -> None:
            if progress:
                progress("human-detection", len(cached) + done, len(candidates))

        _run_opencv_stage(pending, for_ensemble=False, on_done=opencv_progress)
    elif pending and normalized_backend == "ensemble":
        half_budget = len(pending) // 2

        def prepass_progress(done: int, _total: int) -> None:
            if progress:
                progress(
                    "human-detection",
                    len(cached) + (done // 2),
                    len(candidates),
                )

        uncertain = _run_opencv_stage(
            pending, for_ensemble=True, on_done=prepass_progress
        )
        if not uncertain:
            if progress:
                progress("human-detection", len(candidates), len(candidates))
        else:
            effective_model = photon_model.strip() or DEFAULT_PHOTON_MODEL
            if progress and not photon_model_ready(effective_model):
                progress(
                    "human-detection",
                    len(cached) + half_budget,
                    len(candidates),
                    "Preparing the Photon model — first use downloads ~10 GB, "
                    "which can take a long time…",
                )
            photon_detector = create_person_detector(
                "photon",
                confidence=confidence,
                photon_model=photon_model,
            )
            mark_photon_model_ready(effective_model)
            try:
                remaining_budget = len(pending) - half_budget
                total_uncertain = len(uncertain)
                for idx, record in enumerate(uncertain, start=1):
                    if cancelled and cancelled():
                        raise InterruptedError("scan cancelled")
                    cv_backend = record.human_detector or "opencv"
                    analyze_person_presence(
                        record,
                        photon_detector,
                        cache_signature=signature,
                        cancelled=cancelled,
                    )
                    record.human_detector = f"ensemble:{cv_backend}+{photon_detector.backend}"
                    if progress:
                        scaled = half_budget + round(
                            (idx / total_uncertain) * remaining_budget
                        )
                        progress(
                            "human-detection",
                            len(cached) + scaled,
                            len(candidates),
                        )
            finally:
                photon_detector.close()
    elif pending:
        effective_model = photon_model.strip() or DEFAULT_PHOTON_MODEL
        if (
            normalized_backend == "photon"
            and progress
            and not photon_model_ready(effective_model)
        ):
            # The SDK's first run downloads ~10 GB inside create_person_detector
            # with no progress of its own; say so instead of sitting silent.
            progress(
                "human-detection",
                len(cached),
                len(candidates),
                "Preparing the Photon model — first use downloads ~10 GB, "
                "which can take a long time…",
            )
        detector = create_person_detector(
            backend,
            confidence=confidence,
            photon_model=photon_model,
        )
        if normalized_backend == "photon":
            mark_photon_model_ready(effective_model)
        try:
            for index, record in enumerate(pending, start=len(cached) + 1):
                if cancelled and cancelled():
                    raise InterruptedError("scan cancelled")
                analyze_person_presence(
                    record,
                    detector,
                    cache_signature=signature,
                    cancelled=cancelled,
                )
                if progress:
                    progress("human-detection", index, len(candidates))
        finally:
            detector.close()

    if confirm_with_faces and normalized_backend in {"photon", "ensemble"}:
        # YuNet + genderage is a cheap second opinion on Photon's no-person
        # pile: any counted face, especially a female face, is kept.
        from .face_detection import protect_no_person_candidates

        protect_no_person_candidates(
            candidates,
            workers=workers,
            progress=progress,
            cancelled=cancelled,
        )

    return [record for record in candidates if may_enter_no_person_review(record)]
