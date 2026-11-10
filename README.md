# Dedupe

Local **Gemini-style** finder for **duplicate and near-duplicate** images, videos, and GIFs.

Point it at a folder, scan recursively, review groups in a browser UI, then move extras to **Trash** or a **quarantine folder**.

## Features

- **Exact duplicates** — size → partial hash → SHA-256
- **Similar media** — perceptual hashing plus local-detail verification for images/GIFs, including small expression, pose, and hair changes; ffmpeg frame sampling for videos
- **Protected libraries** — compare cleanup folders against an archive whose files cannot be removed or moved; use **Compare against a protected library** or repeat `--protect-folder PATH`
- **Video clips & versions** — optional `--video-versions` pass finds overlapping footage and compares sampled audio; approximate matching ranges align pair playback, and these suggestions require manual selection
- **Sets and semantic look-alikes** — optional layer that embeds images and GIFs with Google DeepMind's EmbeddingGemma 2, run through [DigUp](https://github.com/ARahim3/DigUp)'s installed helper (no extra Python deps). A **Sets** tab groups images of one subject or shoot for browsing and one-click trash. Crops, flips, and re-saves the hashes missed can join Similar after local-detail verification, with a "% look-alike" score (never preselected): `dedupe scan --semantic`, or *Sets & look-alikes (DigUp)* in the scan options. Add `--decider` (or *Verify look-alikes with Decider*) for an additional model check; a model score alone cannot establish a duplicate or override a detected local difference
- **Low-resolution review** — surfaces images, GIFs, and videos below configurable per-type megapixel bounds as independent deletion suggestions
- **Random 50 review** — every scan draws a fresh sample of up to 50 media files for a fast Keep/Delete check with the arrow keys
- **Non-Human media** — optional OpenCV review surfaces images, GIFs, and sampled videos where no person was detected; one click (or `d`) moves a file to Trash, with Undo on the toast
- **Fewest people first** — sort or filter any Files, Non-Human, or Faces review by how likely each file is to show a person, so the likeliest deletions come first. Scores come from a bundled YOLOX-tiny person detector, are computed on demand for the open folder, and are cached; `dedupe rank-people PATH…` does the same from the terminal
- **Face counts** — optional OpenCV pass counts faces in images and GIFs; every file card shows its count, files with detected faces get their own Faces review tab (busiest shots first), and a bulk rule ("at least … faces") selects group-photo shots in one click
- **Smart Select** — automatic keep (best resolution/size/date) plus keep newest/oldest/largest/etc.
- **Safe actions** — Trash (macOS-recoverable) or move to a quarantine folder; dry-run previews; act on Exact, Similar, or Non-Human separately or all at once
- **Scan cache** — `~/.cache/dedupe/hashes.sqlite3` reuses hashes and completed OpenCV person checks for unchanged media, hydrated in batched queries so large libraries start fast
- **Thumbnail cache** — previews are kept on disk under `~/.cache/dedupe/thumbnails/` and pruned least-recently-used against a size budget
- **Saved cleanup jobs** — named folders/settings with separate resumable reviews and **Check new arrivals**, which focuses results on new or changed files while comparing against the full library
- **Resumable reviews** — the last completed review and each saved job's selections are saved atomically under `~/.local/state/dedupe/` and revalidated when resumed
- **Scan quality report** — stage timings, cache hits, failures, skips, and dependency warnings make incomplete analysis visible
- **Local web UI** — search/sort/filter, advanced filters, bulk selection, similarity presets and explanations, a lightbox with metadata, keeper overlay/flicker comparison, full-resolution zoom, and in-place selection, keyboard navigation, native picker, dependency-gated scan options, light and dark themes (follows the OS), and isolate
- **Preview-first actions** — Trash and quarantine always run preflight before their final confirmation; the review sheet shows category counts, affected bytes, and how long the preview stays valid
- **Action receipts** — every executed action and dry-run preview writes a JSON receipt you can list, inspect, prune, and undo from the CLI
- **Permanent History** — browse earlier cleanups, restore available files after restarting, and reconsider remembered **Not duplicates** pairs
- **Interrupted cleanup recovery** — move intentions and outcomes are recorded durably as work proceeds; History reconciles interrupted batches without moving files and flags uncertain outcomes

## Requirements

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/) + ffprobe (for video similarity and video thumbnails)
- macOS recommended (Trash + `open -R` reveal); Linux works for scan/quarantine

