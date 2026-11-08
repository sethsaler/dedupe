# The scan pipeline

## Summary

A scan is one full pass over a set of folders: it finds every media file, fingerprints them, and turns the fingerprints into review groups — exact duplicates, similar images and videos, low-resolution candidates, a random review sample, and optionally no-person and faces candidates. This document owns the order of the stages, every detection threshold, and the rules for what counts as a media file; the experience of starting and watching a scan lives in [Scan setup](../ui/scan-setup.md) (web UI) and [`scan`](../cli/scan.md) (CLI).

## What a scan examines

A scan walks the given folders recursively and picks up three kinds of media by file extension:

- **Images:** `.jpg`, `.jpeg`, `.png`, `.heic`, `.heif`, `.webp`, `.avif`, `.tif`, `.tiff`, `.bmp`, `.raw`, `.cr2`, `.nef`, `.arw`, `.dng`
- **GIFs:** `.gif`
- **Videos:** `.mp4`, `.mov`, `.m4v`, `.avi`, `.mkv`, `.webm`, `.mts`, `.m2ts`, `.wmv`, `.flv`, `.3gp`, `.mpg`, `.mpeg`, `.ts`, `.vob`

AVIF files render natively in the browser, so they need no transcode anywhere. Like the other formats browsers cannot play natively (`.avi`, `.mkv`, `.wmv`, `.flv`, `.mts`, `.m2ts`), the MPEG-1/2 containers (`.mpg`, `.mpeg`, `.ts`, `.vob`) fingerprint and thumbnail normally but the lightbox's video player may not play them; their cards still show a poster frame.

Everything else is ignored silently. Hidden files are skipped unless the scan is told to include them. Each kind can be turned off for a scan (`include_images`, `include_gifs`, `include_videos`), and exclusion globs remove matching paths before anything is read.

Before walking begins, each folder path is expanded (`~` allowed), resolved, and checked. A missing folder produces an error and is skipped rather than aborting the scan. A folder inside a Photos.app library is refused with a message telling the user to export media from Photos first; Photos libraries are never scanned directly.

## The stages, in order

```mermaid
stateDiagram-v2
    [*] --> inventory : folders resolved
    inventory --> cache : media files found
    cache --> hashing : hashes restored where possible
    hashing --> reviewgroups : exact/image/video stages done
    reviewgroups --> humandetection : low-res + random groups built
    humandetection --> facedetection : if no-person was enabled
    facedetection --> done : if faces was enabled
    done --> [*]
```

1. **Inventory.** Walk the folders, count files matching the enabled media kinds. The progress line shows files found so far.
2. **Cache hydration.** The [hash cache](../cross-cutting/caches-and-files.md) is consulted; files whose size, modification time, filesystem change time, device, and inode match a cached entry get their stored hashes back without re-reading the file. Older entries without change time are refreshed once. The progress line shows hits out of total. If the cache cannot be opened, the scan continues without it and says so.
3. **Hashing — three stages at once.** Exact duplicate detection, image/GIF similarity hashing, and video fingerprinting all run concurrently, because they touch disjoint file sets and different kinds of work (disk reads, CPU decoding, ffmpeg subprocesses). Groups are *published* in a fixed order: all exact groups before any similar group, because similar grouping consults which files are already exact duplicates. The expensive hashing never waits; only the publishing does. Progress from all three merges into one combined count and ETA.
3a. **Sets and semantic look-alikes** (only when requested). Every image and GIF is embedded with EmbeddingGemma 2 through DigUp's helper, starting with the hashing stages: embedding runs on the GPU while hashing uses the CPU. Once the image stage has published its groups, the embeddings are grouped into Sets and, for near-identical pairs, Similar look-alike groups. This runs alongside the remaining stages.
4. **Low-resolution dimensions.** Files that still lack dimensions are probed (cheap header reads for images, ffprobe for videos) so low-resolution suggestions can be made. This stage exists only when low-resolution review is enabled, which it is by default.
5. **Review groups.** Low-resolution groups (skipping files with a durable [Keep decision](../glossary.md)) and the random review sample are built and streamed to the UI.
6. **Person detection** (only when the no-person review was requested). Every file is analyzed for people using the chosen [backend](../glossary.md); the default is OpenCV.
7. **Face detection** (only when faces review was requested). Face counting plus gender classification run on images, GIFs, and videos using OpenCV.

After the last stage, everything the scan learned is written back to the hash cache, and the final result — files, groups, errors, and per-stage diagnostics — is assembled.

