# Dedupe — Local Gemini-style Media Duplicate Finder
## Goal
Build a local app that scans a folder (and subfolders) for **duplicate and near-duplicate media** — primarily **images, videos, and GIFs** — with a Gemini 2–inspired workflow: pick folders → scan → review groups → smart-select → safely remove extras.

* * *
## Product principles (what Gemini 2 does well)
| Gemini 2 concept | Our equivalent |
| --- | --- |
| Exact duplicates | Byte-identical files (content hash) |
| Similar files | Visually near-identical media (perceptual hash) |
| Smart Select | Rules to auto-keep one file per group |
| Thumbnail + list review | Side-by-side preview of each group |
| Safe removal | Move to Trash / quarantine folder (never hard-delete by default) |
| Folder-scoped scan | User points at one or more root folders |

**Out of scope for v1** (can add later): Photos.app library integration, iTunes/Music library, live “duplicates monitor”, cross-drive background daemon, Smart Select machine-learning from user habits.

* * *
## Target UX flow
1. **Select folders** — one or more roots to scan (recursive by default).
  
2. **Configure** — file types, exact vs similar, similarity threshold, exclusions.
  
3. **Scan** — progress (files found, hashed, groups so far, ETA).
  
4. **Results** — **Exact**, **Similar**, and optional **AI review candidates**.
  
5. **Review** — each group as thumbnails + metadata (path, size, dimensions, modified date).
  
6. **Select** — Smart Select rules or manual checkboxes.
  
7. **Act** — move selected files to Trash or a chosen folder; optional dry-run report.
  

Safety rails:

- Never delete without explicit confirm.
  
- Default action = Trash (recoverable), not permanent delete.
  
- Always keep at least one file per group.
  
- Dry-run mode that only reports.
  

* * *
## Detection pipeline
### Stage 1 — Inventory
Walk roots recursively, skip symlinks and hidden files by default, enforce scan-root containment, and respect exclusion globs.

Supported media (v1):

| Bucket | Extensions |
| --- | --- |
| Images | `.jpg`, `.jpeg`, `.png`, `.heic`, `.webp`, `.tif`, `.tiff`, `.bmp`, `.raw` (best-effort) |
| GIFs | `.gif` (treated as image for hashing; listed separately in UI) |
| Videos | `.mp4`, `.mov`, `.m4v`, `.avi`, `.mkv`, `.webm`, `.mts` |

Collect metadata: path, size, mtime, extension, mime, dimensions (when cheap).
### Stage 2 — Exact duplicates
1. Bucket by **file size** (same size is a necessary condition).
  
2. For multi-file size buckets, compute **partial hash** (first 64KB) then full **SHA-256** if still tied.
  
3. Group files with identical full hash → **Exact Duplicate** sets.
  

Fast, exact, works for any binary including video/GIF.
### Stage 3 — Similar media (visual)
Only for files **not** already claimed as exact duplicates of each other (or run independently — configurable).

**Images / GIFs**

- Decode with Pillow (+ `pillow-heif` for HEIC).
  
- Compute perceptual hashes: **pHash** (primary) + **dHash** (secondary), via `imagehash`.
  
- Index hashes in a BK-tree (`pybktree`) for fast Hamming-distance search.
  
- Default threshold: Hamming distance ≤ 6 on 64-bit pHash (tunable slider: Strict → Loose).
  
- Optional: require agreement from dHash within a looser threshold to cut false positives.
  

**Videos**

- Sample N frames with **ffmpeg** (e.g. 1 fps or fixed 8–16 evenly spaced frames).
  
- Hash each frame; combine into a video fingerprint (XOR / majority / ordered frame-hash sequence).
  
- Compare fingerprints with Hamming / sequence distance.
  
- Fallback: if ffmpeg unavailable, only exact-hash video duplicates.
  

**GIF**

- Hash first frame + every Nth frame (or middle frame for short GIFs) so animated look-alikes group correctly.
  
