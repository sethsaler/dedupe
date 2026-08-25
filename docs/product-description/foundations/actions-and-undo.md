# Actions and undo

## Summary

Actions are the durable step of the whole product: the moment selected files actually move. There are three — **Trash**, **Quarantine**, and **Isolate** — each safer than the one before in a different direction: Trash uses the system trash, Quarantine moves files into a folder Dedupe controls (and can give back with `undo`), Isolate makes copies laid out for comparison without touching the originals at all. Every action is previewed first, every file is revalidated twice — once for the batch and once more immediately before its own move — and every executed trash or quarantine action writes a receipt. This document owns that safety model; the sheet where the user confirms is [Action sheet](../ui/action-sheet.md), the CLI commands are [`isolate`](../cli/isolate.md), [`undo`](../cli/undo.md), and [`receipts`](../cli/receipts.md).

## The three actions

**Trash.** Selected files go to the macOS Trash via the system mechanism. Recovery is whatever Finder's Trash offers until it is emptied. Dedupe cannot programmatically restore trashed files — "Put Back" in Finder is the recovery path — with one exception: individual review candidates trashed from the Non-Human and Faces categories can be restored from the UI, because Dedupe remembers where each one went.

**Quarantine.** Selected files move into a quarantine folder the user chooses. Files keep their names; collisions get a unique suffix. Nothing is deleted — quarantine is a move, fully reversible with `dedupe undo` against the receipt. Quarantining to another volume is refused by default; it can be explicitly allowed, in which case Dedupe copies, verifies the copy, and only then removes the original.

**Isolate.** Each duplicate group is laid out in its own subfolder under a `_Dedupe Review` directory placed inside the scanned source, with the suggested keeper prefixed `KEEP__` and a small JSON describing the group beside it. The default mode copies — originals are never touched; hardlink, symlink, and move modes exist for users who want them. Isolate is organized in timestamped sessions, so running it twice produces two session folders, never a merged mess.

## The safety model

Every destructive path through the product applies the same layers, in this order:

1. **The effective selection.** What the user selected is reduced by the veto rules in [Duplicate group](duplicate-group.md#what-actually-acts-the-effective-selection): Keep decisions, unreviewed candidates, and the last-survivor guarantee. The preview shows the effective selection's counts and bytes, so the numbers on screen are the numbers that will act.
2. **Batch preflight.** Before anything moves, every selected file is checked against the disk: it exists, is a regular file (symbolic links are refused outright), is still inside the scanned folders, and its size, device, inode, and modification time match the scan snapshot. For exact groups the files are additionally re-hashed: each must still match its scan hash and still be byte-identical to the group's keeper. Any file failing preflight is excluded with its reason; the others proceed.
3. **Keeper protection.** Each keep-one group's retained member is validated too. If it has only drifted in metadata (an mtime or inode change with identical size), Dedupe re-hashes it and accepts it when the content still matches — this tolerates filesystems and sync tools that touch metadata. If the keeper genuinely changed, the whole group is held back: removing duplicates of a file that is itself no longer what it was is exactly the mistake the product exists to prevent.
4. **Immediate revalidation.** In an executed action each file is checked *again*, alone, in the instant before its own move. The batch preflight can be seconds old by the time a large action reaches its last files; this check closes that window.
5. **The receipt.** After execution, a JSON receipt records the action, every file, its outcome and destination, and the time. Receipts live in `~/.cache/dedupe/logs/` and are what `dedupe receipts` reads and `dedupe undo` consumes.

Dry runs stop after step 2: the preview is a real preflight, not an estimate — a file that would fail to move says so in the preview.

## Undo

**Quarantine undo** (`dedupe undo`, or the UI's equivalent) reads a receipt and moves every successfully quarantined file back to its original path, in reverse order. The preflight is all-or-nothing: if any quarantined file is gone or any original path is now occupied, the entire undo is refused — nothing is partially restored. Restores always may cross volumes, since a quarantine folder may legitimately live on another disk. The undo itself writes a receipt.

**Trash undo** does not exist as a command; the macOS Trash is the recovery mechanism. The exception is the per-candidate restore for Non-Human and Faces review items, which moves one trashed file back to its original path with the same occupied-path refusal.

## Preview tokens

The web UI adds one more layer: an action preview is confirmed against a short-lived server-side token. If the token lapses while the confirmation sheet stays open, the execute is never attempted with stale authority — the selection is re-verified, the numbers are refreshed, and the user confirms again. The countdown on the sheet is this token's lifetime made visible; see [Action sheet](../ui/action-sheet.md).

## Cancel and interrupt

| Event | Before execute | During execute |
| --- | --- | --- |
| The user aborts explicitly | Closing the sheet or letting the preview token lapse cancels; nothing moved. | There is no stop button once files begin moving; the action runs to completion of the file list. Ctrl+C against the server mid-action is the only abort, and it is not safe: files already moved stay moved, and the receipt may be missing them. |
| The user does something else mid-way | Selection changes after the preview was taken are re-verified by the token check; a mismatch means confirming again. | File actions are locked while an action runs — other requests get a "file actions are locked" refusal rather than interleaving. |
| A clean complete happens elsewhere | A completed action writes its receipt and releases the lock; a second action then previews from the new state. | Same lock applies; nothing runs concurrently. |
| The environment fails | A preflight failure (disk full surfaced as an unreadable destination, permission denied) excludes the affected files with reasons shown in the result. | A per-file failure is recorded on that file's item; the remaining files still move. The receipt records both outcomes. |
| The page or process goes away | Preview state lives in the server; reloading the browser loses the sheet but not the scan. | The server-side action continues or dies with the process; a death mid-action leaves some files moved and possibly no receipt. This is the product's one genuinely unsafe window. |
| Something else changes the target | That is what preflight and immediate revalidation catch: changed files are excluded with their reasons. | The immediate revalidation runs per file; a file that changes between its check and its move is a race too small to close entirely. |
| The input channel changes | No effect (UI); the CLI takes no stdin. | No effect. |
| A resumed review supersedes | A resumed session revalidates everything before the first preview, so resumed selections act under the same rules. | No effect during execution. |

After an interrupted or failed action the receipt (when written) is authoritative for what moved; `dedupe receipts show` displays it.

## Interactions with other systems

**Files on disk.** Quarantine creates its destination folder; isolate creates its `_Dedupe Review/session-…` tree; every executed trash/quarantine writes a receipt; undo writes a receipt. Full list in [Files Dedupe writes](../cross-cutting/caches-and-files.md).

**Safety and undo.** (This document.)

**Review sessions.** Actions act on the current result, whether it came from a fresh scan or a [resumed session](review-session.md); selections for moved files are cleared from the saved session afterwards.

**Optional dependencies.** None directly; actions move bytes. Thumbnails of moved files drop out of the preview cache naturally.

**Concurrency and resource limits.** Executed actions run their per-file work on a bounded thread pool with results kept in input order; names in the quarantine folder are reserved up front so parallel workers never collide. File actions are serialized against scans — both hold the same lock and refuse to overlap.

**macOS specifics.** Trash goes through the system trash for the file's volume. iCloud-evicted files fail revalidation (they are not readable) and are excluded rather than moved.

**Configuration and defaults.** Receipts directory `~/.cache/dedupe/logs/` (overridable per command); quarantine directory is always user-supplied; isolate's `_Dedupe Review` default location derives from the scanned roots.

## Edge cases

- A selection that spans overlapping groups (one file in an exact and a similar group) acts once; the last-survivor guarantee is evaluated against *every* duplicate group, including ones the action's filter does not include.
- Quarantining two files with the same name produces two distinct destination names; undo returns both to their distinct originals.
- Undo against a dry-run receipt is refused ("only executed quarantine receipts can be undone").
- Undo whose receipt was already undone reports each file as missing from quarantine and does nothing.
- Isolate in move mode is the only isolate mode that can lose data; it is refused across filesystem boundaries unless explicitly allowed.
- A file that became a symbolic link between scan and action is refused by name — acting on the link would silently act on its target.

## Open questions and verification

- The exact behavior when the server process is killed mid-action (is a partial receipt written?) is read from the code path, not reproduced by hand; reproducing it safely needs a disposable setup.
- The preview-token lifetime is a number to be confirmed in [Action sheet](../ui/action-sheet.md).
- Trash recovery via Finder's "Put Back" is a macOS behavior Dedupe relies on but does not control; its reliability across macOS versions is outside the product.

Verified against dedupe commit `2a6cede`.