```bash
# macOS
brew install ffmpeg
```

## Install

### Install or update from GitHub

On macOS or Linux, run this same command for both the first install and future updates:

```bash
curl -fsSL https://raw.githubusercontent.com/sethsaler/dedupe/main/install.sh | bash
```

The installer requires Git and Python 3.11+, checks out the public repository to
`~/.local/share/dedupe`, creates an isolated virtual environment, and links the
`dedupe` command into `~/.local/bin`. It includes the OpenCV Non-Human detector;
the much larger Photon model remains opt-in. The updater only fast-forwards a
clean installer-managed checkout, so it will not discard local changes.

If `~/.local/bin` is not already on your `PATH`, add it to your shell profile:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Then start the app with `dedupe ui`. On macOS, you can also double-click
`~/.local/share/dedupe/Dedupe.command` in Finder. Install ffmpeg separately for
video similarity and thumbnails:

```bash
brew install ffmpeg
```

### Install from a local checkout

```bash
cd dedupe
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Optional Non-Human detection (OpenCV)
pip install -e ".[human]"

# Optional Photon / Moondream backend (includes OpenCV for ensemble mode)
pip install -e ".[vision]"
```

## Quick start

### Web UI

```bash
dedupe ui
# → http://127.0.0.1:8765
```

**macOS double-click:** open `Dedupe.command` (repo root) or `launchers/Dedupe.command`.  
That starts the local server, opens your browser, and keeps a Terminal window for logs / Ctrl+C.

1. Paste a folder path (e.g. `~/Pictures`) or choose **Folders…** / **Files…**. To clean an inbox against an archive, put the inbox here and add the archive under **Compare against a protected library**.
2. Configure optional exclusion globs, then hit **Scan** — review groups stream into the sidebar
   With Exact detection enabled (the default), byte-identical copies appear on the **Exact** tab for review; nothing moves until you confirm. **Delete All Exact Duplicates** marks every copy except each group's keeper and opens the confirmation sheet; **Delete All Selected Exact Matches** (`a`) trashes only what you marked.
3. Choose a category across the top. **Similar** opens on the **Similar board**: every similar group as one row, with the suggested keeper first. Standard similar copies start marked Remove; semantic look-alikes and video-version suggestions require manual selection. Click a photo (or press `Space`) to keep it, press `n` on a row that isn't really a duplicate (undo from the row), then move every marked copy to Trash with one confirmed batch that a single Undo restores. **Compare ↗** opens any group in the side-by-side swipe review — **Same · Trash copy**, **Different · Keep both**, Skip, and Undo, with videos playing and scrubbing in either pane — and **▦ Board** comes back.
4. In **Non-Human**, **Faces**, and **All files**, switch between a resizable **Gallery** and single-file **Focus**. `←` / `→` navigate Focus without making a decision; Trash is immediate and undoable. Layout and preview size are remembered. **File details** reveals full paths, dates, and detection evidence.
5. Review **Low-res** and **Random** one item at a time with `←` Delete and `→` Keep, including in the expanded lightbox. These decisions only stage removals; confirm with the Low-res + Random action to move selected files to `_Dedupe Quarantine`.
6. Open **Filter & organize** for advanced filters (size, dimensions, path, faces) and bulk selection. A group matches when any of its files match. Bulk selection applies only to the groups currently shown.

Click an image or **Expand ↗** for the full-screen lightbox. Images support full-resolution
zoom and panning, videos have native playback controls, and GIFs retain their animation.
The review stage fits its media and decision controls to the available window height.

Bulk selection is re-derived on the server, so duplicate groups always keep their suggested
keeper no matter what the browser asks for.

The confirmation sheet counts down how long its preview stays valid. If the preview lapses
while the sheet is open, the execute is never attempted with a stale token: the selection is
re-verified automatically and you confirm the refreshed numbers.

After an app restart the page starts clean at the scan setup — the last scan's results are
not loaded. A banner offers **Resume saved review** to pick the previous review back up
(groups, selections, and review state); use **Discard saved review** to clear the saved
session instead. Changed, missing, or out-of-root files are removed from a
resumed review before it is shown, and every file is still revalidated immediately before an
action. The resumed-session banner reports how many files were pruned and why (no longer on
disk, changed since the scan, outside the scanned folders, became a symbolic link, could not
be read), with a "What was dropped?" list of up to 20 example files.