### Stage 4 — Grouping & ranking
Each group:

- **Members**: 2+ paths
  
- **Kind**: `exact` | `similar`
  
- **Media type**: image / gif / video
  
- **Suggested keep**: highest resolution × largest bytes × newest mtime (weighted), preferring shorter path depth as soft tie-break
  
- **Space reclaimable**: sum(sizes) − size(suggested keep)
  

* * *
## Smart Select rules (v1)
| Rule | Behavior |
| --- | --- |
| **Automatic (default)** | Keep suggested keep; select rest for removal |
| **Keep newest** | Keep latest mtime |
| **Keep oldest** | Keep earliest mtime |
| **Keep largest** | Keep biggest file (bytes / resolution) |
| **Keep smallest** | Keep smallest file |
| **Keep shortest path** | Prefer shallower / simpler path |
| **Deselect all** | Manual only |
| **Invert** | Flip selection within group |

User can override any selection before acting.

* * *
## Architecture
```
dedupe/
├── pyproject.toml / requirements.txt
├── README.md
├── PLAN.md
├── src/dedupe/
│   ├── __init__.py
│   ├── cli.py              # entry: dedupe scan / review
│   ├── scanner.py          # walk + inventory
│   ├── exact.py            # size → partial → sha256
│   ├── similar_image.py    # pHash / dHash + BK-tree
│   ├── similar_video.py    # ffmpeg frame sample + fingerprint
│   ├── grouping.py         # group build, ranking, smart select
│   ├── actions.py          # trash / move / report (safe)
│   ├── cache.py            # SQLite hash cache by (path, size, mtime)
│   ├── models.py           # FileRecord, ReviewGroup, ScanResult
│   ├── human_detection.py  # OpenCV / Photon / ensemble person review
│   ├── human_benchmark.py  # labeled detector comparison harness
│   └── web/
│       ├── app.py          # local Flask API + UI server
│       ├── templates/      # browser review shell
│       └── static/         # interaction and styling
├── tests/
└── launchers/
    └── Dedupe.command      # macOS double-click
```
### Why Python + a localhost web UI for v1
- Python provides Pillow, ImageHash, OpenCV, ffmpeg orchestration, hashing, and safe Trash support.
  
- A loopback-only Flask UI provides a richer side-by-side review surface while keeping media local.
  
- Mutating API calls require a per-launch token and current scan generation; the server rejects cross-origin writes.
  

Alternative considered and deferred:

- Electron/Tauri: prettier UI, heavier install.
  
- Pure CLI: useful, but Gemini-like review needs previews.
  
### Persistence
- **SQLite cache** under `~/.cache/dedupe/`: algorithm version + path + device/inode + size + nanosecond mtime → sha256/phash/video fingerprint.
  
- Invalidate cache entry when size or mtime changes.
  

* * *
## Tech stack (v1)
| Concern | Library |
| --- | --- |
| Language | Python 3.11+ |
| Images | Pillow, pillow-heif |
| Optional person review | OpenCV baseline; Photon / Moondream 3.1; conservative ensemble |
| Perceptual hash | imagehash |
| Near-neighbor index | pybktree |
| Video frames | ffmpeg / ffprobe (system dep) |
| Safe trash | send2trash |
| UI  | Flask + vanilla browser UI + Pillow thumbnails |
| Packaging | pyproject.toml, optional `pipx install -e .` |
| Tests | pytest |

* * *
## CLI sketch
```bash
# Scan and print summary JSON
dedupe scan ~/Pictures --similar --threshold 6 --json results.json
# Open GUI
dedupe ui
# Scan then open review on last results
dedupe scan ~/Movies --include video --ui
# Dry-run smart select
dedupe scan ~/Downloads --action trash --smart automatic --dry-run
```

