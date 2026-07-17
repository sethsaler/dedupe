# Dedupe

Local **Gemini-style** finder for **duplicate and near-duplicate** images, videos, and GIFs.

Point it at a folder, scan recursively, review groups in a browser UI, then move extras to **Trash** or a **quarantine folder**.

## Features

- **Exact duplicates** — size → partial hash → SHA-256
- **Similar media** — perceptual hashing for images/GIFs; ffmpeg frame sampling for videos
- **Smart Select** — automatic keep (best resolution/size/date) plus keep newest/oldest/largest/etc.
- **Safe actions** — Trash (macOS-recoverable) or move to a quarantine folder; dry-run previews
- **Hash cache** — `~/.cache/dedupe/hashes.sqlite3` for fast re-scans
- **Local web UI** — thumbnails, lightbox, smart select, keyboard nav, native folder picker, isolate

## Requirements

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/) + ffprobe (for video similarity and video thumbnails)
- macOS recommended (Trash + `open -R` reveal); Linux works for scan/quarantine

```bash
# macOS
brew install ffmpeg
```

## Install

```bash
cd dedupe
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
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
2. Hit **Scan** — review groups in the sidebar
3. Smart Select keep/remove, open thumbnails in the lightbox
4. **Trash**, **Quarantine**, or **Isolate** (copies into `_Dedupe Review` inside the source)

Keyboard: `j`/`k` groups · `Space` toggle remove · `Enter` lightbox · `?` shortcuts

### CLI

```bash
# Scan and summarize
dedupe scan ~/Pictures

# Write full JSON results
dedupe scan ~/Pictures ~/Downloads --json results.json

# Exact only (faster)
dedupe scan ~/Movies --no-similar

# Parallel hashing (default: auto = CPU count; 1 = serial)
dedupe scan ~/Pictures --workers 8

# Stricter similarity (0 = almost exact visual match)
dedupe scan ~/Pictures --threshold 4

# Dry-run trash selection
dedupe scan ~/Downloads --action trash --dry-run

# Isolate matches into review folders *inside the scanned source*
dedupe scan ~/Pictures --action isolate --execute
# → ~/Pictures/_Dedupe Review/exact/… and …/similar/…

# Only exact matches (still under the source by default)
dedupe scan ~/Pictures --action isolate --isolate-kinds exact --execute

# Re-use a previous scan JSON (defaults to that scan's root/_Dedupe Review)
dedupe isolate results.json --execute

# Override only if you really want a different location
dedupe isolate results.json --review-dir /some/other/path --execute

# Open UI with last scan results
dedupe scan ~/Pictures --ui
```

### Isolate for human review

When exact or similar groups are found, isolate builds a review tree **inside the scanned source folder** (never Desktop or the dedupe repo by default):

```
<your scanned folder>/
  photo1.jpg
  photo1_copy.jpg
  …
  _Dedupe Review/          ← created here, next to the media
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
| `--isolate-mode hardlink` | | Same bytes, no extra disk use (same volume) |
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
| Similar videos | Sample frames with ffmpeg → XOR pHash fingerprint (default Hamming ≤ 8) |

**Near-identical only** — same photo at different quality/export/resolution. Different poses of the same person (or burst frames that actually move) are filtered out by comparing pHash across image quadrants + center crop.

### Parallelism & resource limits

Hashing stages run in a **bounded** thread pool so large libraries don’t pin every core or thrash disk/RAM:

| Setting | Default | Cap |
| --- | --- | --- |
| `--workers N` | auto (`min(cpu−1, 8)`) | overall budget; `1` = serial |
| Exact SHA-256 | ≤ budget | max **4** concurrent full-file reads |
| Image pHash | ≤ budget | max **6**; images downscaled ≤512px before hash |
| Video fingerprints | ≤ budget | max **2** concurrent ffmpeg (each `-threads 1`) |

Also:

- Futures stay windowed (~2× workers in flight) so 50k files don’t allocate 50k tasks at once
- Image decode uses Pillow `draft()` + thumbnail so 12MP HEIC/JPEG never hold full-res RGB
- Video uses **one-pass** frame sampling (not N independent seeks) at 320px width
- Hash cache means re-scans skip almost all of the above

For a laptop-friendly scan of a huge folder: `dedupe scan ~/Pictures --workers 2`.

## Safety

- Never hard-deletes in the UI
- Always leaves at least one file per group
- Actions are logged under `~/.cache/dedupe/logs/`
- Use **Preview** next to Trash / Quarantine / Isolate before executing

## Project layout

```
src/dedupe/
  engine.py          # orchestrates a full scan
  exact.py           # byte-identical groups
  similar_image.py   # perceptual image/GIF groups
  similar_video.py   # video fingerprints
  parallel.py        # thread-pool map for hashing stages
  grouping.py        # ranking + smart select
  actions.py         # trash / quarantine
  cache.py           # SQLite hash cache
  cli.py             # `dedupe` entry point
  web/               # Flask UI
```

## Tests

```bash
pytest
```

## License

MIT
