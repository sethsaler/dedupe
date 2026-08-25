# Files Dedupe writes

## Summary

Dedupe is careful with the user's media and comparatively free with its own bookkeeping: a scan never modifies a media file, but it reads and writes a small family of state files — a hash cache, a review session, keep decisions, and action receipts — and the actions add user-visible folders: the quarantine directory, `_Dedupe Quarantine`, and `_Dedupe Review`. This document is the owner of that inventory: every file and directory Dedupe creates, where it lives, when it is written and read, and what happens if it goes missing, corrupt, or too large. Other documents link here instead of restating it. The one asset Dedupe *reads but never writes* — the bundled YuNet model — is included because its absence changes behavior.

## The inventory

| What | Where | Written when | Safe to delete? |
| --- | --- | --- | --- |
| Hash cache | `~/.cache/dedupe/hashes.sqlite3` | End of every scan; distinct and manual-review marks as they happen | Yes — next scan redoes the work; learned decisions are lost |
| Review session | `~/.local/state/dedupe/review-session.json` | Scan completion; every persisted selection change; resume-time pruning | Yes — the app starts clean; the current review is lost |
| Keep decisions | `~/.local/state/dedupe/keep-decisions.json` | Low-resolution review decisions (Keep) | Yes — kept low-res files resurface in future scans |
| Receipts | `~/.cache/dedupe/logs/` | Every executed trash/quarantine action, and every undo | With care — deleting a receipt removes the undo path for that action |
| Quarantine directory | User-chosen (remembered in the browser) | When a Quarantine action executes | No — it holds quarantined files until undo |
| `_Dedupe Quarantine` | Beside the scanned root | When a Trash action includes low-res/random selections | No — it holds those files |
| `_Dedupe Review` | Inside the scanned source | When Isolate runs | Yes, when the user is done comparing — copies by default |
| Thumbnails | A cached transcode store (see open questions) | On demand, when a preview is requested | Yes — regenerated on demand |
| YuNet model | Bundled under `src/dedupe/assets/` with the installation | Never (shipped read-only) | No — the OpenCV person detector fails closed without it |

Plus one thing that is not a file at all: the browser's local storage remembers scan settings, recent folders, and the quarantine directory. Clearing browser state resets these to defaults silently.

## The hash cache

The cache is a SQLite database keyed by strong file identity — size, modification time, inode, and device — so an entry only applies while the file is byte-for-byte the one that was scanned; change the file and its cached identity stops matching. It stores the work of the expensive stages: exact hashes, image perceptual hashes and tile hashes, video fingerprints, person-detection verdicts, and face counts.

It is read at the start of a scan (hydration — matching files skip re-hashing entirely, which is why a second scan of the same library is mostly an inventory walk) and written at the end (`store_all`). Two kinds of *decisions* also live here, because they must outlive any single review session:

- **Distinct pairs** — a similar group marked *Mark as distinct* records its files as pairwise not-duplicates, and grouping refuses to reunite them while the entries are current.
- **Manual person reviews** — *Mark all remaining as human* records each file as manually confirmed human with detector `manual_review`; a manual decision outranks any detector version, but the same identity check still invalidates it when the file changes.

**When it goes wrong.** If the cache cannot be opened, the scan continues without it and says so ("Cache unavailable: …") — the result is complete, only slower. A cache *write* failure at the end of a scan is surfaced in the diagnostics, because its consequence is silent: the next scan redoes work it should have skipped. Deleting the file is safe and has exactly that consequence, plus the loss of distinct and manual-review decisions. The database is opened so concurrent access does not trip "database is locked", and old caches are migrated column-by-column rather than discarded.

## The review session file

The review session is the only file that holds a whole review: files, groups, selections, and reviewed paths, wrapped in a version-1 envelope with a `saved_at` stamp. It is written atomically — new content goes to a temporary file in the same directory, is synced to disk, and renamed over the old file — so a crash leaves either the old session or the new one, never a torn file. Permissions are private: `0600` for the file, `0700` for its directory, because it is a full inventory of the user's media paths. The directory honors `XDG_STATE_HOME`.

It is written when a scan completes and again on every persisted selection change; on resume it is read, revalidated, pruned of stale files, and immediately saved back so the same drops are never reported twice.

**When it goes wrong.** Three refusals, all at load time:

- **Missing** — the app starts clean; not an error.
- **Oversize** — a session larger than 64 MB is refused rather than parsed.
- **Corrupt or wrong version** — reported as corrupt with the underlying error, left on disk untouched for inspection, and the app starts clean. Nothing is guessed at, migrated, or partially read.

See [The review session](../foundations/review-session.md) for the full resume semantics.

## The keep-decisions file

A small durable store of explicit *Keep decisions*: low-resolution candidates the user reviewed and left unselected. Each decision is recorded against the file's identity; if the file changes, the decision stops applying. The file is read when low-resolution groups are built (kept files are skipped) and written as review decisions land — a Keep adds a decision, selecting the file for removal or withdrawing the review clears it. The store is treated as a convenience: a failure to write it never fails the selection request. Deleting the file is safe; the consequence is that kept low-resolution files resurface in the next scan.

