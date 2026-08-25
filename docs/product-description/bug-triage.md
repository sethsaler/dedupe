# Bug triage

A consolidated list of the defects and inconsistencies that the feature documents raised in their "Open questions and verification" sections and in their bodies. Each entry was drafted from the dedupe source repository as of commit `e8969e4` and its tests; none has been confirmed by a hand-verification pass — the confirmation rows in the checklists ([verification/](verification/)) are still pending. Every entry was filed as an issue on [sethsaler/dedupe](https://github.com/sethsaler/dedupe) on 2026-08-24 (#3–#7); the Issue lines link them. B-01 through B-04 were subsequently fixed in the source repo (commits `719cf43`–`2a6cede`), and this set of documents was re-pinned to `2a6cede` and revised to describe the fixed behavior; B-05 was resolved by a product call as a copy-only preview change (`518b958`, post-pin, no behavior change). The list exists so the product team can decide, item by item, whether to fix, to document as intended, or to leave.

## Summary

Fifty-odd open questions across twenty-one documents collapsed to five entries after merging. Two are medium: one loses a promised undo path (the per-candidate restore), the other meets every CLI user who presses Ctrl+C. Three are low — two polish issues in the receipts and doctor commands, one copy decision. One entry is a product call rather than a fix. The common thread in the mediums: recovery paths that exist in memory but were not made durable.

| ID | Title | Severity | Area | Decision needed | Issue |
| --- | --- | --- | --- | --- | --- |
| B-01 | Per-candidate trash undo vanishes when the server restarts | medium | ui (Non-Human / Faces) | fix (done) | [#3](https://github.com/sethsaler/dedupe/issues/3) |
| B-02 | Ctrl+C during a CLI scan exits with a raw KeyboardInterrupt traceback | medium | cli | fix (done) | [#4](https://github.com/sethsaler/dedupe/issues/4) |
| B-03 | Corrupt receipts are invisible to `receipts list` and survive `prune` | low | cli | fix (done) | [#5](https://github.com/sethsaler/dedupe/issues/5) |
| B-04 | `doctor` prints the keep-decisions label as "Keep_Decisions" | low | cli | fix (done) | [#6](https://github.com/sethsaler/dedupe/issues/6) |
| B-05 | The review-quarantine split of Trash is only explained after the fact | low | ui | product call (done: keep one button, lead with the split) | [#7](https://github.com/sethsaler/dedupe/issues/7) |

## Medium

### B-01: Per-candidate trash undo vanishes when the server restarts

- **Where the user meets it:** Trashing a single candidate in the Non-Human or Faces review, then restarting the app (or letting the server auto-stop when the tab closes, as it does by design).
- **What happens / what was expected:** After the restart, the candidate's card no longer offers the undo control, although the file is still sitting in the macOS Trash and the user was told it could be restored. Expected: the restore stays available for as long as the file is recoverable, or the UI says the restore window has ended.
- **Reproduce:** 1. Scan with no-person detection over people-free images. 2. Trash one candidate from its card. 3. Stop the server and relaunch `dedupe ui`. 4. Open the Non-Human tab and inspect the trashed candidate's card.
- **Why (from the code):** `src/dedupe/web/app.py` keeps the trash destinations in `state["deleted_files"]`, an in-memory dict initialized empty in `create_app` and cleared on every new scan and resume. The undo endpoint `/api/review-candidate/undo` answers "there is no deleted file to undo" when the path is not in that dict, and the client only renders the undo control from the `deleted_paths` payload derived from it. Nothing persists the map.
- **Severity:** `medium`. Wrong but recoverable — Finder's Trash still has the file — but a promised undo step is missing, which fits the medium definition exactly.
- **Decision needed:** `fix`. Persist `deleted_files` (it is small: path → Trash destination) in the review session envelope, or at least show a "restore from Finder" note on deleted candidates after a restart.
- **Raised by:** [ui/no-person-review.md](ui/no-person-review.md#open-questions-and-verification), [ui/faces-review.md](ui/faces-review.md#open-questions-and-verification)
- **Status:** Fixed in [sethsaler/dedupe@2a6cede](https://github.com/sethsaler/dedupe/commit/2a6cede): the trash map is persisted in the review session envelope and restored on startup/resume, and trashed candidates are exempt from resume pruning; covered by `test_candidate_trash_undo_survives_a_restart`. Checklist item NONHUMAN-04 should pass against builds at or after this commit.
- **Issue:** [sethsaler/dedupe#3](https://github.com/sethsaler/dedupe/issues/3)

### B-02: Ctrl+C during a CLI scan exits with a raw KeyboardInterrupt traceback

- **Where the user meets it:** Any `dedupe scan` interrupted in a terminal — the most routine abort there is.
- **What happens / what was expected:** A Python traceback ending in `KeyboardInterrupt` and an exit code of 1. Expected: a clean "scan cancelled" line and, ideally, the partial diagnostics gathered so far.
- **Reproduce:** 1. `dedupe scan` a folder large enough to take several seconds. 2. Press Ctrl+C during the hashing phase.
- **Why (from the code):** `src/dedupe/cli.py` `main()` (lines 901–925) dispatches to `cmd_scan` with no `KeyboardInterrupt` handling anywhere in the chain; the engine's cooperative-cancel machinery (`cancelled` callback, `InterruptedError`) is only wired up in the web app, which passes `cancel_event.is_set`. The CLI passes no `cancelled` and installs no signal handler.
- **Severity:** `medium`. Nothing is damaged — scans are safe to interrupt — but one common action produces an ugly, confusing failure mode every time.
- **Decision needed:** `fix`. Catch `KeyboardInterrupt` in `main()` (or wire the same cooperative cancel to SIGINT in `cmd_scan`) and print "scan cancelled".
- **Raised by:** [cli/scan.md](cli/scan.md#open-questions-and-verification)
- **Status:** Fixed in [sethsaler/dedupe@caa26e9](https://github.com/sethsaler/dedupe/commit/caa26e9): `main()` now catches `KeyboardInterrupt` and prints "cancelled" with exit code 130; covered by `test_main_reports_keyboard_interrupt_cleanly`. Checklist item SCAN-08 should pass against builds at or after this commit.
- **Issue:** [sethsaler/dedupe#4](https://github.com/sethsaler/dedupe/issues/4)

## Low

### B-03: Corrupt receipts are invisible to `receipts list` and survive `prune`

- **Where the user meets it:** A damaged receipt file (interrupted write, hand edit) in `~/.cache/dedupe/logs/`.
- **What happens / what was expected:** The file appears in no listing, cannot be inspected, and cannot be pruned by any rule — it is simply gone from the product's view while still on disk. Expected: either surface it ("1 unreadable receipt") or let `prune` clean it.
- **Reproduce:** 1. `echo 'not json' > ~/.cache/dedupe/logs/action-broken.json`. 2. `dedupe receipts list` (absent). 3. `dedupe receipts prune --drop-previews --execute` (still present on disk).
- **Why (from the code):** `src/dedupe/receipts.py` `summarize_receipt` returns `None` for anything unparseable and `list_receipts` skips `None` summaries; `prune_receipts` iterates the same parsed summaries, so unparsed files match no rule and are never unlinked.
- **Severity:** `low`. A quirk only an expert notices; no data at risk.
- **Decision needed:** `fix`. Count and report unreadable receipts in `list` (and `--json`), and let `prune --execute` remove them.
- **Raised by:** [cli/receipts.md](cli/receipts.md#open-questions-and-verification)
- **Status:** Fixed in [sethsaler/dedupe@42be7b0](https://github.com/sethsaler/dedupe/commit/42be7b0): `receipts list` warns about unreadable receipts (names to stderr) and `prune` with any criterion removes them with reason "unreadable receipt"; covered by `test_unreadable_receipts_are_visible_and_prunable` and `test_receipts_list_warns_about_unreadable_receipts`.
- **Issue:** [sethsaler/dedupe#5](https://github.com/sethsaler/dedupe/issues/5)

### B-04: `doctor` prints the keep-decisions label as "Keep_Decisions"

- **Where the user meets it:** Every `dedupe doctor` run, in the path block.
- **What happens / what was expected:** `Keep_Decisions path: …` — an odd capitalized label among otherwise plain ones (`Cache path`, `State path`). Expected: `Keep decisions path: …`.
- **Reproduce:** `dedupe doctor`; read the third path line.
- **Why (from the code):** `src/dedupe/cli.py` `cmd_doctor` prints `f"{name.title()} path: …"` over the key `keep_decisions`; `str.title()` turns the underscore into a word boundary and capitalizes both halves.
- **Severity:** `low`. Cosmetic copy slip.
- **Decision needed:** `fix`. Map the key to a display label instead of title-casing it.
- **Raised by:** [cli/doctor.md](cli/doctor.md#open-questions-and-verification)
- **Status:** Fixed in [sethsaler/dedupe@719cf43](https://github.com/sethsaler/dedupe/commit/719cf43): path labels use a display map, printing `Keep decisions path:`; covered by `test_doctor_plain_output_uses_plain_path_labels`. Checklist item DOCTOR-04 is obsolete for builds at or after this commit.
- **Issue:** [sethsaler/dedupe#6](https://github.com/sethsaler/dedupe/issues/6)

### B-05: The review-quarantine split of Trash is only explained after the fact

- **Where the user meets it:** The first time a Trash action includes low-resolution or random-review selections: those files do not go to the macOS Trash but to a `_Dedupe Quarantine` folder beside the scan root.
- **What happens / what was expected:** The action sheet's preview does state the split (count and destination), but the user asked for "Trash"; two destinations for one button is a surprise that reads as inconsistency. Expected: either the preview leads with the split (before the counts), or the button's label/copy prepares for it.
- **Reproduce:** 1. Scan a fixture with a sub-1 MP image. 2. Delete the candidate in Low-res (`←`). 3. Open the Trash preview with nothing else selected and read the sheet.
- **Why (from the code):** `src/dedupe/web/app.py` `/api/action` partitions the effective selection — `review_paths` from `LOW_RESOLUTION`/`RANDOM_REVIEW` groups go to `review_quarantine_dir` as a quarantine part, everything else to the system trash — by deliberate design (review removals stay recoverable without relying on Finder habits). The client renders the note inside the preview body.
- **Severity:** `low`. Behavior is safe and documented in the preview; the issue is framing.
- **Decision needed:** `product call`. Keep one button with the split explained in the preview (status quo, possibly reworded), or split the UI into two explicit actions.
- **Raised by:** [ui/action-sheet.md](ui/action-sheet.md#open-questions-and-verification), [ui/low-res-review.md](ui/low-res-review.md#while-extended), [ui/random-review.md](ui/random-review.md#while-extended)
- **Status:** Resolved as a product call on 2026-08-24: keep one Trash button and make the split visible — the review-quarantine note now leads the Trash preview sheet whenever it applies ([sethsaler/dedupe@518b958](https://github.com/sethsaler/dedupe/commit/518b958), copy-only, post-pin). The documents' claim that the preview states the split remains correct.
- **Issue:** [sethsaler/dedupe#7](https://github.com/sethsaler/dedupe/issues/7)
