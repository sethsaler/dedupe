# Dedupe

Local **Gemini-style** finder for **duplicate and near-duplicate** images, videos, and GIFs.

Point it at a folder, scan recursively, review groups in a browser UI, then move extras to **Trash** or a **quarantine folder**.

## Features

- **Exact duplicates** — size → partial hash → SHA-256
- **Similar media** — perceptual hashing for images/GIFs; ffmpeg frame sampling for videos
- **Non-Human media** — optional OpenCV review surfaces images, GIFs, and sampled videos where no person was detected (a high-likelihood "not a human" filter)
- **Smart Select** — automatic keep (best resolution/size/date) plus keep newest/oldest/largest/etc.
- **Safe actions** — Trash (macOS-recoverable) or move to a quarantine folder; dry-run previews; act on Exact, Similar, or Non-Human separately or all at once
- **Scan cache** — `~/.cache/dedupe/hashes.sqlite3` reuses hashes and completed OpenCV person checks for unchanged media
- **Resumable reviews** — the last completed review and selections are saved atomically under `~/.local/state/dedupe/` and revalidated when resumed
- **Scan quality report** — stage timings, cache hits, failures, skips, and dependency warnings make incomplete analysis visible
- **Local web UI** — search/sort/filter, similarity presets and explanations, overlay/flicker comparison, keyboard navigation, native picker, and isolate
- **Preview-first actions** — Trash and quarantine always run preflight before their final confirmation; the review sheet shows category counts and affected bytes

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

1. Paste a folder path (e.g. `~/Pictures`) or click **Choose…**
2. Configure optional exclusion globs, then hit **Scan** — review groups stream into the sidebar
3. Search or sort groups, Smart Select keep/remove, and compare Similar images with the lightbox overlay
4. Review the action preview, then **Trash**, **Quarantine**, or **Isolate** (copies into `_Dedupe Review` inside the source)

Completed reviews resume automatically after an app restart. Use **Discard saved review**
to clear the saved session. Changed, missing, or out-of-root files are removed from a
resumed review before it is shown, and every file is still revalidated immediately before an action.

Keyboard: `j`/`k` groups · `Space` toggle remove · `Enter` lightbox · `?` shortcuts

### CLI

```bash
# Scan and summarize
dedupe scan ~/Pictures

# Write full JSON results
dedupe scan ~/Pictures ~/Downloads --json results.json

# Exact only (faster)
dedupe scan ~/Movies --no-similar

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

# Restore a quarantine action from its receipt (preview first)
dedupe undo ~/.cache/dedupe/logs/action-<timestamp>-<id>.json
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
| Similar images/GIFs | Global pHash + dHash candidates, then **regional tile pHash** to reject pose/composition changes (default Hamming ≤ 6, tile max ≤ 8) |
| Similar videos | Ordered pHashes from direct ffmpeg timeline seeks, compared at normalized positions (default mean Hamming ≤ 8) |
| No person detected | Offline OpenCV YuNet face + full-body detection on images, representative GIF frames, and up to 16 direct-seek video frames with positive-evidence early exit |

The no-person review can use `opencv` (fast default), `photon` (Moondream 3.1 through the local Photon runtime), or `ensemble` (OpenCV positives first, then Photon on uncertain frames). Photon stays opt-in: its first use can download roughly 10 GB of model weights, and detection returns person/face boxes rather than a calibrated confidence score. All processing remains local after the model is available.

“No person detected” is a conservative computer-vision-assisted review filter, not a guarantee. It is opt-in and leaves non-human files unselected until you review them manually or apply **Mark reviewed + select non-human**. OpenCV runs the bundled YuNet face model before its full-body detector; if the face model is missing, corrupt, or cannot start, the scan fails closed and surfaces no media as Non-Human. The UI shows how many frames were analyzed. Obscured or unsampled people can still be missed. OpenCV is an optional, CPU-only dependency and does not download a model at runtime.

The bundled YuNet model comes from the official OpenCV Model Zoo. Its MIT license is included at `src/dedupe/assets/LICENSE-YUNET.txt`.

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

**Near-identical only** — same photo at different quality/export/resolution. Different poses of the same person (or burst frames that actually move) are filtered out by comparing pHash across image quadrants + center crop.

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

## Safety

- Never hard-deletes in the UI
- Always leaves at least one file per group
- File identity, scan-root containment, and exact hashes are revalidated before execution
- File and directory symlinks are skipped by default
- Executed actions receive unique atomic receipts under `~/.cache/dedupe/logs/`
- Quarantine receipts can restore files with `dedupe undo`; Trash is restored through Finder
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
  human_benchmark.py # labeled OpenCV / Photon comparison harness
  similarity_benchmark.py # labeled near-duplicate pair benchmark
  review_session.py  # atomic resumable review storage
  parallel.py        # thread-pool map for hashing stages
  grouping.py        # ranking + smart select
  actions.py         # trash / quarantine
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

Normal `pytest` runs exclude the browser test. CI covers Python 3.11–3.14, Ruff,
wheel installation, and a dedicated Chromium workflow.

## License

MIT