## Receipts

Every executed trash or quarantine action writes a JSON receipt to `~/.cache/dedupe/logs/`, naming the action, the time, and every file with its outcome and destination; an undo writes its own receipt. Dry-run previews also write receipts, marked as previews — `dedupe receipts prune` can drop them separately, and `dedupe undo` refuses them ("only executed quarantine receipts can be undone"). Filenames encode the dry-run flag, a timestamp, and a session id; `dedupe receipts list` shows them newest first, `show` prints one, `prune` deletes old ones by age or count (dry-run by default, `--execute` to actually delete).

Deleting receipt files is safe for the product but destroys the undo path for the actions they describe — quarantine contents then have to be restored by hand.

## Quarantine and review directories

**The quarantine directory** for the Quarantine action is user-supplied and remembered in the browser; it is created at execute time if missing. Files keep their names; collisions get a unique suffix. This is where `dedupe undo` looks.

**`_Dedupe Quarantine`** is a special case the UI applies when a Trash action includes low-resolution or random-review selections: those files are quarantined instead of trashed, into `_Dedupe Quarantine` placed beside the scanned root (the first existing root directory, else its parent, else the parent of a selected file, else the working directory). The action sheet reports how many went there. See [Action sheet](../ui/action-sheet.md).

**`_Dedupe Review`** is what Isolate creates inside the scanned source (derived from the scanned roots; working directory as a last resort). Each run makes its own timestamped session folder, `session-{UTC stamp}-{short id}`, containing `exact/` and `similar/` subfolders, one folder per group named with its index, kind, media type, member count, keeper name, and group id. The suggested keeper is prefixed `KEEP__`, each group folder carries a `_group.json`, and the session root carries `_review_index.json`. Copy is the default mode — originals untouched; the tree is safe to delete when the user is done comparing.

## Thumbnails

Previews are generated on demand and cached: browser-safe formats serve untouched for full-size lightbox views; formats like HEIC/TIFF are transcoded to JPEG for thumbnails and full views; videos get a still or no preview at all. Cached entries are keyed by the source file's mtime and size and served with immutable cache headers, so a cached body can never be stale. Only files that belong to the active scan are ever served. The on-disk location of the transcode store is listed under open questions; deleting it costs a regeneration, nothing more.

## The YuNet model (read-only asset)

The OpenCV person detector reads a bundled YuNet face model shipped with the installation (its MIT license sits at `src/dedupe/assets/LICENSE-YUNET.txt`). Dedupe never writes it. If it is missing, corrupt, or cannot start, the no-person review **fails closed**: no media is surfaced as Non-Human. `dedupe doctor` reports this as `OpenCV/YuNet (optional): not ready`.

## What `doctor` does to these paths

`doctor` probes the three application paths — cache, review session, keep decisions — by creating their parent directories if missing and checking writability without ever touching the files themselves. Running `doctor` on a fresh machine is therefore enough to create `~/.cache/dedupe` and `~/.local/state/dedupe`. See [`doctor`](../cli/doctor.md).

## Interactions with other systems

This document *is* the files-on-disk entry of the cross-cutting list; the remaining concerns in their fixed order:

**Safety and undo.** Receipts are the undo mechanism's memory; the quarantine folders are where recoverable files live. Deleting either removes a recovery path — the only destructive consequence anywhere in this inventory.

**Review sessions.** The session file's full lifecycle is owned by [The review session](../foundations/review-session.md); this document covers only its existence, permissions, and failure modes.

**Optional dependencies.** The YuNet model belongs to the OpenCV backend; the thumbnail transcoder leans on the same media tooling. Degraded behavior per dependency is collected in [Optional dependencies](optional-dependencies.md).

**Concurrency and resource limits.** The cache is opened for concurrent access without lock errors; session writes are atomic; receipt names are collision-safe by construction.

**macOS specifics.** Paths follow `~/.cache` and `~/.local/state` conventions (with `XDG_STATE_HOME` honored for the state files); private permissions matter because the session file is a map of the user's media.

**Configuration and defaults.** None of the paths are configurable except via `XDG_STATE_HOME` (state files) and per-command directory flags (`receipts --log-dir`, isolate's review directory, quarantine's user-chosen directory).

## Edge cases

- Deleting the cache mid-scan is harmless to the result; the final store recreates it.
- A session saved by a newer version is refused as unsupported, not migrated.
- The same file can be named by many receipts; each receipt stands alone for undo purposes.
- Two isolate runs never merge: each gets its own session folder.
- `_Dedupe Quarantine` can be created by a Trash action even when the user never asked for quarantine — the action sheet says so in its result.

## Open questions and verification

- The thumbnail transcode store's on-disk location and any eviction policy (the source README describes it as a bounded least-recently-used cache) were not confirmed from `web/media.py` in this pass.
- The exact filename of the bundled YuNet model asset under `src/dedupe/assets/` was not confirmed.
- Whether the receipts directory itself is created with private permissions, like the session directory, was not confirmed.

Verified against dedupe commit `2a6cede`.
