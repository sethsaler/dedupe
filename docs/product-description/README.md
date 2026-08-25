# Dedupe product description

A written description of the user experience of Dedupe: what the user sees, what they can do, and exactly what happens when they do it.

## Purpose

Dedupe is, from the user's point of view, a large state chart. The user moves through it with folder paths, scan runs, selection changes, keyboard shortcuts, and confirmed actions. Most of that behavior is defined implicitly, spread across the scan engine, the review session store, the Flask server, the browser app, and the tests. There is no single place that says, in plain language, "when the user does X, this is what happens, and this is what happens if they do Y halfway through."

This project is that place. It describes the full experience a user has of Dedupe on macOS with the default installation: the local web UI at `http://127.0.0.1:8765` and the `dedupe` command, with nothing customized and optional dependencies (ffmpeg, OpenCV) present as `dedupe doctor` reports them.

The documents are for people who need to understand or change the product: designers, engineers, writers, testers, and anyone evaluating whether a behavior is intentional. They are written from the outside in. They describe the experience, not the implementation.

### What this is not

- Not API documentation. The HTTP endpoints between the browser and the server are internal; they are described only where they change what the user sees.
- Not organized by package. `engine.py`, `grouping.py`, `actions.py` are not described separately. A single behavior is described once, wherever the user encounters it.
- Not a technical design document. Where a technical detail is critical to understanding the experience, it appears in a block quote labeled `Technical note:` and nowhere else.

## Conventions

- Describe the experience, not the code. "The group keeps its suggested keeper no matter what the browser asks for" rather than "the server re-derives the selection."
- Technical detail goes in block quotes, prefixed with `Technical note:`. Use it only when the mechanism changes what the user would expect.
- Use sentence case for headings.
- Name the vocabulary consistently. The [glossary](glossary.md) is the source of truth for terms like *duplicate group*, *keeper*, *selection*, *review session*, *receipt*, *isolate*, and *revalidation*.
- Every document ends with the commit of the source repo it was verified against and a list of open questions.
- When a behavior is surprising, say so and say why it is that way if the reason is known. Do not smooth it over.

## The work to be done

Each document describes one feature. Features are large things (the group review list, the scan pipeline) or small things (`doctor`, the lightbox), but each is described in full, including its edge cases and its interactions with other features.

### Document template

Every feature document follows the same skeleton so that documents are comparable and nothing is skipped. Dedupe has two entry points that share one engine, so the skeleton has two dialects: CLI documents describe an *invocation*, UI documents describe a *task* in the browser. Both use the same five slots, renamed per surface, the same interrupt list, and the same cross-cutting order.

