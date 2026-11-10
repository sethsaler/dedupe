"""People likelihood: a continuous 0–1 score for "does this file show a person?"

The Non-Human review answers a yes/no question with a recall-first detector
stack. That is the right shape for a safety filter, but it cannot *order* a
set of files, and on ordinary photos it says "person" almost everywhere. This
module answers a different question — which files are *least* likely to show
people — so any set of files can be sorted with the likeliest deletions first.

The score is the highest "person" confidence from the bundled YOLOX-tiny COCO
detector (Apache-2.0, Megvii), run once per image at 512 px through OpenCV's
DNN module. Videos take the maximum over a few evenly spaced frames; GIFs over
first / middle / last.

How it was chosen (labeled COCO val2017 sample, 500 images tuned on, 500 held
out, half with people from frame-filling to tiny and distant): ranking
non-human files first, the current OpenCV presence detector scores average
precision 0.54 (it flags 98% of people-free photos as "person"); YOLOX-S at
640 px 0.980; YOLOX-tiny at 512 px 0.982 at 0.18 s per photo. Larger inputs,
tiling, mirrored passes, int8 weights, and fusing face/HOG/segmentation
signals did not improve held-out ranking. ``dedupe benchmark-people`` reruns
the measurement on any labeled manifest.

A score is advice for ordering a review, never a deletion decision: nothing is
selected or removed because of it.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from .image_frames import animation_frame_count
from .models import FileRecord, MediaType
from .parallel import map_parallel, resolve_workers

ProgressCb = Callable[[int, int], None]

PEOPLE_SCORE_VERSION = "people-score-v1"
PEOPLE_MODEL_PATH = Path(__file__).parent / "assets" / "person_detection_yolox_tiny.onnx"
PEOPLE_MODEL_SHA256 = "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7"
PEOPLE_INPUT_SIZE = 512
# Decode a little above the network input so the letterbox resize, not the
# JPEG draft scaler, does the final downsampling.
PEOPLE_DECODE_MAX_SIDE = PEOPLE_INPUT_SIZE * 2
PEOPLE_VIDEO_FRAMES = 5
PEOPLE_VIDEO_FRAME_WIDTH = 640
PEOPLE_MEDIA_TYPES = (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO)
# COCO class 0 is "person"; YOLOX rows are [cx, cy, w, h, objectness, 80 classes].
_PERSON_COLUMN = 5
_STRIDES = (8, 16, 32)
_PAD_VALUE = 114.0


def people_score_signature() -> str:
    """Identify the scorer so cached scores from another model are not reused."""
    return "|".join(
        (
            PEOPLE_SCORE_VERSION,
            f"yolox-tiny={PEOPLE_MODEL_SHA256[:12]}",
            f"input={PEOPLE_INPUT_SIZE}",
            "channels=bgr",
            f"video-frames={PEOPLE_VIDEO_FRAMES}",
        )
    )


def has_current_people_score(record: FileRecord) -> bool:
    return (
        record.people_score is not None
        and record.people_score_signature == people_score_signature()
    )


def people_scoring_available() -> tuple[bool, str | None]:
    """(available, reason) without loading the model."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False, "people scoring requires OpenCV; install with `pip install -e '.[human]'`"
    if not PEOPLE_MODEL_PATH.is_file():
        return False, "the bundled person-detection model is missing"
    return True, None