For recurring work, open **Saved cleanup jobs**, enter a name, and **Save job**.
Selecting a job restores its folders and settings; **Resume review** restores its
own saved progress. **Check new arrivals** compares against all current files but
shows duplicate groups involving new or changed files and new independent review
candidates. It requires an initial completed scan. Changed job settings require
another full scan before checking arrivals. **Delete job** removes only that job
and its saved review, not media.

Open **History** to browse completed or interrupted cleanups, even before loading
a scan. **Restore files…** verifies the recorded destinations and original paths,
then asks for confirmation. Unavailable or changed files can block restoration.
**Reconsider this pair** removes a remembered Not duplicates decision so those
files can match on the next scan. Recovery never guesses which file moved or
automatically restores uncertain outcomes.

Keyboard:

| Key | Action |
| --- | --- |
| `j` / `↓` | Next group |
| `k` / `↑` | Previous group |
| `[` / `]` | Previous / next group needing attention |
| `←` / `→` on tabs | Switch result category (tabs also take `Home` / `End`) |
| `u` | Use the suggested selection for this group |
| `s` | Apply the selection rule to this group |
| `a` / `A` | Open the action review sheet (preview trash) for exact / similar selections |
| `Space` | Toggle remove on the focused card; in the lightbox, toggle removal for exact/similar files |
| `Enter` | Open the lightbox |
| `←` / `→` | Delete / Keep in Low-res and Random 50 review; otherwise focus previous / next card or navigate the lightbox |
| `d` | Trash the focused Non-Human/Faces file; in an exact/similar lightbox, toggle removal |
| `z` | Full-resolution zoom in the lightbox (drag pans) |
| `Esc` | Close the lightbox, help, or overlay |
| `?` | Shortcut help |

On the Similar board, `j` / `k` move between rows, `←` / `→` between files, `Space` keeps or removes, `Shift+K` keeps only the focused file, `u` restores the suggestion, `n` marks the row not duplicates, `Enter` opens the lightbox, `c` opens the swipe compare, and `A` moves every marked copy to Trash.

### CLI

```bash
# Scan and summarize
dedupe scan ~/Pictures

# Write full JSON results
dedupe scan ~/Pictures ~/Downloads --json results.json

# Exact only (faster)
dedupe scan ~/Movies --no-similar

# Clean Downloads against an archive that must remain untouched
dedupe scan ~/Downloads --protect-folder ~/Pictures/Archive --ui

# Include trimmed/offset video versions for manual review
dedupe scan ~/Movies --video-versions --ui

# Surface non-human media where OpenCV detected no person, for manual review
dedupe scan ~/Pictures --find-no-person --ui

# Run the same review with Photon, or use OpenCV-first ensemble mode
dedupe scan ~/Pictures --find-no-person --human-backend photon --ui
dedupe scan ~/Pictures --find-no-person --human-backend ensemble --ui

# Skip exports and cache folders
dedupe scan ~/Pictures --exclude 'exports/**' --exclude cache

# Parallel hashing (default: auto = CPU count; 1 = serial)
dedupe scan ~/Pictures --workers 8

# Stricter similarity (0 = almost exact visual match)
dedupe scan ~/Pictures --threshold 4

# Dry-run trash selection
dedupe scan ~/Downloads --action trash --dry-run

# Isolate matches into review folders *inside the scanned source*
dedupe scan ~/Pictures --action isolate --execute
# → ~/Pictures/_Dedupe Review/session-<timestamp>/exact/… and …/similar/…

# Only exact matches (still under the source by default)
dedupe scan ~/Pictures --action isolate --isolate-kinds exact --execute

# Re-use a previous scan JSON (defaults to that scan's root/_Dedupe Review)
dedupe isolate results.json --execute

# Override only if you really want a different location
dedupe isolate results.json --review-dir /some/other/path --execute

# Quarantine onto another volume (copy, verify, then delete the original)
dedupe scan ~/Pictures --action quarantine --quarantine-dir /Volumes/Backup/dupes \
  --allow-cross-device --execute

# Same flag for `--isolate-mode move` across volumes
dedupe isolate results.json --isolate-mode move --allow-cross-device --execute

# Browse action receipts (newest first)
dedupe receipts list
dedupe receipts list --limit 50 --no-previews --undoable
dedupe receipts list --json

# Inspect one receipt by id, filename, path, or unique id substring
dedupe receipts show action-20260718T101500.482913Z-1a2b3c4d
dedupe receipts show 1a2b3c4d --items 0

# Delete old receipts (dry-run preview unless --execute)
dedupe receipts prune --older-than 30
dedupe receipts prune --keep 50 --drop-previews --execute

# Restore a quarantine action from its receipt id or path (preview first)
dedupe undo action-20260718T101500.482913Z-1a2b3c4d
dedupe undo ~/.cache/dedupe/logs/action-<timestamp>-<id>.json --execute

# Open UI with last scan results
dedupe scan ~/Pictures --ui

# Check dependencies, optional detectors, and writable app paths
dedupe doctor
dedupe doctor --json
```

