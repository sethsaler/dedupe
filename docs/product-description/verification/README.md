# Hand verification

The feature documents were written from the code and the tests. This directory is the protocol for checking them against the running product, one observable claim at a time.

## What is here

| File | Covers |
| --- | --- |
| [foundations.md](foundations.md) | `foundations/*` |
| [ui.md](ui.md) | `ui/*` |
| [cli.md](cli.md) | `cli/*` and `cross-cutting/*` |

Each file has one table per document. Each row is an item with a stable ID, a priority, what it needs, the claim with a link to the document section, the setup, numbered steps, the expected result, and a Result column. Items that cannot be checked by hand are listed under each document as "Not checkable by hand".

Priorities: **P1** is an established fact, a claim many documents depend on, or a suspected bug; **P2** is an ordinary claim; **P3** is a number, a color, or a timing.

## How to run a pass

1. Bring up the surface. Web UI: `.venv/bin/dedupe ui` in the source repo → `http://127.0.0.1:8765`. CLI: `.venv/bin/dedupe {subcommand}`. For a clean state: discard the saved review (Discard saved review in the UI, or delete `~/.local/state/dedupe/review-session.json`), and point scans at scratch folders under `/tmp` — never a real photo library.
2. Confirm the commit. Every document says `Verified against dedupe commit 2a6cede`. Run `git rev-parse --short HEAD` in `/Users/sethsaler/Documents/GitHub/dedupe`; if it differs, some failures will be drift, not defects.
3. Keep the documents open beside the product. Read the linked section before each item; the item is a summary, the section is the claim.
4. Work through P1 first across all files, then P2, then P3.
5. Record `pass`, `fail`, or `blocked` in the Result column, with a note for anything other than a clean pass. A fail is something the document says that the product does not do; a blocked item could not be run.
6. File every fail in [`bug-triage.md`](../bug-triage.md): if the entry exists, add a Status line quoting the item ID; if not, add an entry with the item ID under "Raised by". A fail is not automatically a product bug; sometimes the document is wrong. Say which in the Status line.
7. When every P1 and P2 item for a document has passed or been filed, change its row in the [coverage table](../README.md#coverage) from `drafted` to `verified`.

## Devices and conditions

- **Scratch media set.** A folder under `/tmp` with: two byte-identical images; a resized (similar) copy of one; one image under 1 MP; one image over 1 MP; optionally one short generated video (ffmpeg required). Recreate it fresh for sections that move files.
- **TTY vs pipe.** CLI progress and the `\r` behavior differ; items that mention the progress line need a real terminal.
- **Two tabs, one browser.** Enough for shutdown-grace and shared-state items. A second *browser* is not needed — the product is single-user, local-only.
- **Photos.app library.** For the refusal item: `~/Pictures/Photos Library.photoslibrary` (any macOS machine with Photos).
- **Busy port.** For `ui` port items: `python3 -m http.server 8765` first.
- **ffmpeg/OpenCV present.** The drafting machine has both (`dedupe doctor` says ready); degraded-mode items need them removed and are marked accordingly.

## Driving the product from a console or script

Most CLI items are commands with expected output and exit codes, and were run as a scripted first pass (see Results so far). The web UI items need a real browser: input must be real input (keyboard shortcuts, checkbox toggles, drag-and-drop), and the claims are about what is shown — countdowns, banners, toasts, green keeper highlighting — which no script can see. The browser devtools network tab can confirm preview tokens and dry-run payloads; use it to observe, not to gesture. The e2e test suite (`tests/test_browser_workflow.py`, run with `.venv/bin/pytest -m e2e`) covers some flows mechanically and is a useful adjunct, but passing e2e tests is not a substitute for the checklist.

## Results so far

**Scripted CLI pass, 2026-08-24**, on macOS (Darwin 27), against commit `e8969e4`, using `.venv/bin/dedupe` and a scratch media set in `/tmp/dedupe-verify`. Covered: `doctor` output and exit code, scan over the scratch set (summary, diagnostics, JSON output, bare-path shortcut), receipts list on an empty directory, isolate dry-run and execute from the JSON, undo eligibility refusals. Results are recorded in the Result columns of [cli.md](cli.md). What this pass did **not** cover: anything visual (progress-line behavior on a TTY, the web UI in a browser), anything destructive against a real state worth keeping (trash/quarantine executes were run only against scratch files), degraded-dependency behavior, and timing claims. No document is marked `verified` on the strength of this pass.

**Re-pin note.** The set was drafted at `e8969e4` and re-pinned to `2a6cede` after the triaged fixes B-01 to B-04 landed; the documents now describe the fixed behavior. The suspected-bug checklist rows written for them (NONHUMAN-04, SCAN-08, RECEIPTS-05, DOCTOR-04) have been rewritten to assert the post-fix behavior and are expected to pass against builds at or after `2a6cede`. Items recorded `pass` in the scripted pass were unaffected by the fixes except DOCTOR-01's output block, whose `Keep decisions path` line matches the post-fix label.

**e2e mechanical pass, 2026-08-24**, against `2a6cede` (`.venv/bin/pytest -m e2e`, 3 passed). The Playwright suite drives a real loopback server and a real browser, with console/page-error assertions, over three flows: (1) scan via the form, exact-group pre-selection (one selected, one keeper kept), lightbox open/close, path search and category tab filtering, and a dry-run Trash preview that moves nothing; (2) the low-resolution decision review's `←` Delete / `→` Keep with revisit-and-correct and review shortcuts inert while the confirmation sheet is open; (3) bulk selection with advanced filters. These give mechanical confidence in the claims behind SCANSET-03 (partial), GROUPS-04 (search filtering), LIGHTBOX-01 (button path only — Esc not exercised), LOWRES-01, and SHEET-01 (dry-run preview only). They are not a substitute for the checklist: nothing destructive was confirmed, no keyboard navigation (`j`/`k`/`[`/`]`), no resume banner, and no countdown were exercised. No document is marked `verified` on this pass.

**e2e mechanical pass, 2026-09-02**, against the post-improvement working tree (2026-09 UX phase) (`.venv/bin/pytest -m e2e`, 12 passed). The suite grew by five tests: `test_scan_setup_hints_and_exclusion_check`, `test_tabs_and_group_list_are_keyboard_navigable`, `test_lightbox_shows_metadata_and_toggles_selection`, `test_lightbox_zoom_swaps_to_full_resolution`, and `test_error_toasts_persist_until_dismissed`. The checklist rows they bear on (SCANSET-09 and -11, GROUPS-14 and -15, LIGHTBOX-04 to -06, TOAST-01) record `pass (e2e, 2026-09-02)` in their Result columns; rows without e2e coverage keep their `—`. Still not a substitute for the checklist: no resume banner, no per-candidate trash undo, no countdown, and nothing destructive was exercised. No document is marked `verified` on this pass.