class PeopleScorer:
    """One YOLOX network. OpenCV nets are not thread-safe: one per thread."""

    def __init__(self) -> None:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError(
                "people scoring requires OpenCV; install with `pip install -e '.[human]'`"
            ) from exc
        if not PEOPLE_MODEL_PATH.is_file():
            raise RuntimeError("the bundled person-detection model is missing")
        with PEOPLE_MODEL_PATH.open("rb") as model_file:
            digest = hashlib.file_digest(model_file, "sha256").hexdigest()
        if digest != PEOPLE_MODEL_SHA256:
            raise RuntimeError("the bundled person-detection model failed its integrity check")
        self.cv2 = cv2
        self.np = np
        try:
            self.net = cv2.dnn.readNet(str(PEOPLE_MODEL_PATH))
        except cv2.error as exc:
            raise RuntimeError(f"the person-detection model could not start: {exc}") from exc
        # The anchor grid both shape-checks the bundled model and decodes
        # box geometry for person_boxes().
        grids = []
        strides = []
        for stride in _STRIDES:
            cells = PEOPLE_INPUT_SIZE // stride
            xv, yv = np.meshgrid(np.arange(cells), np.arange(cells))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.extend([stride] * (cells * cells))
        self._grid = np.concatenate(grids).astype(np.float32)
        self._stride = np.array(strides, dtype=np.float32)[:, None]
        self._anchor_count = len(strides)

    def _forward(self, rgb):
        """One network pass; returns ``(output, letterbox_ratio)``."""
        cv2, np = self.cv2, self.np
        height, width = rgb.shape[:2]
        size = PEOPLE_INPUT_SIZE
        ratio = min(size / height, size / width)
        resized = cv2.resize(
            rgb,
            (max(1, int(width * ratio)), max(1, int(height * ratio))),
            interpolation=cv2.INTER_LINEAR,
        )
        canvas = np.full((size, size, 3), _PAD_VALUE, np.float32)
        # The released YOLOX weights expect OpenCV-style BGR, 0–255, unnormalized.
        canvas[: resized.shape[0], : resized.shape[1]] = resized[:, :, ::-1]
        self.net.setInput(np.transpose(canvas, (2, 0, 1))[None])
        output = self.net.forward(self.net.getUnconnectedOutLayersNames())[0][0]
        if output.shape[0] != self._anchor_count:
            raise RuntimeError("unexpected person-detection model output")
        return output, ratio

    def score_rgb(self, rgb) -> float:
        """Highest person confidence (objectness × class) in one RGB frame."""
        if not rgb.shape[0] or not rgb.shape[1]:
            return 0.0
        output, _ratio = self._forward(rgb)
        scores = output[:, 4] * output[:, _PERSON_COLUMN]
        return float(max(0.0, min(1.0, scores.max())))

    def person_boxes(
        self, rgb, *, threshold: float = 0.3
    ) -> list[tuple[float, float, float, float]]:
        """Person boxes in ``rgb`` as fractions ``(x0, y0, x1, y1)``.

        YOLOX rows are grid-relative: centers decode as
        ``(raw + grid) * stride`` and sizes as ``exp(raw) * stride`` in
        letterboxed input pixels, then scale back by the letterbox ratio.
        Overlapping duplicates are merged with standard NMS.
        """
        cv2, np = self.cv2, self.np
        height, width = rgb.shape[:2]
        if not height or not width:
            return []
        output, ratio = self._forward(rgb)
        scores = output[:, 4] * output[:, _PERSON_COLUMN]
        keep = scores >= threshold
        if not keep.any():
            return []
        xy = (output[:, :2] + self._grid) * self._stride
        wh = np.exp(output[:, 2:4]) * self._stride
        corners = np.stack(
            [
                (xy[:, 0] - wh[:, 0] / 2) / ratio,
                (xy[:, 1] - wh[:, 1] / 2) / ratio,
                wh[:, 0] / ratio,
                wh[:, 1] / ratio,
            ],
            axis=1,
        )[keep]
        kept_scores = scores[keep]
        chosen = cv2.dnn.NMSBoxes(corners.tolist(), kept_scores.tolist(), threshold, 0.45)
        boxes = []
        for index in np.ravel(chosen):
            x, y, w, h = (float(value) for value in corners[index])
            boxes.append((
                min(1.0, max(0.0, x / width)),
                min(1.0, max(0.0, y / height)),
                min(1.0, max(0.0, (x + w) / width)),
                min(1.0, max(0.0, (y + h) / height)),
            ))
        return boxes