### Isolate for human review

When exact or similar groups are found, isolate builds a review tree **inside the scanned source folder** (never Desktop or the dedupe repo by default):

```
<your scanned folder>/
  photo1.jpg
  photo1_copy.jpg
  …
  _Dedupe Review/          ← created here, next to the media
    session-20260718T…/
      exact/
        001_exact_image_n2_photo_abc123/
          KEEP__photo.jpg      ← suggested keep
          photo_copy.jpg
          _group.json          ← sources + metadata
          README.txt
      similar/
        001_similar_image_n2_…
      _review_index.json
```

`_Dedupe Review` is skipped on future scans so review copies are not re-detected.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--isolate-mode copy` | yes | Copy files into review folders (safe) |
| `--isolate-mode hardlink` | | Same inode and no extra disk use; editing either name edits the same file |
| `--isolate-mode symlink` | | Symlinks back to originals |
| `--isolate-mode move` | | **Moves** originals into review (destructive layout) |
| `--isolate-kinds all\|exact\|similar` | `all` | Filter which groups to isolate |
| `--review-dir PATH` | `<scan root>/_Dedupe Review` | Override (optional) |

Requires `--execute` to write folders (otherwise dry-run only).

## How detection works

| Kind | Method |
| --- | --- |
| Exact | Same size → matching first 64KB hash → matching full SHA-256 |
| Similar images/GIFs | Global pHash + dHash candidates, then regional tile pHash, color/transparency checks, and finer local-detail verification for small expression/pose changes (default Hamming ≤ 6, tile max ≤ 8) |
| Similar videos | Ordered pHashes from direct ffmpeg timeline seeks, compared at normalized positions (default mean Hamming ≤ 8) |
| Video clips & versions (opt-in) | Denser sampled footage alignment with approximate overlapping ranges and sampled-audio evidence; always manual review |
| Low resolution | Display-oriented dimensions below the pre-scan megapixel bound for that media type (1 MP by default); candidates remain unselected until reviewed |
| Random review | A fresh, unique sample of up to 50 scanned images, GIFs, and videos; Keep/Delete decisions are staged for the normal preview-and-confirm action flow |
| No person detected | Offline OpenCV YuNet face (recall-first: 0.35, extra close-up scale, mirrored + tiled passes) + INRIA/Daimler HOG on images, representative GIF frames, and up to 16 direct-seek video frames with positive-evidence early exit |

The no-person review can use `opencv` (fast default), `photon` (Moondream 3.1 through the local Photon runtime), or `ensemble` (OpenCV positives first, then Photon on uncertain frames). Photon stays opt-in: its first use can download roughly 10 GB of model weights, and it queries `woman`, `girl`, `person`, and `face`. All processing remains local after the model is available. The scan setup UI exposes the backend whenever Non-Human is enabled.

“No person detected” is a conservative computer-vision-assisted review filter, not a guarantee. It is opt-in and leaves non-human files unselected until you review them manually or apply **Mark reviewed + select non-human**. OpenCV runs the bundled YuNet face model before its full-body detectors; if the face model is missing, corrupt, or cannot start, the scan fails closed and surfaces no media as Non-Human. A counted face — especially a female face — vetoes Non-Human membership even if the person detector said no. The UI shows how many frames were analyzed. Obscured or unsampled people can still be missed. OpenCV is an optional, CPU-only dependency and does not download a model at runtime.

The bundled YuNet model comes from the official OpenCV Model Zoo. Its MIT license is included at `src/dedupe/assets/LICENSE-YUNET.txt`.

### People likelihood (least likely first)

The Non-Human review answers yes/no. To *order* any set of files with the likeliest
deletions first, Dedupe also computes a 0–1 **people score**: the highest "person"
confidence from the bundled YOLOX-tiny COCO detector (Apache-2.0, Megvii; license at
`src/dedupe/assets/LICENSE-YOLOX.txt`), run once per image at 512 px through OpenCV
(videos: the maximum over five sampled frames; GIFs: first/middle/last). In the web UI,
choose **Fewest people first** in the Files, Non-Human, or Faces sort menu, or narrow the
cards with **Probably no people** (score ≤ 5%) / **Maybe no people** (≤ 20%). The open
folder is scored on demand with progress in the pager; scores are cached per file and
reused until the file changes. Nothing is ever selected or removed because of a score.

```bash
dedupe rank-people ~/Pictures/Downloads --limit 100        # least likely first
dedupe rank-people ~/Pictures/Downloads --max-score 0.05 --json
```

Measured on a labeled COCO val2017 sample (500 images to tune on, 500 held out, half
with people from frame-filling to tiny and distant), ranking people-free files first:

| Scorer | Held-out AP | People-free files before the first person | Time per photo |
| --- | --- | --- | --- |
| Existing OpenCV presence detector (binary) | 0.56 | 0 | 0.09 s |
| YOLOX-S @ 640 px | 0.984 | 93 / 250 | 0.46 s |
| **YOLOX-tiny @ 512 px (shipped)** | **0.988** | **172 / 250** | **0.18 s** |

At ≤ 5%, the filter kept about 80% of people-free photos, and 2–4% of what it kept
contained a person, all of them tiny and distant. Larger inputs, tiling, mirrored
passes, int8 weights, and fusing face/HOG/segmentation signals did not improve held-out
ranking. Rerun the measurement on your own labeled media with the same manifest format
as `benchmark-humans`:

```bash
dedupe benchmark-people benchmark.json --json people-rank.json
# Recreate the COCO corpus used above (downloads ~400 MB):
python scripts/create_people_corpus.py /tmp/people-corpus
dedupe benchmark-people /tmp/people-corpus/test.json
```

### Benchmark Photon against your own media

Use a hand-labeled JSON manifest. The sample names below are illustrative; supply your own
private media. Relative media paths are resolved from the manifest folder:

```json
[
  {"path": "samples/family-photo.jpg", "has_person": true},
  {"path": "samples/empty-room.jpg", "has_person": false},
  {"path": "samples/walkthrough.mov", "has_person": true}
]
```

```bash
# OpenCV baseline only; no Photon download
dedupe benchmark-humans benchmark.json --json benchmark-opencv.json

