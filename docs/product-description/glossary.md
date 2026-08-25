# Glossary

The vocabulary used across these documents. When a document uses one of these words, it means exactly this.

## The surfaces

**Web UI.** The browser interface served by `dedupe ui` at `http://127.0.0.1:8765`. It is local-only: the server runs on the user's machine and the browser talks to it on the loopback address. Most of the user's time goes here.

**CLI.** The `dedupe` command and its subcommands (`scan`, `ui`, `isolate`, `undo`, `receipts`, `doctor`, plus the out-of-scope benchmarks). Running `dedupe PATH...` is a shortcut for `dedupe scan PATH...`.

**Review category.** One of the ways a scan surfaces files for review: *Duplicates* (exact groups), *Similar images*, *Similar videos*, *Low-res*, *Random 50*, *Non-Human*, and *Faces*. Each appears as a tab in the sidebar with its own group list; *All* shows every group.

## The objects

**Media file.** An image, GIF, or video found under the scanned folders. Nothing else is examined; other file types are skipped.

**Duplicate group.** A set of media files the scan believes belong together for review. Groups come in several kinds: *exact* (byte-identical), *similar* (visually close images/GIFs or videos), *low resolution*, *random*, *non-human*, and *faces*. Exact and similar groups are duplicates of each other; the others are review lists of independent files.

**Member.** One media file inside a duplicate group.

**Keeper.** The one file in a duplicate group the user intends to survive an action. The *suggested keeper* is the member Dedupe recommends keeping; it is highlighted in green in the UI. The suggestion ranks members by more pixels first, then larger file size, then newer modification time, then shorter path. A duplicate group never has its suggested keeper selected for removal by bulk operations, so at least one member always survives.

**Selection.** The set of members in a group currently marked for removal. Members are selected or deselected individually, by bulk operations, or by a selection rule. Only the selection is acted on when an action is confirmed.

**Review candidate.** A file in an independent review category (*non-human*, *faces*, *low-res*, *random*) that is judged on its own, not against other members of a group. Candidates can be trashed and restored one at a time.

**Receipt.** The JSON record written after a trash or quarantine action runs. It lists every file moved and where it went, and it is what `dedupe undo` and `dedupe receipts` read.

## State

**Selected for removal.** A member's state when its checkbox is on; the action sheet will move exactly these files. The opposite state is *kept* (unselected). In duplicate groups the suggested keeper starts unselected; in independent categories nothing starts selected.

**Needs attention.** A group is shown as needing attention when its selection is not in a complete state for review — the `[` and `]` keys jump between such groups among the groups currently shown.

**Complete.** A group whose review state needs no further decision (for example, a duplicate group with at least one member selected and its keeper kept). Completed groups can be hidden from the list with the *Hide completed* control.

**Reviewed.** A file the user has explicitly decided about in a review category. For low-resolution candidates, reviewing and leaving a file unselected records a durable *Keep decision* so future scans stop resurfacing it. For non-human candidates, *Mark all remaining as human* records the rest as reviewed in bulk.

**Keep decision.** A durable record (stored in the keep-decisions file) that a specific file was reviewed and kept. A kept decision matches a file by its identity; if the file changes, the decision no longer applies.

**Distinct.** A similar group the user has marked *Mark as distinct*: its files are recorded as not duplicates of each other, and the group stays hidden in future scans unless one of the files changes.

## Engines and detection

**Diagnostics.** The per-stage accounting a scan carries: for each stage, how many items were attempted, succeeded, failed, and skipped, how long it took, and up to ten warnings. The CLI prints a diagnostics block after every scan; the UI shows the same numbers with the results. Diagnostics are how a user learns that a stage ran but found nothing, versus never ran at all.

**Fail closed.** A detector fails closed when its own unavailability produces *no candidates* rather than guesses: a missing or corrupt YuNet model surfaces no Non-Human review at all. The rule exists because a person detector that quietly misses people would lead to deleting photos of people.