1. **Summary.** One paragraph describing the feature abstractly. For example: "`doctor` checks whether Dedupe has everything it needs to run and reports each dependency and writable path as ready or not."
2. **The simple case.** The common path in prose.
3. **The interaction, event by event.** The five phases of the unit of interaction. For a CLI invocation: **invoke** (argument parsing, what is validated, what prints first), **exit immediately** (`--help`, usage errors: exit code, stderr vs stdout), **begin running** (first side effect, first progress output, when Ctrl+C becomes unsafe), **while running** (progress, what is written when, idempotence), **finish** (final output, exit code, what landed on disk). For a UI task: **start** (what opens it, what is prefilled, what is validated), **end without changing anything** (close, Escape: is anything recorded?), **become extended** (the first change or the scan's first side effect), **while extended** (what streams, what is recomputed, what the user can still do), **complete** (what is confirmed and committed, the failure path). Include a small state diagram (Mermaid `stateDiagram-v2`) of the states the user passes through.
4. **Modifiers.** A table of the surface's variant axis — for CLI: flags, TTY vs pipe, optional dependencies present, prior state on disk; for UI: keyboard shortcuts, filter and selection state, a saved review session, detection engine availability — and what each one does when set at the start and when changed *during* the interaction.
5. **Cancel and interrupt.** The same checklist in every document, in this order:
   - The user aborts explicitly: Ctrl+C (CLI), Escape, a Cancel button, stopping a scan.
   - The user does something else mid-way: switches review category, changes a filter, starts another scan, applies a selection rule.
   - A clean complete happens elsewhere: another action is confirmed, a keyboard shortcut commits, `undo` runs.
   - The environment fails: disk full, permission denied, a media file unreadable or corrupt, an optional dependency missing, the server process dies.
   - The page or process goes away: browser reload or tab closed, terminal window closed, app restarted.
   - Something else changes the target: a file is changed, moved, or deleted on disk after the scan; a path becomes a symbolic link; a file ends up outside the scanned roots.
   - The input channel changes: stdin or stdout closed (CLI); no counterpart in the UI — the cell says "No effect."
   - A resumed review supersedes: a saved review session is restored and stale entries are pruned.
6. **Interactions with other systems.** In this fixed order: **files on disk** (caches, receipts, session files, `_Dedupe Review`), **safety and undo** (trash, quarantine, isolate, receipts), **review sessions**, **optional dependencies** (ffmpeg, OpenCV, Photon), **concurrency and resource limits**, **macOS specifics** (Photos.app libraries, Finder Trash), **configuration and defaults**. Include each even when the answer is "no interaction."
7. **Edge cases.** Anything a user could notice that is not covered above.
8. **Open questions and verification.** The source repo commit the document was verified against, and any behavior that could not be confirmed.

Item 5 matters most. Asking the same interrupt questions of every feature is how gaps and inconsistencies are found.

### Method

For each document:

1. Read where the behavior lives: `src/dedupe/engine.py` orchestrates scans, `src/dedupe/web/app.py` and `static/app.js` own the UI state, `src/dedupe/review_session.py` owns persistence, `src/dedupe/actions.py` owns trash/quarantine/isolate and receipts, `src/dedupe/cli.py` defines every command and flag.
2. Read the matching tests in `tests/`. `test_engine.py`, `test_actions.py`, `test_review_session.py`, `test_grouping.py`, `test_web.py`, and `test_browser_workflow.py` read as executable specifications of the edge cases.
3. Draft the document.
4. Try anything ambiguous on the running product: `.venv/bin/dedupe ui` → `http://127.0.0.1:8765` in the source repo, or `.venv/bin/dedupe {subcommand}`. Tests settle "what happens"; the running product settles how it feels, what is visible while the interaction is in progress, and what the timing is like.
5. Record the commit verified against.

### Verification

Drafting reads the code; verification watches the product. The `verification/` directory holds one checklist per cluster of documents, each item a single observable claim with setup, steps, expected result, a priority, and the device it needs. A tester runs them against the app at commit `2a6cede`, records `pass`, `fail`, or `blocked` in the Result column, and files every failure in `bug-triage.md` with the item's ID. A document moves from `drafted` to `verified` in the coverage table only when every P1 and P2 item for it has passed or been filed.

`bug-triage.md` is the other half: every behavior the documents flagged as a likely defect, deduplicated, with reproduction steps, the reason in the code, a severity, and the decision the product team needs to make. Entries confirmed in the running product carry a Status line.

### Order of work

1. **Pilot: `cli/doctor.md`.** Small, self-contained, real output, one flag (`--json`). Used to settle the template, tone, and depth.
2. **Foundations: `foundations/`.** The scan pipeline (owns every detection threshold), the duplicate group (owns keeper and selection), the review session (owns persistence and revalidation), actions and undo (owns the safety model). Everything else refers to them.
3. **The group review UI: `ui/`.** The bulk of the experience. Written third so the template is already proven; its documents must agree on where one screen hands off to the next.
4. **Everything else.** The remaining CLI commands and the cross-cutting documents. Once the exemplars exist they can be drafted in parallel, followed by a consistency pass and a verification pass across the whole set.

Progress is tracked in the [coverage table](#coverage) below.

### Scope decisions

- **Two surfaces, one repo.** Dedupe's web UI and CLI share one engine and one set of on-disk state; describing them separately would split every safety fact in two. CLI documents use the invocation dialect of the template, UI documents the task dialect; the interrupt list and cross-cutting order are identical across both.
- **Benchmarks out of scope.** `benchmark-humans` and `benchmark-similarity` are evaluation harnesses for tuning detectors, not part of the deduplication experience. They get a short mention in `cli/scan.md`'s open questions at most.
- **Installation out of scope.** `install.sh`, updates, and the optional macOS `.app` bundle are described nowhere; the described surface is an installed Dedupe at defaults. `launchers/Dedupe.command` double-click startup is covered briefly in `cli/ui-command.md` because users meet it.
- **Photon model internals out of scope.** Only what the user sees of the opt-in Photon backend (a download, engine choices, frame counts) is described; model weights and inference mechanics are not.
- **Interaction shape.** The units of interaction are the CLI *invocation* (phases: invoke / exit immediately / begin running / while running / finish) and the UI *task* (phases: start / end without changing anything / become extended / while extended / complete). The interrupt list and the order of cross-cutting concerns are fixed as written in the document template above and do not change without revisiting every document.
- **Numbered rules.** These are prose documents, not numbered specifications. Stable heading anchors are enough for cross-references.
- **Re-pin.** The set was drafted against commit `e8969e4` and re-pinned to `2a6cede` after the triaged fixes ([bug-triage.md](bug-triage.md) B-01 to B-04) landed; documents describe the post-fix behavior throughout.

## Structure

```
README.md                        this file
goal.md                          the standing instructions for whoever drafts
AGENTS.md, CLAUDE.md             entry points for agents: read README.md, then goal.md
glossary.md                      shared vocabulary
bug-triage.md                    suspected defects collected from every document, with repro steps and decisions needed

verification/
  README.md                      how to run a hand-verification pass and record results
  foundations.md                 checklists for foundations/
  ui.md                          checklists for ui/
  cli.md                         checklists for cli/ and cross-cutting/

foundations/
  scan-pipeline.md               what a scan does stage by stage; owns every detection threshold
  duplicate-group.md             what a group is, the keeper, the selection, ranking and smart select
  review-session.md              how a review survives restarts; pruning, revalidation, resuming
  actions-and-undo.md            trash, quarantine, isolate, receipts, and undo

ui/
  scan-setup.md                  choosing folders, exclusions, starting a scan, watching progress
  group-list.md                  the review list: cards, keyboard navigation, advanced filters, bulk selection
  lightbox.md                    the full-screen overlay for comparing similar media
  action-sheet.md                the preview-and-confirm sheet, its countdown, and re-verification
  low-res-review.md              the item-at-a-time low-resolution review
  random-review.md               the Random 50 sample review
  no-person-review.md            the no-person-detected review and its engine choices
  faces-review.md                the faces review category and the face filters
  session-resume.md              the resumed-session banner, dropped files, discarding a saved review

cli/
  doctor.md                      dependency and path health check (the pilot)
  scan.md                        the scan command: flags, progress, JSON output, bare-path shortcut
  ui-command.md                  starting the server, opening the browser, the .command launcher
  isolate.md                     isolating groups from a JSON scan into review folders
  undo.md                        restoring a quarantine from its receipt
  receipts.md                    listing, inspecting, and pruning action receipts

cross-cutting/
  caches-and-files.md            every file Dedupe writes, where, and when it is safe to delete
  optional-dependencies.md       how the experience degrades without ffmpeg, OpenCV, or Photon
```

## Coverage

Status is one of `not started`, `drafted`, or `verified`.

| Document | Status |
| --- | --- |
| glossary.md | drafted |
| bug-triage.md | drafted |
| verification/ (3 checklists) | drafted |
| foundations/scan-pipeline.md | drafted |
| foundations/duplicate-group.md | drafted |
| foundations/review-session.md | drafted |
| foundations/actions-and-undo.md | drafted |
| ui/scan-setup.md | drafted |
| ui/group-list.md | drafted |
| ui/lightbox.md | drafted |
| ui/action-sheet.md | drafted |
| ui/low-res-review.md | drafted |
| ui/random-review.md | drafted |
| ui/no-person-review.md | drafted |
| ui/faces-review.md | drafted |
| ui/session-resume.md | drafted |
| cli/doctor.md | drafted |
| cli/scan.md | drafted |
| cli/ui-command.md | drafted |
| cli/isolate.md | drafted |
| cli/undo.md | drafted |
| cli/receipts.md | drafted |
| cross-cutting/caches-and-files.md | drafted |
| cross-cutting/optional-dependencies.md | drafted |

## Reference

The source of truth is the Dedupe repository at `/Users/sethsaler/Documents/GitHub/dedupe`, pinned at commit `2a6cede`. The relevant locations are:

- `src/dedupe/cli.py`: every command, flag, and default; the entry point for the CLI surface.
- `src/dedupe/web/app.py`, `src/dedupe/web/static/app.js`, `templates/index.html`: the UI surface and its state.
- `src/dedupe/engine.py`: scan orchestration — the stage order and what each stage produces.
- `src/dedupe/exact.py`, `similar_image.py`, `similar_video.py`, `human_detection.py`, `face_detection.py`: the detectors and their thresholds.
- `src/dedupe/grouping.py`, `keep_decisions.py`: ranking, smart select, and remembered Keep decisions.
- `src/dedupe/review_session.py`, `src/dedupe/actions.py`, `src/dedupe/receipts.py`: persistence, actions, receipts.
- `src/dedupe/cache.py`, `src/dedupe/parallel.py`: the hash cache and thread-pool limits.
- `src/dedupe/models.py`: the data model shared across stages.
- `tests/`: behavioral tests; `test_engine.py`, `test_actions.py`, `test_review_session.py`, `test_grouping.py`, `test_web.py`, `test_browser_workflow.py` are closest to executable specifications.