# Side-by-side comparison; first Photon run may download model weights
# Requires: pip install -e ".[vision]" and network access for the first ~10 GB download
dedupe benchmark-humans benchmark.json \
  --backends opencv photon ensemble \
  --json benchmark-all.json
```

The terminal report includes person recall, no-person precision, accuracy, runtime, and every false-negative path. For this workflow, prioritize **person recall** and inspect every listed missed-person file before deciding whether Photon is safe enough for your library. The JSON output also includes per-file decisions, sampled-frame counts, evidence scores, errors, and latency.

### Benchmark similarity against labeled pairs

Similarity manifests label pairs rather than individual files. Relative paths resolve from
the manifest folder:

```json
{
  "pairs": [
    {"path_a": "samples/original.jpg", "path_b": "samples/reexport.jpg", "similar": true},
    {"path_a": "samples/pose-a.jpg", "path_b": "samples/pose-b.jpg", "similar": false}
  ]
}
```

```bash
dedupe benchmark-similarity similarity-benchmark.json \
  --threshold 6 --video-threshold 8 \
  --json similarity-report.json
```

The report prioritizes false positives, then false negatives, and includes precision,
recall, runtime, errors, and per-pair decisions. Use representative private media; the
repository does not ship personal benchmark photos. For cleanup safety, optimize Similar
matching for low false-positive rates before increasing recall.

Crop and Not-duplicates reviews in the UI are also logged locally (see
[Files and caches](#files-and-caches)) with the suggestion or pair distances shown at
decision time, including skips, undos, and an optional reason on a skipped crop:

```bash
dedupe decisions summary                       # crop acceptance by kind/signature/level, skip reasons, distinct-pair distances
dedupe decisions export-benchmark pairs.json   # marked-distinct pairs as a benchmark manifest (negatives only)
dedupe decisions export-crops crops.json       # standing applied/skipped/undone crop verdicts
```

Only pairs actually decided together export by default; `--include-group` and
`--include-backfill` add the weaker whole-group and never-shown-together labels. These
are your own review decisions, not independent ground truth.

**Near-identical only** — Similar is intended for copies of one photograph at different
quality, export, or resolution. After coarse hashing, overlapping fine-detail patches
check for small expression, pose, and hair changes. Global exposure changes are accounted
for so they cannot hide a local difference. Semantic search applies the same local-detail
requirement after aligning edited copies; subject similarity alone belongs in **Sets**.
Ambiguous pairs can be left out of Similar, including some heavily edited true copies.

These checks run again on cached scans. After updating, run a new scan to reassess old
matches; **Resume saved review** restores the earlier groups without reclassifying them.
Keep using the image comparison before removal. Synthetic changes to the repository's
photo fixture check regressions, but do not measure accuracy on your own burst photos,
small faces, or low-resolution images.

**Video clips & versions** adds a separate review pass for trims, offsets, and
soundtrack changes. It samples at most 96 video positions and requires at least
two seconds and 80% coverage of the shorter video to agree. Matching ranges are
approximate; short excerpts inside long recordings, heavy edits, and speed changes
can be missed. Audio labels describe sampled audio, not a full soundtrack proof.
Review both versions before selecting a removal. See the [cleanup workflow](docs/cleanup-workflow.md).

Semantic analysis can be cancelled while waiting for its helper, and stalled
requests time out. Completed batches remain cached. **Retry analysis using cached
work**, in Scan quality, reruns the last scan settings and reuses completed work.
Content-based embedding reuse covers renamed or moved files; cached neighbor
comparisons avoid recomputing unchanged pairs when new files are added. A broader
similarity threshold or changed model can require comparison work again.

### Parallelism & resource limits

Hashing stages run in a **bounded** thread pool so large libraries don’t pin every core or thrash disk/RAM:

| Setting | Default | Cap |
| --- | --- | --- |
| `--workers N` | auto (`min(cpu−1, 8)`) | overall budget; `1` = serial |
| Exact SHA-256 | ≤ budget | max **4** concurrent full-file reads |
| Image pHash | ≤ budget | max **6**; images downscaled ≤512px before hash |
| Video fingerprints | ≤ budget | max **4** concurrent direct-seek ffmpeg jobs (each `-threads 1`) |
| OpenCV person detection | ≤ budget | max **4** thread-local detectors; Photon/ensemble remain serial |

Also:

- Futures stay windowed (~2× workers in flight) so 50k files don’t allocate 50k tasks at once
- Image decode uses Pillow `draft()` + thumbnail so 12MP HEIC/JPEG never hold full-res RGB
- Video fingerprints fast-seek directly to 32px grayscale samples; person detection uses separate 640px direct seeks and stops decoding after positive evidence
- The scan cache means re-scans skip hashes and person checks for unchanged media, including files renamed on the same filesystem; new, replaced, or modified files are analyzed normally

For a laptop-friendly scan of a huge folder: `dedupe scan ~/Pictures --workers 2`.

## Files and caches

| Path | Contents |
| --- | --- |
| `~/.cache/dedupe/hashes.sqlite3` | Scan cache: hashes, video fingerprints, and completed person checks |
| `~/.cache/dedupe/thumbnails/` | Disk-backed grid and lightbox thumbnails, pruned least-recently-used against a 512 MB budget |
| `~/.cache/dedupe/logs/` | Action receipts, plus `.journal` move events until completion or reconciliation; preview receipts use `preview-*.json` |
| `~/.local/state/dedupe/review-session.json` | The resumable review (result + selections); honours `XDG_STATE_HOME` |
| `~/.local/state/dedupe/cleanup-jobs/` | Named job settings, scan baselines, and each job's saved review; honours `XDG_STATE_HOME` |
| `~/.local/state/dedupe/decision-log.jsonl` | Append-only crop and Not-duplicates decisions with the detector output shown at decision time, for detector tuning; honours `XDG_STATE_HOME`, overridable with `DEDUPE_DECISION_LOG_PATH` |

`DEDUPE_THUMBNAIL_CACHE_DIR` relocates the thumbnail cache and
`DEDUPE_THUMBNAIL_CACHE_BUDGET` sets its budget in bytes. Receipts are pruned with
`dedupe receipts prune`. Thumbnail and analysis data can be regenerated, but
deleting the hash database also forgets remembered Not duplicates and person-review
decisions stored there.
Keep receipts and pending `.journal` files while you may need to restore or inspect
a cleanup. Deleting a receipt removes its History entry and restoration record.

## Safety

- Never hard-deletes in the UI
- Always leaves at least one file per group
- File identity, scan-root containment, and exact hashes are revalidated before execution
- Protected library files cannot be selected for removal or moved, including through isolate-move
- File and directory symlinks are skipped by default
- Executed actions receive unique atomic receipts under `~/.cache/dedupe/logs/`, named `action-<stamp>-<session>.json`; dry-run previews are written alongside them as `preview-<stamp>-<session>.json`
- Trash and quarantine receipts can restore files through History or `dedupe undo <receipt-id|path>` when their destinations remain available; unresolved Trash locations require Finder
- Destructive moves require a writable journal before starting; interrupted actions are reconciled from recorded intent and file identity, with uncertain outcomes left for inspection
- `dedupe receipts list / show / prune` browses and trims that history without touching the files themselves
- Mutating localhost API calls require a per-launch session token and current scan generation
- Trash and quarantine execute only after a fresh preview and confirmation in the UI
- Photos.app `.photoslibrary` packages are never entered or accepted as scan roots; export media from Photos to a normal folder first

### Photos.app libraries

Dedupe deliberately does not manipulate a Photos library package. In Photos.app, select the
assets to review and use **File → Export** to a normal folder, then scan that export. This
keeps Photos metadata and library ownership under supported Apple workflows. Direct library
integration remains out of scope until it can use a supported Apple API end to end.

### Optional macOS application bundle

From the repository root of a local checkout, build a Finder-launchable wrapper around the existing installation:

```bash
scripts/build-macos-app.sh
open build/Dedupe.app
```

The ignored `build/Dedupe.app` is a launcher, not a self-contained Python distribution.
Developer ID signing and notarization are explicit, credential-gated release steps; see
[`packaging/README.md`](packaging/README.md).

## Project layout

```
src/dedupe/
  engine.py          # orchestrates a full scan
  exact.py           # byte-identical groups
  similar_image.py   # perceptual image/GIF groups
  similar_video.py   # video fingerprints
  human_detection.py # optional local person detection
  people_score.py    # 0–1 people likelihood (YOLOX-tiny) for least-likely-first order
  human_benchmark.py # labeled detector comparison + people-ranking harness
  similarity_benchmark.py # labeled near-duplicate pair benchmark
  review_session.py  # atomic resumable review storage
  jobs.py            # named cleanup jobs and new-arrival review
  parallel.py        # thread-pool map for hashing stages
  grouping.py        # ranking + smart select
  actions.py         # trash / quarantine
  receipts.py        # receipt discovery, inspection, and pruning
  action_journal.py  # durable move events and interruption reconciliation
  history.py         # past cleanups and remembered pair decisions
  cache.py           # SQLite hash cache
  cli.py             # `dedupe` entry point
  web/               # Flask UI, native picker, and media previews
