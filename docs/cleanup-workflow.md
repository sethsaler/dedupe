# Faster duplicate cleanup

Choose **Find duplicates quickly** to compare all selected folders together,
without face counting, person detection, low-resolution review, or random review.
Exact copies appear for review; scanning never automatically trashes them.
**Delete All Exact Duplicates** prepares the suggested selection and opens a
confirmation sheet. **Custom settings** retains individual controls.

Choose **Find duplicates and edited copies** to add the optional DigUp semantic
engine at a 94% candidate threshold. Candidates must pass local image verification:
OpenCV can align cropped or filtered copies, then check their overlapping detail.
If alignment is unavailable, the ordinary orientation, regional, and fine-detail
checks must establish the match. A local content change rejects a candidate even
if most of the background matches or a model rates it highly. Without DigUp,
standard matching continues to work. Semantic matches stay manual review suggestions;
no semantic score is a probability or proof of duplication. Related subjects
remain in Sets. Animations do not use the new still-image geometry verifier.

## Clean an inbox against a protected archive

Enter the folder to clean under **Folders or files to scan**. Open **Compare
against a protected library** and enter archive folders, one per line. Dedupe
includes both sides in the comparison and prevents archive files from being
selected for removal or moved, including through isolate-move. Protected copies
are preferred as keepers. A folder included on both sides remains protected.
Turn off **Scan folders separately** before using protected libraries; protection
requires one combined comparison across folders.

```bash
dedupe scan ~/Downloads --protect-folder ~/Pictures/Archive --ui
# Repeat --protect-folder for additional archives.
```

Protected library folders are stronger than keeper preferences: preferences
rank copies, while protection prevents a file from being removed or moved.

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

## Repeat a cleanup without losing your place

Open **Saved cleanup jobs**, enter a name, and choose **Save job**. The job stores
the current folders and scan settings. Select it later to restore those settings,
then choose **Resume review** to continue its independently saved review.
Switching jobs does not replace another job's saved progress.

After the job has completed a scan, **Check new arrivals** compares the current
library and focuses review on new or changed files. Duplicate groups retain
their older matching copies, so a new download can still be compared with the
archive. This is a focused review backed by cached analysis, not a filesystem
watcher: the scan still inventories the selected folders. Changing a job's
settings requires a full scan before checking new arrivals again.

**Delete job** removes the saved settings and review, leaving media untouched.
The ordinary **Resume saved review** banner remains available for the last review.

## Compare details

In Similar pair review, expand a still image for an overlay, hold-to-flicker,
or Difference mode. Zoom loads both originals into one coordinate space;
panning moves them together. Different aspect ratios/crops may not line up, so
a bright difference is not itself proof of different content. GIFs retain their
animation view. Video pairs offer synchronized playback, seeking, and speed.
Standard pairs align playback from the start. Metadata shows image format/capture
details or audio codec/channel information and is fetched only for the visible pair.

Enable **Video clips & versions** in Options to look for trimmed clips, offset
versions, and soundtrack changes. The CLI equivalent is:

```bash
dedupe scan ~/Movies --video-versions --ui
```

Version matches show an **Approximate matching section** for each file, and
synchronized playback starts at those offsets. Audio labels distinguish sampled
agreement, different audio, no audio, and unavailable evidence. These suggestions
require manual selection; matching footage does not establish that either
version is expendable.

The version pass samples at most 96 timeline positions and requires at least two
seconds of matching footage covering 80% of the shorter video. Short excerpts
inside long recordings, heavy edits, or speed changes can be missed. Alignment is
approximate. Audio comparison checks two windows inside the overlap; agreement
does not prove complete soundtracks are identical. Standard video matching does
not compare audio content.

## Review past cleanups and recover interruptions

Open **History** to see previous cleanups and their affected files. **Restore
files…** checks that the moved files are still available and their original paths
are free, then opens a confirmation. It works after restarting, without loading
the old scan. Restoring files does not recreate review groups; scan again to see
them. Changed, missing, or already-restored files can block a restore.

The **Not duplicates** section lists remembered pair decisions. **Reconsider this
pair** removes that exclusion so the files can match on a subsequent scan. It
does not move either file.

Before moving files, Dedupe writes a receipt and records each move's intent and
outcome as work proceeds. An unwritable journal stops destructive work. If a
cleanup is interrupted, History checks the recorded paths and file identities
without moving anything. Confirmed moves can be restored normally. Outcomes that
cannot be identified safely remain flagged for inspection, including unresolved
Trash locations or an interrupted copy with both paths present. No uncertain
file is automatically restored or removed.

Keep the receipts and pending `.journal` files under `~/.cache/dedupe/logs/`
while a cleanup may need recovery. The CLI can also inspect complete receipts
and restore supported actions with `dedupe receipts` and `dedupe undo`.

## Retry incomplete analysis

Expand **Scan quality** to inspect failures and warnings. **Retry analysis using
cached work** reruns the last scan configuration, reusing completed work and
retrying uncached analysis. It is a full scan with cache reuse, not a retry limited
to the displayed error list.

Semantic analysis responds to cancellation while waiting for its helper. Stalled
helper startup or requests time out, and completed embedding batches remain
cached. Content-based reuse follows unchanged media through renames and moves.
Neighbor comparisons reuse unchanged pairs and compare newly added vectors with
the current library; a broader threshold or changed model may require more work.
These changes do not guarantee every file can be decoded or matched: review the
quality report after a retry.

## Measure improvements

Scan diagnostics include total/stage durations, cache hits, and time until the
first duplicate group. A cold/warm benchmark uses a fresh temporary cache and
never calls deletion actions:

```bash
.venv/bin/python scripts/benchmark_cleanup.py /path/to/library --json /tmp/timing.json
# Add --semantic to include the optional edited-copy engine.
```

The semantic neighbor cache preserves exact cosine comparisons for unchanged
vectors, avoiding repeat comparisons on compatible subsequent scans.

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
# or --decider for an additional model check; local image evidence is still required.
```

Reports include precision, recall, errors, individual missed/wrong pairs, timings,
and category breakdowns. Optional `category` values in the input manifest allow
separate checks for crops, resizes, burst photos, and other transformations.

For reproducible smoke data (not a substitute for your library):

```bash
.venv/bin/python scripts/create_similarity_corpus.py /tmp/new-corpus
```

This generates 24 labeled pairs from the repository's existing licensed
astronaut fixture: resize, JPEG compression, rotation, crop, brightness,
grayscale, unrelated content, a localized edit, and smaller synthetic changes
around the smile, hair, eyes, and pose. The small-change cases include compressed
and brighter exports, paired both with the unchanged original (distinct) and with
their own edited source (a true copy). The output directory must not already
exist. Preserve independent negative examples when tuning thresholds; optimizing
only known duplicates can silently increase false positives. These synthetic
cases do not measure accuracy on a real portrait or burst-photo library.