def _image_frames(path: Path, max_frames: int):
    import numpy as np
    from PIL import Image, ImageOps

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass
    with Image.open(path) as image:
        image.draft("RGB", (PEOPLE_DECODE_MAX_SIDE, PEOPLE_DECODE_MAX_SIDE))
        count = animation_frame_count(image)
        indexes = sorted({0, count // 2, count - 1})[:max_frames] if count > 1 else [0]
        for index in indexes:
            image.seek(index)
            frame = ImageOps.exif_transpose(image) if count == 1 else image
            rgb = frame.convert("RGB")
            rgb.thumbnail((PEOPLE_DECODE_MAX_SIDE, PEOPLE_DECODE_MAX_SIDE))
            yield np.asarray(rgb)


def _video_frames(record: FileRecord):
    import numpy as np
    from PIL import Image

    from .similar_video import (
        _extract_seek_frame_ppm,
        _extract_seek_frames_ppm,
        ffmpeg_available,
        probe_video,
    )

    if not ffmpeg_available():
        raise RuntimeError("people scoring for videos requires ffmpeg")
    duration = record.duration
    if duration is None or duration <= 0:
        duration, _width, _height = probe_video(record.path)
    if not duration or duration <= 0:
        raise RuntimeError("could not read the video's duration")
    count = PEOPLE_VIDEO_FRAMES
    timestamps = [(index + 0.5) * duration / count for index in range(count)]
    ppms = _extract_seek_frames_ppm(
        record.path, timestamps, frame_width=PEOPLE_VIDEO_FRAME_WIDTH
    )
    if len(ppms) != len(timestamps):
        ppms = [
            ppm
            for ppm in (
                _extract_seek_frame_ppm(
                    record.path, timestamp, frame_width=PEOPLE_VIDEO_FRAME_WIDTH
                )
                for timestamp in timestamps
            )
            if ppm is not None
        ]
    if not ppms:
        raise RuntimeError("no video frames could be decoded")
    for ppm in ppms:
        with Image.open(BytesIO(ppm)) as image:
            yield np.asarray(image.convert("RGB"))


def score_record(record: FileRecord, scorer: PeopleScorer) -> float | None:
    """Score one file in place. A failure leaves no score (sorts as unknown)."""
    record.people_score = None
    record.people_score_signature = None
    if record.media_type not in PEOPLE_MEDIA_TYPES:
        return None
    try:
        if record.media_type == MediaType.VIDEO:
            frames = _video_frames(record)
        else:
            frames = _image_frames(Path(record.path), max_frames=3)
        best = None
        for frame in frames:
            value = scorer.score_rgb(frame)
            best = value if best is None else max(best, value)
    except Exception:
        return None
    if best is None:
        return None
    record.people_score = round(best, 4)
    record.people_score_signature = people_score_signature()
    return record.people_score


def score_people(
    records: list[FileRecord],
    *,
    workers: int | None = None,
    progress: ProgressCb | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[FileRecord]:
    """Score every media record without a current score; return those scored now."""
    pending = [
        record
        for record in records
        if record.media_type in PEOPLE_MEDIA_TYPES and not has_current_people_score(record)
    ]
    if not pending:
        if progress:
            progress(0, 0)
        return []
    local = threading.local()
    scorers: list[PeopleScorer] = []
    scorers_lock = threading.Lock()
    # Fail fast (and with a clear message) before any worker starts.
    first = PeopleScorer()
    scorers.append(first)
    claimed = threading.Event()

    def scorer_for_thread() -> PeopleScorer:
        scorer = getattr(local, "scorer", None)
        if scorer is None:
            if not claimed.is_set():
                with scorers_lock:
                    if not claimed.is_set():
                        claimed.set()
                        local.scorer = first
                        return first
            scorer = PeopleScorer()
            with scorers_lock:
                scorers.append(scorer)
            local.scorer = scorer
        return scorer

    map_parallel(
        lambda record: score_record(record, scorer_for_thread()),
        pending,
        workers=resolve_workers(workers),
        backend="thread",
        progress=progress,
        progress_every=1,
        cancelled=cancelled,
    )
    return pending


def least_likely_first_key(record: FileRecord) -> tuple:
    """Sort key: lowest people score first; unscored files last, then by path."""
    unscored = record.people_score is None
    return (unscored, record.people_score or 0.0, record.path)
