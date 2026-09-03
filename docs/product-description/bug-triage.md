# Bug triage

A consolidated list of the defects and inconsistencies that the feature documents raised in their "Open questions and verification" sections and in their bodies. Each entry was drafted from the dedupe source repository as of commit `e8969e4` and its tests; none has been confirmed by a hand-verification pass — the confirmation rows in the checklists ([verification/](verification/)) are still pending. Every entry was filed as an issue on [sethsaler/dedupe](https://github.com/sethsaler/dedupe) on 2026-08-24 (#3–#7); the Issue lines link them. B-01 through B-04 were subsequently fixed in the source repo (commits `719cf43`–`2a6cede`), and this set of documents was re-pinned to `2a6cede` and revised to describe the fixed behavior; B-05 was resolved by a product call as a copy-only preview change (`518b958`, post-pin, no behavior change). B-06 came from a user report on 2026-09-02 and was fixed the same day. The list exists so the product team can decide, item by item, whether to fix, to document as intended, or to leave.

## Summary

Fifty-odd open questions across twenty-one documents collapsed to five entries after merging, plus one user-reported entry (B-06). Three are medium: one loses a promised undo path (the per-candidate restore), one meets every CLI user who presses Ctrl+C, and one made the decision reviews' staged deletions impossible to execute. Three are low — two polish issues in the receipts and doctor commands, one copy decision. One entry is a product call rather than a fix. The common thread in the mediums: paths the product promises that the implementation did not actually offer. The 2026-09-03 verification passes added four more entries (B-07 through B-10), all fixed in the same pass: two CLI output gaps, one cancel-path crash with a deadlock sibling, and one focus-trap race.

| ID | Title | Severity | Area | Decision needed | Issue |
| --- | --- | --- | --- | --- | --- |
| B-01 | Per-candidate trash undo vanishes when the server restarts | medium | ui (Non-Human / Faces) | fix (done) | [#3](https://github.com/sethsaler/dedupe/issues/3) |
| B-02 | Ctrl+C during a CLI scan exits with a raw KeyboardInterrupt traceback | medium | cli | fix (done) | [#4](https://github.com/sethsaler/dedupe/issues/4) |
| B-03 | Corrupt receipts are invisible to `receipts list` and survive `prune` | low | cli | fix (done) | [#5](https://github.com/sethsaler/dedupe/issues/5) |
| B-04 | `doctor` prints the keep-decisions label as "Keep_Decisions" | low | cli | fix (done) | [#6](https://github.com/sethsaler/dedupe/issues/6) |
| B-05 | The review-quarantine split of Trash is only explained after the fact | low | ui | product call (done: keep one button, lead with the split) | [#7](https://github.com/sethsaler/dedupe/issues/7) |
| B-06 | Staged Low-res/Random deletions had no action entry point in the UI | medium | ui (action bar) | fix (done) | — |
| B-07 | A busy UI port surfaced werkzeug's notice + an uncaught `SystemExit` | low | cli (ui) | fix (done) | — |
| B-08 | `isolate` printed no per-item failure reasons on an all-or-nothing cancel | low | cli (isolate) | fix (done) | — |
| B-09 | Cancelling a parallel-streams scan crashed the merge loop (`UnboundLocalError`) | medium | engine | fix (done) | — |
| B-10 | Fast Tabbing could escape an overlay's focus trap mid-repaint | low | ui (a11y) | fix (done) | — |

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
- **Status:** Resolved as a product call on 2026-08-24: keep one Trash button and make the split visible — the review-quarantine note now leads the Trash preview sheet whenever it applies ([sethsaler/dedupe@518b958](https://github.com/sethsaler/dedupe/commit/518b958), copy-only, post-pin). Regressed once when `d9702af` ("simplify duplicate deletion actions") dropped the note from the sheet body; restored in the 2026-09 improvement phases, which also added the count to the execute-result toast. Note that between `d9702af` and the B-06 fix the note was unreachable in practice, because no action-bar scope covered review selections; B-06 restored the entry point. The documents' claim that the preview states the split remains correct.
- **Issue:** [sethsaler/dedupe#7](https://github.com/sethsaler/dedupe/issues/7)

## Medium (user-reported)

### B-06: Staged Low-res/Random deletions had no action entry point in the UI

- **Where the user meets it:** After marking Low-res or Random review candidates Delete (`←`), trying to actually remove the staged files.
- **What happens / what was expected:** The action bar offered only the Exact and Similar scopes; staged review selections counted toward neither, so no button, shortcut, or menu could execute them and the files never moved. Expected: a visible way to confirm the staged deletions the review UI collects.
- **Reproduce:** 1. Scan a folder with sub-1 MP images. 2. In the Low-res tab, press `←` on some candidates ("1 marked Delete"). 3. Look for a way to delete the staged files — both action-bar buttons stay disabled for this scope.
- **Why (from the code):** `d9702af` ("ui: simplify duplicate deletion actions") replaced the action bar's scope selector — which included "Low-res + Random review" — with two hard-wired buttons, and the client thereafter only sent `kinds: "exact"` or `"similar"`. The server retained full support for `kinds: "review_suggestions"` including the `_Dedupe Quarantine` split (covered by `test_independent_review_decision_is_persisted_and_actionable` and `test_trash_routes_review_decisions_to_dedicated_quarantine`), and the client's quarantine-note rendering survived as unreachable code.
- **Severity:** `medium`. Nothing is damaged — the files stay in place and the selections persist — but the decision reviews' core promise ("Delete") could not be completed from the UI.
- **Decision needed:** `fix` (done). Restore a scoped entry point rather than re-mixing scopes into one button: a third action-bar button wired to the server's existing `review_suggestions` scope.
- **Raised by:** user report, 2026-09-02; [ui/action-sheet.md](ui/action-sheet.md), [ui/low-res-review.md](ui/low-res-review.md#while-extended), [ui/random-review.md](ui/random-review.md#while-extended)
- **Status:** Fixed in the working tree on 2026-09-02 (uncommitted at writing): a third action-bar button **Delete All Selected Low-res + Random** opens the Trash sheet scoped to review suggestions, leading with the `_Dedupe Quarantine` destination and a **Move to Quarantine** confirm; the decision-review summary line now names the button. Covered by `test_bulk_action_ui_separates_exact_matches_from_similars` and the extended e2e `test_low_resolution_review_uses_left_delete_and_right_keep`. Checklist item SHEET-01 has been rewritten to match.
- **Issue:** none filed (reported directly).

## Found by the 2026-09-03 verification passes (scripted + e2e)

### B-07: A busy UI port surfaced werkzeug's notice + an uncaught `SystemExit`

- **Where the user meets it:** Starting `dedupe ui` while something else already listens on the port (e.g. a previous Dedupe UI that never stopped).
- **What happens / what was expected:** werkzeug printed "Address already in use / Port … is in use by another program…" and raised `SystemExit(1)`, which no layer caught. Expected ([cli/ui-command.md](cli/ui-command.md#exit-immediately), checklist UICMD-03): a deliberate error and exit. A scripting subtlety made this worse to pin down: `python3 -m http.server 8765` (the checklist's blocker) binds the *wildcard* address, and the UI binds `127.0.0.1` specifically — with `SO_REUSEADDR` on both, the two binds coexist on macOS and the UI actually works; only a loopback-bound blocker reproduces the failure.
- **Reproduce:** 1. `python3 -m http.server --bind 127.0.0.1 8765`. 2. `dedupe ui`.
- **Why (from the code):** `werkzeug.serving.make_server` converts the bind `OSError` into a printed message plus `SystemExit`; `cli.py` `cmd_ui` called `run_app` with no guard, and `main()` catches only `KeyboardInterrupt`.
- **Severity:** `low`. Nothing damaged; an ugly failure on a routine condition.
- **Decision needed:** `fix` (done). `cmd_ui` now probes the port with a plain bind (same `SO_REUSEADDR` semantics) before starting and prints `error: port N is not available (Address already in use) — is another Dedupe UI already running?`, exit 2; `run_app` also binds before printing the URL line so the URL never prints for a server that failed to start.
- **Raised by:** checklist UICMD-03, scripted pass 2026-09-03.
- **Status:** Fixed in the working tree on 2026-09-03; covered by `test_ui_on_a_busy_port_exits_cleanly`.
- **Issue:** none filed (found and fixed in the same pass).

### B-08: `isolate` printed no per-item failure reasons on an all-or-nothing cancel

- **Where the user meets it:** `dedupe isolate results.json --execute` when a scanned file changed afterwards.
- **What happens / what was expected:** The command printed `EXECUTED isolate: 0 ok, 13 failed` and a log path — but not *why*, although [cli/isolate.md](cli/isolate.md#begin-running) promises "every item is reported failed, either with its own reason or with 'isolate cancelled because another file failed preflight'" and `dedupe undo` prints exactly such `failed:` lines. The reasons existed only inside the receipt JSON.
- **Reproduce:** 1. Scan a duplicate pair with `--json out.json`. 2. Modify one file. 3. `dedupe isolate out.json --execute`.
- **Why (from the code):** `cli.py` `cmd_isolate` printed the summary, review root, and folder list but never iterated `action_result.items` for failures.
- **Severity:** `low`. The cancel itself was correct (nothing placed); the user just could not see the reason without opening the receipt.
- **Decision needed:** `fix` (done). `cmd_isolate` now prints `  failed: {path}: {reason}` per failed item, matching `cmd_undo`.
- **Raised by:** checklist ISOLATE-04, scripted pass 2026-09-03.
- **Status:** Fixed in the working tree on 2026-09-03; covered by `test_isolate_prints_per_item_failure_reasons`.
- **Issue:** none filed (found and fixed in the same pass).

### B-09: Cancelling a parallel-streams scan crashed the merge loop (`UnboundLocalError`)

- **Where the user meets it:** Pressing Cancel (or Ctrl+C, after the cooperative-cancel fix) on a multi-folder scan with parallel streams on — the default whenever streams are enabled.
- **What happens / what was expected:** The scan worker died with `UnboundLocalError: cannot access local variable 'sub'`, surfacing a sticky error toast with that Python-internals message. The previous results were still restored (the cancel wrapper caught it), so the visible harm was the cryptic error instead of a clean "Scan cancelled". Expected: a clean cancel.
- **Reproduce:** 1. Start a default (parallel-streams) scan over a large folder in the UI. 2. Press Cancel mid-run. Surfaced mechanically by the e2e row SCANSET-04 (`test_scan_streams_groups_and_cancel_restores_previous`).
- **Why (from the code):** `engine.py` `run_scans_parallel` caught `InterruptedError` from a stream future and set `interrupted = True` but did not `continue`, then tried to merge the unbound `sub`. A sibling hazard existed in `run_scan`'s dimensions stage, which set its done-event in a `finally` that did not cover its own cancel check — a cancel there hung the review stage's wait and deadlocked the stage pool (found by the scripted Ctrl+C pass on the CLI, which had just gained cooperative cancel).
- **Severity:** `medium`. A routine action (cancel) produced a crash message; the deadlock variant could strand a scan indefinitely.
- **Decision needed:** `fix` (done). The merge loop skips cancelled futures; the dimensions stage's `finally` now covers its waits and cancel check; cancelled scans also persist completed stage work to the hash cache as the cancel propagates, so a rerun reuses it (the behavior [scan-pipeline.md](foundations/scan-pipeline.md#cancellation-and-failure) always promised).
- **Raised by:** checklist SCANSET-04 / SCANP-09, scripted + e2e passes, 2026-09-03.
- **Status:** Fixed in the working tree on 2026-09-03; covered by `test_parallel_streams_cancel_raises_interrupted_not_unbound` (fails without the fix, verified by reverting) and `test_run_scan_cancel_midway_raises_promptly`.
- **Issue:** none filed (found and fixed in the same pass).

### B-10: Fast Tabbing could escape an overlay's focus trap mid-repaint

- **Where the user meets it:** Holding or tapping Tab rapidly inside the lightbox or the help sheet.
- **What happens / what was expected:** Very occasionally, focus landed on the page behind the overlay. Expected ([lightbox.md](ui/lightbox.md#start), checklist LIGHTBOX-03): Tab is trapped while the overlay is open, always.
- **Reproduce:** Mechanical only: the e2e row LIGHTBOX-03 failed in some full-suite runs, never alone. A re-render that momentarily hides the overlay's controls empties the trap's focusable set; the un-prevented Tab then falls through to the browser's native focus order.
- **Why (from the code):** `trapTabKey` (static/util.js) returns false when it finds nothing to cycle, and the keyboard handlers in static/keyboard.js did not `preventDefault` on that path.
- **Severity:** `low`. Rare, timing-dependent, and self-correcting on the next Tab — but focus traps are an a11y promise.
- **Decision needed:** `fix` (done). Both overlay Tab handlers now swallow the key when the trap has nothing to cycle.
- **Raised by:** checklist LIGHTBOX-03 flake, e2e suite, 2026-09-03.
- **Status:** Fixed in the working tree on 2026-09-03; three consecutive full e2e runs green after the fix (previously failing intermittently).
- **Issue:** none filed (found and fixed in the same pass).