## How each detection works

**Exact duplicates.** Files are bucketed by size; a file whose size is unique cannot be an exact duplicate and is skipped. Within a bucket, the first 64 KiB of each file is hashed; only files whose partial hashes match go on to a full SHA-256 of the whole file. For files at or below 64 KiB, the first pass already covers every byte, so its digest is reused without reading the file again. Files with matching full hashes form an exact group. Larger files that share a prefix but differ later do not match.

**Similar images and GIFs.** Candidate pairs first pass global and regional comparisons, followed by color, transparency, and local-detail checks. Similar is intended for copies of one photograph. Small changes in expression, pose, or hair can keep two otherwise similar pictures out of the category. A pair must pass every check:

> Technical note: candidate hashing downsizes images to at most 512 px per side and computes pHash and dHash. Regional tile hashes then compare quadrants and the center. These coarse checks are followed by the checks below; a matching hash is not sufficient evidence by itself.

| Check | Default |
| --- | --- |
| Global image pHash | ≤ 6 |
| Candidate pairing dHash | ≤ 10 |
| Aspect-ratio agreement | ≥ 95% |
| Tile pHash, worst tile | ≤ 8 |
| Tile pHash, mean | ≤ 5.0 |
| Color and transparency, each RGBA channel of a 128 px thumbnail | Mean absolute difference ≤ 32 on a 0–255 scale |
| Dense detail, worst 16×16 grayscale block of a 128 px thumbnail | Reject when the block's mean absolute difference > 3.0 **and** worst/mean > 8× |
| Fine local detail | Overlapping smaller patches must agree after accounting for global exposure and supported image alignment |

Pairs that pass the tile check are compared for color and transparency changes. A large average difference in any channel rejects the pair, even when its grayscale hashes are identical. A coarse detail check also rejects a strong, concentrated change. The finer check then looks for smaller differences that those averages can hide, including changes around a mouth, eye, or hair. A global brightness change does not excuse a local change. The comparison allows supported global resampling while retaining local differences; it does not move individual facial features into alignment. If the files cannot supply the comparison evidence, the pair is not grouped. These checks also run on cached scans.

> Technical note: the coarse check uses 128 px thumbnails and an 8×8 grid. Fine-detail frames retain up to 768 px on the longer side; a pair is compared at the lower available resolution. After fitting a global exposure change (scale bounded to 0.5–2), grayscale detail is smoothed with sigma 0.8 and checked in overlapping 7×7, 15×15, and 31×31 windows. Four or more valid window centers at any one size reject the pair when both mean absolute difference exceeds 4 on a 0–255 scale and normalized structural residual exceeds 0.25. Structural residual is the sum of the two local variances minus twice their covariance, divided by the sum of the variances plus 58.5. A whole-frame alignment may compensate for up to 1° rotation and 1.5 px translation at the 128 px registration scale, accepted only when clipped mean error improves by at least 15%; uncertain transformed edges are excluded. The aligned edited-copy verifier shares this local-detail test.

**Rotated and mirrored copies.** A still image whose pixels were turned — rotated 90° or 180°, or mirrored, as happens when an app drops or ignores the EXIF orientation tag, or when a front-camera shot is exported mirrored — is found as a similar copy of its source. Candidate lookup also tries each of the seven other orientations of every still, and a turned pair must then pass the same tile, color, and local-detail checks in the matched orientation (the dHash check applies upright only). Candidate orientations are checked in order of pHash agreement until one passes; a failed turn does not hide another valid one. The group's member card and the swipe deck name the turn ("rotated 90°", "rotated 180°", "mirrored", "flipped upside down", "rotated and mirrored"), and the similarity score is measured in that orientation. Files whose turn is recorded only in the EXIF tag need none of this; they are decoded upright. Animations are only compared upright. Stills cached by an earlier version are read once more on the next scan to add their turned fingerprints; if that read fails, their cached fingerprints are kept and they are only found upright.

> Technical note: candidate lookup splits each 64-bit pHash into seven chunks; two hashes within 6 bits agree exactly on at least one chunk, so candidates come from seven dictionary lookups instead of a BK-tree walk. Searching eight orientations this way costs less than searching one did with the BK-tree.

Animations use the first frame to find candidates, then compare regional detail, color, transparency, and dense detail at eight positions in playback time. Frame order matters: differences at those sampled positions can reject animations with the same opening frame. Equivalent exports can still match when their frame counts differ. Old animation fingerprints are refreshed on the next scan.

