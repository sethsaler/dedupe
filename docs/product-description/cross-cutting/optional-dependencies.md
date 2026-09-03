# Optional dependencies

## Summary

Dedupe's core — finding exact and similar images, reviewing groups, moving files — runs on its required Python packages alone. Everything else is an optional capability layered on top: ffmpeg brings video understanding, OpenCV brings person and face detection, and the opt-in Photon backend brings a heavier person detector that downloads itself on first use. Each missing piece degrades the experience in a specific, visible way rather than breaking it: stages are skipped with warnings, categories come back empty, or the health check names the blocker. This document owns those degraded behaviors, one dependency at a time; the baseline health check that reports them is [`doctor`](../cli/doctor.md), and the scan stages they affect are owned by [The scan pipeline](../foundations/scan-pipeline.md).

## ffmpeg and ffprobe

**What needs them.** Everything about videos: similar-video fingerprinting (up to 16 sampled frames extracted by direct ffmpeg seeks), video dimension probing for the low-resolution review, frame sampling for person detection and face counting in videos, and video previews in the UI.

**What happens without them.** A scan completes normally; the video similarity stage is skipped entirely. In the scan's diagnostics the `similar_video` stage reports zero attempted files and carries the warning "ffmpeg/ffprobe unavailable; eligible videos were not analyzed". No similar-video groups are produced; exact duplicates of videos are still found (exact hashing reads bytes, not frames). Videos without dimensions cannot be judged for the low-resolution review and appear among that stage's failures. In the UI, video cards have no fallback still when a preview cannot be generated — the preview endpoint answers "no preview" — while images fall back to serving the original file.

**How the user finds out.** `dedupe doctor` prints one line per binary — the first line of `ffmpeg -version` / `ffprobe -version`, or `not found` — and in `--json` form records `available`, `path`, and `version` for each. Neither binary affects the exit code: they are capabilities, not blockers. After a scan, the stage warning above appears in the diagnostics the UI shows with the results.

> Technical note: the availability check requires *both* binaries on `PATH`; a machine with ffmpeg but not ffprobe (or the reverse) counts as missing.

**How to get them.** Install ffmpeg (which provides ffprobe) through the system package manager, e.g. `brew install ffmpeg`, then confirm with `dedupe doctor`.

## OpenCV and the bundled models

**What needs them.** The no-person review (`opencv` and `ensemble` backends) and the faces review. OpenCV is CPU-only; the YuNet face model and the InsightFace genderage model ship in the application's assets and download nothing at runtime.

**What happens without them.** The affected review categories produce nothing. The no-person review fails closed by design: OpenCV runs the bundled YuNet face model before its full-body detector, and if the face model is missing, corrupt, or cannot start, the scan surfaces no media as Non-Human rather than guessing. With no candidates, there is nothing to select and nothing an action can move from that category. A scan that did not request these reviews is entirely unaffected.

> Technical note: failing closed is deliberate — a person detector that quietly misses people would lead to deleting photos of people, which is the exact mistake the product exists to prevent.

**How the user finds out.** `dedupe doctor` prints `OpenCV/YuNet (optional): ready` only when both the cv2 module imports *and* the bundled YuNet model file exists on disk; any other state prints `not ready`, and the `--json` form separates `available`, `version`, `yunet_model`, and `yunet_ready` so the two halves can fail independently. A damaged model file alone makes the line `not ready` while the exit code stays 0 — OpenCV is optional. During a scan, per-file analysis failures accumulate in the stage diagnostics as "N file(s) could not be analyzed for people" / "…for faces".

**How to get them.** The standard installation includes OpenCV and the bundled models; `dedupe doctor` is the check. A `not ready` line with cv2 present points at the model file named in the `--json` report.

## Photon

**What needs it.** The `photon` and `ensemble` backends of the no-person review: Moondream's local Photon runtime running a vision model (default `moondream3.1-9B-A2B`). Photon is opt-in; the default backend is OpenCV.