**Scan.** One full run over a set of folders: discovering media files, hashing them, detecting exact and similar groups, and producing the review categories. A scan's results stay loaded in the UI until a new scan replaces them or the server restarts.

**Detection threshold.** The maximum Hamming distance between perceptual hashes for two files to count as similar. Defaults: 6 for images (with tile checks up to 8), 8 for videos. `foundations/scan-pipeline.md` owns these numbers.

**Hash cache.** The on-disk SQLite store of per-file hashes, so rescans skip recomputing unchanged files. Owned by `cross-cutting/caches-and-files.md`.

**Backend.** The person detector used by the no-person review: `opencv` (fast, default), `photon` (opt-in, downloads roughly 10 GB of model weights on first use), or `ensemble` (OpenCV first, then Photon on uncertain frames). All processing is local.

**Revalidation.** The check that every selected file still matches its scan snapshot immediately before an action runs. Files that changed, moved, or vanished are excluded; the action never runs on stale information.

## Interactions

**Invocation.** One run of a CLI command, from argument parsing to exit code. The unit of interaction for `cli/` documents. Its phases are *invoke*, *exit immediately*, *begin running*, *while running*, and *finish*.

**Task.** One user activity in the web UI that has a beginning, a possibly long middle, and an end — configuring and running a scan, reviewing a category, confirming an action. The unit of interaction for `ui/` documents. Its phases are *start*, *end without changing anything*, *become extended*, *while extended*, and *complete*.

**Action.** A confirmed, executed operation on the selection: *Trash*, *Quarantine*, or *Isolate*. Actions are previewed in the action sheet before they run, and trash and quarantine actions write a receipt.

**Trash.** Moving selected files to the macOS Trash via the system mechanism, where the user can still recover them until the Trash is emptied.

**Quarantine.** Moving selected files into a quarantine folder Dedupe creates, keeping them out of the scanned folders but fully recoverable with `dedupe undo`.

**Isolate.** Copying (or moving) group members into review folders inside a `_Dedupe Review` directory next to the source, so the user can compare files in Finder. Isolate is a CLI operation over a previous scan's JSON output.

**Preview token.** A short-lived server-side confirmation that the previewed action matches what will execute. The action sheet counts down while it is valid; if it lapses, the selection is re-verified and the numbers are shown again before confirming.

**Resume.** Restoring a saved review session after the app restarts. Changed, missing, or out-of-root files are pruned from the resumed session before it is shown, and the banner reports what was dropped and why.

## Events that end or interrupt

**Cancel.** The user stops an interaction before it commits: Escape closes overlays and sheets, Ctrl+C stops a CLI run. Nothing is committed by a cancel.

**Complete.** The interaction reaches its durable step: an action executes, a receipt is written, a review decision is saved. Completes are one-way except through *undo* or the macOS Trash.

**Interrupt.** Something outside the interaction stops or supersedes it: the server dies, the browser tab closes, a file changes on disk. Interrupted work is either recoverable (a saved review session resumes) or lost (an unconfirmed selection), and the document for each feature says which.

**Safe to interrupt.** A run is safe to interrupt while it has written nothing the user did not ask for: Ctrl+C during a scan leaves the folders as they were. Once an action begins moving files, interruption is no longer free; the receipt records what completed.

## Files on disk

**Review session file.** The saved review state (`~/.local/state/dedupe/review-session.json`) that lets a completed review survive an app restart. Written atomically so a crash cannot corrupt it.

**Keep-decisions file.** The durable store of *Keep decisions* (`~/.local/state/dedupe/keep-decisions.json`).

**`_Dedupe Review`.** The directory `isolate` creates inside the scanned source to hold review copies, named so it is visible in Finder and excluded from nothing by default.

**Receipts directory.** Where action receipts are stored; `dedupe receipts list` reads it, `dedupe receipts prune` deletes old entries.