Groups are built around the best-ranked member (see [Duplicate group](duplicate-group.md)), never by chaining fuzzy matches transitively. Pairs the user previously marked *distinct* are never regrouped while the decision is current — the matcher and the clustering pass both consult the recorded pairs, which follow the files through renames and metadata-only drift via their perceptual content identity and lapse only on a real content change.

**Sets and semantic look-alikes.** Optional (`--semantic`, or *Sets & look-alikes (DigUp)* in the scan options), and available only when [DigUp](../cross-cutting/optional-dependencies.md#digup-embeddinggemma-2) is installed. The first frame of each image and GIF is rendered upright, downscaled to 896 px, and embedded with Google DeepMind's EmbeddingGemma 2 at a 140-token vision budget. One embedding pass feeds two layers:

- **Sets** (cosine ≥ **0.88**): images of one subject or shoot (burst frames, slides from one deck, a series of a scene), grouped around an anchor image. Sets are a browse review like All files: their own tab, no keeper, no selection rules, and each file is trashed with one click (undoable). Each member shows its similarity to the set's first image ("92% like the first image").
- **Look-alike duplicates** (candidate cosine ≥ **0.97**, followed by local-detail verification): copies the perceptual hashes missed, such as crops, resizes, grayscale edits, and recompression. They join the Similar review labelled "N% look-alike" only when the image comparison also supplies evidence that they are copies. Files a perceptual Similar group holds are left out, and identical copies alone never form a group.

Both candidate cuts are adjustable: in the scan options, ticking the DigUp option reveals a *Look-alike duplicates* slider (95–99.9%) and a *Sets* slider (80–98%); the CLI takes `--semantic-duplicate-threshold` and `--semantic-threshold`. Raising or lowering a score never bypasses the local-detail checks. Embeddings are cached; a rescan reuses them and verifies the qualifying pairs again. When every image is already embedded by the same DigUp helper, a rescan does not start the helper at all, so the model is never loaded. Verification runs on three threads, each taking the candidates of one image at a time.

Embedding a library for the first time keeps the GPU busy for minutes (roughly 0.1 s per image on an M3 Max). So that the Mac stays cool and usable meanwhile, Dedupe starts DigUp's helper at macOS background priority: its work yields to the apps in use and runs at a lower power state, which makes embedding about 1.5× slower than full speed. *Embed at full speed (warmer)* under the DigUp option (CLI: `--semantic-full-speed`) skips the background policy for that scan; thermal pauses still apply. When macOS reports thermal pressure, embedding also idles after each batch, as long again as the batch took under moderate pressure and three times as long under heavy pressure, and the progress line reads *Look-alikes paused while the Mac cools*. Embedding always advances, even on a Mac that stays warm. Cancelling the scan during a pause stops it promptly.

An embedding score measures resemblance and can rate separate photographs very highly. Edited-copy verification aligns the images, then checks the overlapping detail. An explicit local difference rejects the pair, even when a model rates it highly. When edited-copy alignment is unavailable or inconclusive, the ordinary orientation, tile, and local-detail comparison must establish the match. A similar brightness distribution alone cannot do so. Ambiguous crops and heavy edits can therefore be left out of Similar; they may still appear in Sets. Look-alike duplicates **start with nothing selected**, and bulk selection rules (the CLI's `--smart`, the UI's apply-to-all) skip them. Videos are not embedded yet.

> Technical note: look-alike groups carry `match_basis: "semantic"`. Vectors are cached per file identity and model signature in the hash cache file (`semantic_embeddings` table); cached vectors do not bypass the current duplicate verifier. The signature each helper binary (path, size, modification time) last reported is remembered (`semantic_helper_signatures`) so a fully cached rescan can skip the helper; whenever the helper does start, its live signature is used and replaces the remembered one.

**Decider verification.** Optional (`--decider`, which implies `--semantic`, or *Verify look-alikes with Decider* under the DigUp option), and available only when [Decider.app](../cross-cutting/optional-dependencies.md#decider) is running. It adds a model check for candidates the edited-copy verifier cannot decide. The ordinary local image comparison must still establish a match. A positive model answer cannot override a detected local difference or supply missing image evidence; a negative answer can reject the pair. If Decider cannot answer or stops during the scan, qualifying pairs still need the local image comparison. Sets remain available for separate photographs of the same subject or shoot.

> Technical note: Decider asks whether the pair is one photograph, separately taken photographs of one scene, or different scenes. Its P(same) threshold is **0.8**. It only judges embedding candidates and caches verdicts by pair identity and model (`decider_verdicts`); cached verdicts remain subject to the current local-detail checks.

**Similar videos.** Each video is fingerprinted by sampling up to **8** positions along its timeline and hashing one small grayscale frame per position, extracted with direct ffmpeg seeks rather than decoding the whole timeline. The video stream's duration is preferred over the container duration, so a longer audio track does not push sample positions beyond the picture. Two videos are similar when their fingerprints agree at normalized positions within a mean Hamming distance of **8**, no aligned frame distance exceeds **16** at the default threshold, and the shorter duration is at least **90%** of the longer when both are known. Audio is not compared. A failed fingerprint extraction is reported in diagnostics, and a failed refresh cannot reuse an older fingerprint to form a group. Undecodable sample frames invalidate a fingerprint; malformed cached fingerprints are recomputed. Videos require ffmpeg; without it the stage is skipped with a warning in the diagnostics.

**Low resolution.** A file is a low-resolution candidate when its display dimensions are below a megapixel bound — **1,000,000 pixels** by default, configurable per media type. Candidates are unselected until reviewed; reviewing one and leaving it unselected stores a durable [Keep decision](../glossary.md) so future scans stop resurfacing it.

**Random review.** A fresh, unique sample of up to **50** scanned images, GIFs, and videos, for spot-checking the scan's judgment. The count is configurable down to zero.

**No person.** Offline person detection on images, up to 16 GIF frames, and up to 16 directly-seeked video frames, with early exit on positive evidence. Video frames are extracted in chunks of four per ffmpeg process (at most four processes per video) instead of one process per frame. Backends: `opencv` (default; bundled YuNet face model first at a recall-first 0.35 presence threshold, with a 320 px close-up scale, a mirrored pass, and 2×2 overlapping tiles, then INRIA and Daimler full-body HOG), `photon` (opt-in, roughly 10 GB model download on first use; queries woman / girl / person / face), `ensemble` (OpenCV first, Photon on uncertain frames). Photon and ensemble no-person piles are re-checked with YuNet + genderage; any counted face, especially a female face, is kept. When face counting is also enabled, Non-Human groups are built after those counts land so the same veto applies. The review is conservative and fails closed: if the YuNet model is missing, corrupt, or cannot start, no media is surfaced as Non-Human. Photo detail is retained for the tiled face checks so small, distant faces are not lost by shrinking the whole photo first. Older no-person decisions are re-analyzed on the next scan; manual human confirmations remain in force. See [No-person review](../ui/no-person-review.md).

**Faces.** OpenCV counts faces (and classifies them male/female) in images, GIF frames, and duration-scaled 4–16 sampled video frames (about one frame per five seconds, extracted in a single ffmpeg pass). Images already at or below the second detection pass's 480 px scale skip that redundant pass. Files with at least one face become faces candidates, ordered by face count. Face counting is heuristic and can miscount; the UI says so where deletion is offered.

## One pool vs parallel streams

A scan over several folders can run two ways:

- **One pool (UI and CLI default).** All folders merge into a single inventory; duplicates are found both within and *across* folders. Repeated file paths, overlapping folders, and hardlinks are inventoried once per physical file, including when a file and its containing folder are both selected.
- **Parallel streams.** Each folder runs its own complete pipeline at the same time. There is no cross-folder deduplication: a group contains files from exactly one folder, and every group carries its source folder. The UI scans this way when parallel streams are requested; per-folder progress is shown separately plus one aggregate line. Low-resolution and random groups are still built over the combined result at the end.

## Streaming and progress

Groups stream to the UI the moment each one is finalized — the sidebar fills in during the scan rather than all at once at the end. The group list stays sorted with the most reclaimable space first. Progress reports include the current phase, files found and processed, groups found, elapsed time, and an ETA computed from the current phase's rate. The final message summarizes counts per category.

## Cancellation and failure

Cancellation is checked between stages and inside every worker pool; a cancelled scan stops with phase `cancelled`, keeps whatever groups were already published, and writes nothing further — except the hash cache: the work each stage completed before the cancel is persisted as the cancel propagates, so a rerun reuses it. A scan is [safe to interrupt](../glossary.md) until an action runs against its results.

A single corrupt or unreadable file never aborts the scan; this is deliberate, because media libraries always contain damaged files. Each failure is recorded on the file's record and summarized per stage in the result's diagnostics (attempted, succeeded, failed, skipped, duration, up to ten warnings per stage). Missing ffmpeg downgrades the video stage to a warning; an unavailable cache downgrades to running without one. Root-level problems (missing folder, Photos library) are reported as errors in the result.

## Interactions with other systems

**Files on disk.** The scan reads media files but never modifies them. It reads and writes only the hash cache; see [Files Dedupe writes](../cross-cutting/caches-and-files.md).

**Safety and undo.** None directly — scans propose, [actions](actions-and-undo.md) dispose. Nothing about a scan moves or deletes anything.

**Review sessions.** A scan's results are what a [review session](review-session.md) stores selections against; a new scan replaces the previous results. After an update to matching, run a new scan to reassess earlier groups. Resuming a saved review checks file identity and restores its groups; it does not rerun similarity classification.

**Optional dependencies.** ffmpeg/ffprobe are required for video similarity and video dimension probes; OpenCV for the no-person and faces reviews; Photon only when chosen as a backend. Degraded behavior per dependency is collected in [Optional dependencies](../cross-cutting/optional-dependencies.md).

**Concurrency and resource limits.** Worker counts default to "auto": one fewer than the CPU count (all cores on 2-core machines), capped at 8 overall, with per-stage caps — 4 for exact hashing, 6 for image hashing, 4 for video fingerprinting, 4 for OpenCV detection, 2 for preparing images for DigUp's helper, 3 for verifying look-alike candidates. Photon runs serially. Explicit worker settings are honored, then clamped to the stage cap.

**macOS specifics.** Photos.app libraries are detected and refused at root validation. HEIC/HEIF are first-class image types.

**Configuration and defaults.** The global image and video hash thresholds and the low-resolution pixel bounds are configurable per scan. The secondary dHash, tile, color, and dense-detail checks use internal defaults; they are not separate scan controls. The values in this document are the defaults and the ones the rest of the documentation refers to.

## Edge cases

- Scanning the same folder twice reuses cached fingerprints, but candidate pairs still undergo the current local-detail checks. Cache reuse does not mean those comparisons are skipped.
- A same-size file with a different prefix exits the exact funnel after the 64 KiB partial hash. Matching prefixes on larger files still require a full SHA-256; a matching prefix alone never proves an exact duplicate.
- Zero-byte files are excluded from exact candidacy (`size > 0` is required).
- Similarity requires at least two eligible files of a kind; a single image or video is never compared against itself.
- In parallel-stream mode a duplicate that exists once in each of two folders is not reported; turn off **Scan folders separately** in the UI or omit `--parallel` in the CLI to compare the selected folders together.
- A scan of a folder with no media files completes successfully with zero groups.
- Exclusion patterns are matched during the walk; an excluded folder's contents are never opened.

## Open questions and verification

- The exact worker caps and auto formula are read from `parallel.py`, not confirmed by measuring a real scan's core usage.
- The UI defaults to one pool; explicit independent streams and saved-setting migration are covered by browser regressions.
- The dHash pairing threshold (10) is internal to candidate generation; users only ever set the global threshold (6/8). Whether the UI should surface the distinction is a product question.
- The color limit is verified against generated color collisions, the checked-in photo fixture's re-exports, and moderate exposure changes, not a labeled sample of a user's full library. Strong edits can intentionally stop matching.
- Fine-detail regression cases use synthetic changes to the checked-in photo fixture. They are not measurements of accuracy on a user's real burst photographs. Small or blurred faces and low-resolution sources may not contain enough surviving detail to distinguish every expression or hair change. Ambiguous edited copies can be missed intentionally.
- Video fingerprints ignore color and normalize frames to a square. An aspect-ratio gate would first need display dimensions that account for rotation and non-square pixels. Video and animation changes between sampled positions can still be missed. Short videos with slightly different durations can also receive different sampling grids.

Verified against the post-improvement working tree (pinned at `2a6cede` plus the 2026-09 improvement phases; see the repository README for the commit).
Folder scope, overlapping inputs, cache change-time validation, image/animation verification, and video error handling covered by regressions against the 2026-10-29 working tree. The eight-pair generated smoke corpus has no false positives and one missed crop; this is not a measurement of full-library accuracy.
Fine-detail matching and conservative semantic verification descriptions updated for the 2026-11-06 working tree; live review of a labeled personal photo library remains unverified.
Look-alike scheduling, background priority, and thermal pauses updated for the 2026-11-08 working tree. The 1.5× figure was measured on one M3 Max with synthetic images; thermal pauses are covered by regressions with a simulated pressure level, not by a measured hot Mac.