* * *
## GUI sketch (v1)
```
┌─────────────────────────────────────────────────────────┐
│  [Add Folder…]  [Scan]   Exact ☑  Similar ☑  Thresh [====]│
├──────────────┬──────────────────────────────────────────┤
│ Groups       │  Group preview (thumbnails)               │
│ ○ Exact 42   │  ┌────┐ ┌────┐ ┌────┐                    │
│ ● Similar 17 │  │keep│ │ ☑  │ │ ☑  │                    │
│              │  └────┘ └────┘ └────┘                    │
│  2.4 GB free │  path / size / 4032×3024 / modified       │
│              │  [Reveal in Finder]                        │
├──────────────┴──────────────────────────────────────────┤
│ Smart: [Automatic ▾]  [Select all groups]  [Move to Trash]│
└─────────────────────────────────────────────────────────┘
```

* * *
## Performance notes
- Size-bucketing before hashing → most files never fully hashed.
  
- Parallel hash workers (thread pool for I/O, process pool optional for CPU-bound image decode).
  
- Hash cache makes second scan near-instant for unchanged trees.
  
- Video frame sampling capped (e.g. max 16 frames, max decode resolution 320px).
  
- Large folders: stream progress every N files; don't hold decoded images in memory.
  

* * *
## Safety rules (hard)
1. Never overwrite files.
  
2. Never hard-delete in v1 UI (Trash or move-to-folder only).
  
3. Always leave ≥1 file unselected per group unless user forces a documented override.
  
4. Dry-run must be first-class.
  
5. Revalidate identity and scan-root containment immediately before every action.

6. Log every action to a unique atomic receipt; support quarantine restoration from that receipt.
  

* * *
## Implementation phases
### Phase 1 — Core engine (CLI) — complete
- Inventory walk
  
- Exact duplicate detection
  
- Image similar detection (pHash + BK-tree)
  
- Smart Select rules
  
- JSON report + dry-run
  
- Tests with fixture media
  
### Phase 2 — Video + GIF polish — complete
- ffmpeg video fingerprints
  
- Animated GIF multi-frame hashing
  
- Cache layer
  
### Phase 3 — Local web UI — complete
- Folder pick, progress, group browser, thumbnails
  
- Smart Select + Trash actions
  
- macOS `.command` launcher
  
### Phase 4 — Polish — substantially complete
- Cache-backed incremental re-analysis and durable review resume
  
- Exclusion globs / whitelist folders
  
- Scan-quality report and preview-first action review
  
- Preference and selection persistence

### Phase 5 — Quality and distribution — in progress
- Similarity and human-detection benchmark harnesses

- Python/Ruff/package CI and a real Chromium workflow test

- Credential-free macOS app-bundle scaffold plus gated signing/notarization helper

- Remaining: collect representative private benchmark corpora and publish measured targets

- Remaining: produce signed/notarized release artifacts once Apple credentials and release policy are available
  

* * *
## Locked decisions

| Decision | Choice |
| --- | --- |
| UI primary | **Local web UI** (browser-based review) |
| Similarity depth v1 | **Exact + image/GIF + video similar** from day one |
| Action model | **Trash or quarantine folder** |
| Similar scope | **Near-identical only** (not loose look-alikes) |
| Package / CLI name | `dedupe` |
  

* * *
## Success criteria (v1)
- Point at a folder with known planted duplicates; finds 100% of byte-identical media.
  
- Finds near-duplicate JPEGs that differ only in quality/export settings (perceptual).
  
- Smart Select never marks the sole remaining unique file in a group for removal.
  
- Trash actions are reversible via Finder Trash.
  
- Re-scan of same tree uses cache and is meaningfully faster.
  
- Usable on a multi-GB photo/video dump without OOM.
  

* * *
## Non-goals (v1)
- Cloud / network protocol scanning as first-class
  
- Photos.app library direct integration
  
- Audio fingerprinting
  
- Background always-on monitor
  
- Paid Mac App Store packaging
