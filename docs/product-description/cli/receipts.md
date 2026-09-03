# `receipts`

## Summary

`receipts` is the inspector for everything the action system has recorded: every executed trash, quarantine, isolate, and undo writes a JSON receipt, and this command lists them, opens one in detail, and prunes the old ones. It is read-only over the receipts directory — the receipts themselves are what [`undo`](undo.md) consumes and what [Actions and undo](../foundations/actions-and-undo.md#the-safety-model) promises.

## The simple case

`dedupe receipts list` prints one line per receipt, newest first: id, timestamp, action, executed vs preview, ok/failed counts, bytes, and an `undoable` flag where it applies. `dedupe receipts show {id}` prints one receipt's full item list. `dedupe receipts prune --older-than 30` previews which old receipts would be deleted; adding `--execute` deletes them.

## The interaction, event by event

All three subcommands are short-lived invocations with no extended phase: parse, read the receipts directory, print, exit.

### Invoke

`receipts` requires one of `list`, `show`, or `prune`. Shared flags: `--log-dir DIR` (default `~/.cache/dedupe/logs`) and `--json` for machine-readable output. Receipt ids are the receipt filenames without `.json`; ids, filenames, full paths, and unique id substrings are all accepted wherever a receipt is referenced.

### Exit immediately

A bare `dedupe receipts` exits 2 with a usage message. `show` against a missing or unreadable receipt exits 2 with `error: …`. An empty directory makes `list` print "No action receipts found." and exit 0.

### Begin running

The directory is read synchronously. Corrupt receipts — unparseable JSON, wrong shape — cannot be summarized, so `list` reports them instead of hiding them: a warning on stderr names them (first five, with a count). `prune` in preview mode touches nothing.

### While running

**`list`** — flags: `--limit N` (default 20), `--no-previews` (hide dry-run receipts), `--undoable` (only receipts `undo` can consume). Each line: `{id}  {timestamp}  {action}  {executed|preview}  {N ok / M failed}  {bytes}{ undoable}` — where the timestamp comes from the receipt's `started_at` and is blank when the receipt predates that field (older receipts then show only the id and two spaces). Undoability has exact rules: executed trash and quarantine receipts where every moved item has a real recorded destination (a trashed item recorded as the bare "Trash" marker disqualifies the whole receipt, because one blocked item refuses the whole undo); dry-run previews, isolate receipts, undo receipts, and receipts with no successfully moved items are not undoable, each for a stated reason available in `--json`.

**`show`** — prints `Receipt: {id}`, the path, `Action: {action} ({executed|preview})`, `Started: … Completed: …`, `Result: N ok, M failed`, then one line per item (`[ok  ]` or `[failed]`, source, destination), `--items N` capping the list (default 20, 0 = all). Isolate receipts also show the review root. With `--json`, the raw receipt object.

**`prune`** — flags: `--older-than DAYS`, `--keep N` (retain only the N newest), `--drop-previews` (every dry-run receipt), and `--execute`. Without `--execute` the command is a preview: `DRY-RUN prune: N receipts (bytes freed), M kept` plus up to twenty removed ids with their reasons ("… +N more"). Reasons reflect the rule that matched: age, keep-window, or preview. A receipt is removed when it is older than `--older-than`, or falls outside the `--keep` newest, or is a preview with `--drop-previews` set.

### Finish

`prune --execute` deletes the matching receipt files and reports `EXECUTED prune: …` with the same detail; per-file errors are printed and set the exit code. `list` and `show` exit 0 on success; `show` exits 2 on a bad reference; `prune` exits 1 if any deletion errored, else 0.

## Modifiers

| Modifier | Set at the start | Changed while extended |
| --- | --- | --- |
| `--log-dir DIR` | Which receipts directory to operate on; ids resolve against it. | Fixed. |
| `--json` | Machine-readable output for all three subcommands. | Fixed. |
| `--execute` (prune) | Preview vs deletion. | Fixed. |
| stdout piped | Plain text either way. | No effect. |

## Cancel and interrupt

| Event | Before deletion | During deletion |
| --- | --- | --- |
| The user aborts explicitly (Ctrl+C) | Nothing deleted (preview mode deletes nothing anyway). | Some receipts deleted, others kept; each deletion is an independent unlink, so the directory is always a valid, if partial, result. |
| The user does something else mid-way | Not applicable. | Not applicable. |
| A clean complete happens elsewhere | Not applicable. | A concurrent action writing a new receipt does not interfere; the new receipt is simply not in the prune set. |
| The environment fails | A missing or unreadable directory lists nothing rather than erroring. | A per-file deletion error is reported and the exit code is 1. |
| The page or process goes away | No effect. | Same as Ctrl+C. |
| Something else changes the target | A receipt deleted elsewhere between listing and execution is already gone; the unlink is a no-op error. | Same. |
| The input channel changes | stdin unused. | No effect. |
| A resumed review supersedes | No effect. | No effect. |

## Interactions with other systems

**Files on disk.** Reads the receipts directory; `prune --execute` deletes files. The directory itself is `~/.cache/dedupe/logs` by default; its full description is in [Files Dedupe writes](../cross-cutting/caches-and-files.md).

**Safety and undo.** Pruning an undoable receipt destroys its ability to be undone — the command does not warn about this specifically, only lists `undoable` receipts as such in `list`. This is the one place the user can out-flank the safety model, deliberately.

**Review sessions.** None.

**Optional dependencies.** None.

**Concurrency and resource limits.** Directory scans are bounded by the receipt count; `--limit` bounds `list` output.

**macOS specifics.** None.

**Configuration and defaults.** Default directory, default `--limit 20`, default `--items 20`, preview-by-default for prune.

## Edge cases

- Unique id substrings resolve: the eight-character session prefix of a receipt id is enough for `show` and for [`undo`](undo.md).
- An ambiguous substring fails with a not-found error rather than guessing.
- Corrupt receipts sit outside the parsed rules entirely — no age, no keep-window position, no preview flag — so `prune` removes them with the reason "unreadable receipt" whenever *any* criterion is active, and leaves them alone when none is.
- `list --undoable --no-previews` is the practical query before choosing what to prune.

## Open questions and verification

- The warning for unreadable receipts prints to stderr in plain mode; `--json` output keeps its array shape and the warning rides alongside on stderr — confirm this is what scripts expect.
- `prune`'s interaction between `--keep` and `--drop-previews` (whether previews count toward the keep window) was not determined from the output format; the implementation combines the rules independently.

Verified against dedupe commit `2a6cede`.
