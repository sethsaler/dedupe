# Next features: performance ideas for face counting & gender classification

Ideas for making the Count Faces pipeline (YuNet detection + InsightFace
genderage) faster. Ordered roughly by expected impact. Each entry notes effort
and risk so we can pick what's worth doing.

> **Status 2026-09:** items 1–3 landed (single-pass video frame extraction —
> extended to person detection with chunked 4-frame passes — plus PIL draft
> decode and the duration-scaled 4–16 frame budget). Item 4 landed in its safe
> half only (the second YuNet pass is skipped when the image is already at or
> below its 480 px scale); the "skip when pass 1 found faces" variant still
> needs a real-corpus benchmark. Item 5 remains unbenchmarked. Items 6–8 stay
> parked. The cache signatures moved to `face-count-v3` and
> `human-presence-…|frame-decode=v2`, so stale counts/decisions invalidate.

## Current cost profile (facts, not guesses)

- Detection runs **two YuNet passes per frame**: full-size capped at
  `DETECT_MAX_SIDE = 960`, then a second pass at
  `YUNET_SECOND_PASS_MAX_SIDE = 480` (`face_detection.py::_detect_best`).
- Videos decode **up to 16 frames each** (`FACE_VIDEO_MAX_FRAMES = 16`), one
  `ffmpeg -ss ... -frames:v 1` process **per frame**, at 960px width.
- Genderage classification runs on every detected face: a 96×96 crop through
  a tiny CNN — cheap compared to detection and decode. Don't "optimize" this
  first.
- Parallelism is thread-based, capped at `DEFAULT_HUMAN_WORKERS_CAP = 4`,
  one YuNet detector per thread (YuNet mutates its input size and can't be
  shared).
- Counts are cached by signature (`face-count-v2|…`); unchanged files with a
  matching signature are skipped entirely. Gender results ride the same cache.

## High impact

### 1. Decode video frames in one ffmpeg pass instead of 16
Each sampled frame currently spawns its own ffmpeg process and seeks from the
start of the file. One ffmpeg invocation with an interval filter (e.g.
`fps=1/N` or `select='not(mod(n\,K))'`) emitting all sample frames would
remove ~15 process startups and repeated seeks per video.
- Effort: medium (replace `_extract_seek_frame_ppm` loop with a multi-frame
  extraction path; keep the per-timestamp fallback).
- Risk: low–medium. Sampling becomes decode-order based rather than exact
  timestamps; fine for "busiest frame" semantics.

### 2. Decode images at reduced scale (PIL draft mode)
Detection resizes everything to ≤960px anyway, but images are decoded at full
resolution first (`_pil_frames`). JPEG supports draft-mode decoding
(`Image.draft("RGB", (960, 960))`), which decodes directly at reduced DCT
scale — typically 3–5× faster decode on 12MP+ photos with no accuracy loss
for detection.
- Effort: low–medium (apply draft in `_pil_frames` for JPEG; PIL ignores it
  gracefully for other formats).
- Risk: low. HEIC/GIF unaffected; verify EXIF orientation handling stays
  intact.

### 3. Adaptive video frame budget
16 frames is a lot for a 3-second clip and sparse for a 20-minute one. Scale
the sample count with duration (e.g. 1 frame per ~5s, clamped to 4–16).
- Effort: low (change the `max_frames` argument computed from `duration`).
- Risk: low. Shorter clips get fewer redundant frames; long clips keep the
  cap.

## Medium impact

### 4. Skip the second detection pass when it can't help
The 480px pass exists for close-up faces. When the full-size pass already
finds faces — or the image's short side is small enough that downscaling is
pointless — the second pass mostly re-detects the same faces. Making it
conditional (e.g. only when pass 1 found nothing, or short side > 960)
roughly halves detection time on typical photos.
- Effort: low.
- Risk: medium — it's an accuracy tradeoff. Needs a benchmark on a real
  folder before shipping (counts could drop on some group photos). Gate it
  behind the same signature bump so caches stay honest.

### 5. Raise/expose the worker cap for face counting
Face counting is CPU-bound and ffmpeg-heavy; the cap of 4 is conservative on
modern machines, but more workers also means more concurrent ffmpeg decodes
contending. Benchmark 4 vs 6 vs 8 on a video-heavy folder; expose the winner
as an advanced scan option if it's clearly better.
- Effort: low to expose, medium to validate.
- Risk: low if benchmarked; memory grows per detector instance.

## Low impact / later

### 6. Batch genderage inference
cv2.dnn accepts batched blobs; classifying all of a frame's faces in one
forward pass instead of one-by-one saves a little per-frame overhead.
- Effort: low. Payoff small — genderage is not the bottleneck. Do this only
  if profiling says otherwise.

### 7. Early-exit face *presence* for videos
"Has at least one face" could stop sampling after the first positive frame,
but face counting needs the busiest frame, so the two use cases diverge. Only
worth it if we add a presence-only mode; not for the current feature.
- Effort: medium. Skip unless a use case appears.

### 8. CoreML / Neural Engine offload (Apple Silicon)
Running YuNet (and possibly genderage) through CoreML could move inference to
the ANE. Platform-specific, real packaging and parity work, and the models
would need re-validation.
- Effort: high. Park it until CPU profiling shows inference itself is the
  wall — today, decode and process startup are bigger.

## Not worth doing (checked already)

- Genderage preprocessing/crop: already minimal (one `warpAffine` per face).
- Cache reuse: counts + gender already skip cleanly via the signature;
  fresh scans over an unchanged library cost ~nothing.