```

## Tests

```bash
pytest

# Browser workflow (requires Playwright Chromium)
python -m playwright install chromium
pytest -m e2e
```

Normal `pytest` runs exclude browser tests. PRs and pushes to `main` run Ruff and
Python 3.11 non-browser tests. The Python 3.12–3.14 matrix, Chromium, macOS, and
package validation run on tags or when **CI → Run workflow** is requested.

After installing the full-check prerequisites, run every check for your current
Python version and operating system with one command:

```bash
.venv/bin/python scripts/verify.py
```

See [verification setup and CI policy](docs/verification.md) for prerequisites,
platform coverage, individual suites, and release behavior.

## License

MIT

### Cleanup improvements

The UI opens in **Find duplicates quickly** on every launch, including after a
Sets or custom scan. Exact and similar detection are on; Sets, face/person analysis,
low-resolution review, and Random 50 are off. Resized and lightly edited copies
are covered by similar matching; deeper edited-copy search is available explicitly.
Keeper preferences, exclusions, media types, and thresholds remain remembered.

Scans compare all selected folders together by default, finding exact and similar
copies both within and across folders. **Scan folders separately** (CLI: `--parallel`)
is an explicit option for independent scans that exclude cross-folder matches.

Use the **Scan goal** presets, preferred keeper folders, and **Best cleanup
opportunities** sort to focus review. Similar pairs include image difference/shared
zoom and synchronized video comparison. The [cleanup workflow](docs/cleanup-workflow.md)
explains match evidence, optional edited-copy verification, local review-label
exports, and read-only cold/warm benchmarks.