**What happens without it.** Nothing changes until the user chooses it. The Moondream SDK is an optional extra — a Photon-backed scan without it fails with a message naming the install command (`pip install -e '.[photon]'`). With the SDK present, the first scan that uses a Photon backend downloads roughly 10 GB of model weights; all processing stays local once the model is available. The download happens inside the SDK with no progress hook of its own, so Dedupe narrates it instead: before starting a Photon model this machine has never run, the scan's person-detection stage switches its progress line to "Preparing the Photon model — first use downloads ~10 GB, which can take a long time…" (the CLI prints the same line). Once a model has started successfully, Dedupe records that in `~/.cache/dedupe/photon-ready.json` and later scans go straight to work. `ensemble` limits Photon's involvement to frames OpenCV scored zero on; `photon` uses it throughout. Photon runs serially — its model is substantially heavier and does not promise thread-safe inference — so a Photon scan is slower per file than an OpenCV one. Photon detection returns person/face boxes rather than a calibrated confidence score; the UI shows how many frames were analyzed, not scores.

**How the user finds out.** `dedupe doctor` deliberately does not probe or start the Photon runtime — a health check can never trigger a 10 GB download. Availability therefore announces itself at scan time, not before: either the scan proceeds, or it refuses with the missing-SDK message, or model startup fails with the model name in the error.

**How to get it.** Install the optional extra, choose the backend in the scan options (or `--human-backend photon`); the first run acquires the model.

## Required Python packages

**What needs them.** Everything: PIL (image decoding), imagehash (perceptual hashes), pybktree (hash candidate lookup), send2trash (system Trash), flask (the web UI).

**What happens without them.** Dedupe does not run the affected function at all; this is the only dependency class that blocks core operation. `dedupe doctor` prints `Import {name}: MISSING` for each failure, adds "cannot import {name}" to the blockers list, ends with `Core operation: BLOCKED`, and exits 1. A failing flask import means the web UI cannot start at all.

**How to get them.** Reinstall or update Dedupe through its installer, which creates the isolated environment with all required packages; `dedupe doctor` confirms the result.

## Cancel and interrupt

For a scan running with an optional dependency missing, slow, or downloading:

| Event | Before the affected stage | While the affected stage runs |
| --- | --- | --- |
| The user aborts explicitly (Ctrl+C / Cancel) | The scan stops; nothing was written except the cache at the end. | Same: cancellation is cooperative and checked between work items, including inside a Photon-backed scan. |
| The user does something else mid-way | No effect; selections are locked during scans. | Same. |
| A clean complete happens elsewhere | No effect. | No effect — one scan at a time. |
| The environment fails | This is the subject of this document: a dependency's absence converts to a skipped stage or empty category with a diagnostic warning, not a crash. | Per-file analysis failures are recorded and the scan continues; a corrupt media file never aborts it. |
| The page or process goes away | A browser reload re-attaches to the running scan; closing the last tab schedules the server's graceful shutdown. | Same; a killed server loses the in-progress scan. A Photon download interrupted by process death resumes or retries on next use (see Open questions). |
| Something else changes the target | Caught later by revalidation at action time, not by dependency checks. | Same. |
| The input channel changes | No effect (the CLI takes no stdin; the UI has no counterpart). | No effect. |
| A resumed review supersedes | No effect on dependency behavior. | No effect. |

## Edge cases

- ffprobe missing while ffmpeg is present (or the reverse) counts as missing entirely; `doctor` reports the two binaries separately, which is how the user tells which one is gone.
- A backend name outside `opencv`/`photon`/`ensemble` is rejected before any scan starts ("unknown human detector").
- The YuNet and genderage models' licenses ship with the assets; the model files are data `doctor` checks for, not code it imports.
- `doctor` runs its executable probes with a five-second timeout each; a hung `ffmpeg -version` yields a line without a version rather than a hang.
- Ensemble fails to start if *either* detector fails to initialize — a missing Moondream SDK makes `ensemble` refuse entirely rather than degrading to OpenCV alone.

## Open questions and verification

- Where a Photon model download lands on disk, byte-level progress during it, and what the SDK prints if it fails mid-download remain SDK-internal; Dedupe's part — the "Preparing the Photon model…" narration and the `photon-ready.json` marker — is covered by unit tests. The marker can go stale if the user deletes the SDK's own cache; a stale marker only means the narration is skipped once, never a wrong refusal.
- The user-visible result of requesting the faces or no-person review when cv2 cannot import at all (an error message versus an empty category) was not confirmed by hand.
- Whether the UI distinguishes "stage skipped for missing ffmpeg" from "stage ran, zero matches" anywhere beyond the diagnostics warnings was not confirmed.

Verified against dedupe commit `2a6cede`.
