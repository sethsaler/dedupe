# Faster duplicate cleanup

Choose **Find duplicates quickly** to compare all selected folders together,
without face counting, person detection, low-resolution review, or random review.
The existing exact-duplicate behavior still applies: when Exact is enabled in
the browser, identical extra copies move to Trash after scanning. Presets do not
change deletion safeguards. **Custom settings** retains individual controls.

Choose **Find duplicates and edited copies** to add the optional DigUp semantic
engine at a 94% candidate threshold. Below 97%, candidates must pass OpenCV
geometric verification: distributed matching details, consistent alignment,
sufficient overlap, and low residual differences across image tiles. A local
content change can reject a candidate even if most of the background matches.
If either dependency is absent, this deeper capability is unavailable. Standard
matching continues to work. Semantic matches stay manual review suggestions;
no semantic score is a probability or proof of duplication. Related subjects
remain in Sets. Animations do not use the new still-image geometry verifier.

## Decide what to keep

Enter preferred folders, one per line, highest priority first. Preferences apply
to the next scan, including exact copies. They choose a survivor, not protected
folders: other files in those folders can still be selected or removed.
CLI equivalent: `dedupe scan PATH --prefer-folder ORIGINALS` (repeatable).

Keeper ranking considers preferred folders, camera capture metadata, resolution,
recognizable export names, byte size, modification time, and path. Metadata and
resolution are evidence, not a guarantee of quality: an edited file can retain
EXIF, and an upscale can have more pixels. No sharpness or aesthetic-quality
claim is made. Every member of a perceptual group is still directly matched to
its suggested keeper; preferences are applied before clustering.

**Best cleanup opportunities** is the default review sort. It combines reclaimable
space with match type/strength and a clear resolution winner. Semantic groups
rank more conservatively. The explanation appears with each group; files with
analysis errors are deprioritized. This ordering never changes selection or
authorizes deletion.

## Compare details

In Similar pair review, expand a still image for an overlay, hold-to-flicker,
or Difference mode. Zoom loads both originals into one coordinate space;
panning moves them together. Different aspect ratios/crops may not line up, so
a bright difference is not itself proof of different content. GIFs retain their
animation view. Video pairs offer synchronized playback, seeking, and speed;
time is aligned from the start, not content-aligned across trims. Metadata shows
image format/capture details or audio codec/channel information. Audio content
is not compared. Metadata is fetched only for the visible pair.

## Measure improvements

Scan diagnostics include total/stage durations, cache hits, and time until the
first duplicate group. A cold/warm benchmark uses a fresh temporary cache and
never calls deletion actions:

```bash
.venv/bin/python scripts/benchmark_cleanup.py /path/to/library --json /tmp/timing.json
# Add --semantic to include the optional edited-copy engine.
```

The optional semantic neighbor search skips already-compared matrix blocks.
This preserves the same exact cosine matches while reducing repeated work.

**Export review labels**, under Filter & organize, downloads Same/Different
pair decisions and review timing collected locally in this browser. Undo removes
the pair's label. The export contains absolute paths and no media bytes; nothing
is uploaded. Labels are capped at the most recent 5,000 pairs. Restore any
trashed samples before evaluating their paths. These decisions are useful labels,
not independently verified truth. Timing excludes comparisons lasting over five
minutes and is an interaction estimate, not a controlled usability experiment.

Evaluate a labeled corpus using:

```bash
.venv/bin/dedupe benchmark-similarity pairs.json --json /tmp/accuracy.json
# Add --semantic --semantic-duplicate-threshold 0.94 for edited copies,
# or --decider to also judge unconfirmed candidates with Decider.app.
```

Reports include precision, recall, errors, individual missed/wrong pairs, timings,
and category breakdowns. Optional `category` values in the input manifest allow
separate checks for crops, resizes, burst photos, and other transformations.

For reproducible smoke data (not a substitute for your library):

```bash
.venv/bin/python scripts/create_similarity_corpus.py /tmp/new-corpus
```

This generates eight labeled pairs from the repository's existing licensed
astronaut fixture: resize, JPEG compression, rotation, crop, brightness,
grayscale, unrelated content, and a localized edit. The output directory must
not already exist. Preserve independent negative examples when tuning thresholds;
optimizing only known duplicates can silently increase false positives.
