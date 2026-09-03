# Goal: complete the Dedupe product description

You are working in `docs/product-description/` inside the Dedupe repository. Read `README.md`, `glossary.md`, `foundations/scan-pipeline.md`, and `cli/doctor.md` first. The README defines the purpose, the document template, the method, the structure, and the coverage table. The other three are the exemplars: match their depth, tone, and structure exactly. Your job is to write every document in the README's structure until the coverage table has no `not started` rows, then run a consistency pass.

## Source of truth

The Dedupe repository is the root of this checkout (the source of truth for these documents), pinned at commit `2a6cede`. Describe the experience of an installed Dedupe at defaults: the web UI via `dedupe ui` → `http://127.0.0.1:8765`, and the CLI via `.venv/bin/dedupe {subcommand}`, with ffmpeg and OpenCV present as `dedupe doctor` reports. The benchmarks (`benchmark-humans`, `benchmark-similarity`), the installer, and the macOS app bundle are out of scope.

For each document, read in this order before writing:

1. Where the behavior lives: `src/dedupe/engine.py` (scan orchestration), `src/dedupe/web/app.py` + `static/app.js` + `templates/index.html` (UI state), `src/dedupe/review_session.py` (persistence), `src/dedupe/actions.py` and `receipts.py` (trash/quarantine/isolate, receipts), `src/dedupe/cli.py` (every command and flag).
2. The detectors where relevant: `exact.py`, `similar_image.py`, `similar_video.py`, `human_detection.py`, `face_detection.py`, `grouping.py`, `keep_decisions.py`.
3. The tests in `tests/`. They are close to executable specifications of edge cases. Key files: `test_engine.py`, `test_actions.py`, `test_review_session.py`, `test_grouping.py`, `test_web.py`, `test_browser_workflow.py`, `test_cli.py`, `test_isolate.py`, `test_keep_decisions.py`.
4. Defaults and thresholds: constants at the top of `similar_image.py` (`DEFAULT_THRESHOLD = 6`, `DEFAULT_TILE_MAX = 8`), `similar_video.py` (`DEFAULT_THRESHOLD = 8`), `grouping.py` (`LOW_RESOLUTION_MAX_PIXELS = 1_000_000`, `DEFAULT_RANDOM_REVIEW_COUNT = 50`), `parallel.py` (worker caps), and `cli.py` defaults.
5. Try anything ambiguous live: `.venv/bin/dedupe ui` in the source repo, or the CLI subcommand under a scratch directory in `/tmp`. Never point a scan at a real photo library during drafting.

Do not describe code. Describe what the user sees and does. Technical detail goes only in `> Technical note:` block quotes, and only when the mechanism changes what the user would expect.

## Writing rules

- Follow the eight-section template in the README. CLI documents use the invocation dialect (invoke / exit immediately / begin running / while running / finish); UI documents use the task dialect (start / end without changing anything / become extended / while extended / complete). Foundations and cross-cutting documents may drop sections that do not apply but must still cover interrupt behavior wherever an interaction exists.
- Modifiers and cancel/interrupt go in tables, split by phase, as in `cli/doctor.md` (CLI rows) and `ui/action-sheet.md` (UI rows). The interrupt rows and the order of cross-cutting concerns are fixed in the README; do not add, drop, or reorder them in a single document.
- Use the glossary's words. If you need a term the glossary lacks, add it to `glossary.md` in the right section with a one-paragraph definition, then use it.
- Sentence case for all headings. Direct, concrete language. No hedging, no marketing.
- State surprising behavior plainly and say why if the reason is in the code or a comment. If it looks like a bug, say so in "Open questions" rather than smoothing it over.
- Cross-reference with relative links rather than repeating content. `foundations/scan-pipeline.md` owns thresholds; `foundations/duplicate-group.md` owns keeper and selection semantics; `foundations/review-session.md` owns persistence and revalidation; `foundations/actions-and-undo.md` owns the safety model. Do not restate them; link.
- Every document ends with "## Open questions and verification" listing what was read from code but not confirmed by hand, followed by `Verified against dedupe commit \`2a6cede\``.
- Mermaid `stateDiagram-v2` for each interaction's states. Keep it to the states the user passes through; omit internal bookkeeping states.

## Things already established (do not re-derive, do not contradict)

From `foundations/scan-pipeline.md`:

- Detection thresholds: image global pHash ≤ 6, dHash pairing ≤ 10, tile worst ≤ 8, tile mean ≤ 5.0; video mean Hamming ≤ 8; images downscaled to ≤ 512 px for hashing; videos sample ≤ 16 timeline positions.
- Low-resolution bound: 1,000,000 pixels by default, configurable per media type. Random review: up to 50 files.
- Exact funnel: size bucket → first-64 KB partial hash → full SHA-256; zero-byte files excluded; ≥ 2 files required per kind.
- Stage order: inventory → cache hydrate → exact/image/video concurrently (exact groups always published before similar) → low-res dimension probes → low-res + random groups → person detection (opt-in) → face detection (opt-in) → cache save.
- Workers: auto = min(cpu−1, 8) (all cores on ≤ 2-core); stage caps 4 exact, 6 image, 4 video, 4 OpenCV; Photon serial.
- One-pool scan dedupes across folders; parallel-stream scan (per-folder) never does, and every stream's groups carry their source root.
- Photos.app library roots are refused with an export message. Missing roots error and are skipped.
- Corrupt files never abort a scan; failures go to per-stage diagnostics.
- Backends: opencv (default; YuNet presence 0.35 + flip/tiles + INRIA/Daimler HOG), photon (≈10 GB opt-in download; woman/girl/person/face), ensemble. A counted face (especially female) vetoes Non-Human. YuNet missing/corrupt → Non-Human fails closed (no candidates surfaced).

From `foundations/duplicate-group.md`:
- Kinds: exact, similar (keep-one policy); low_resolution, random_review, no_humans, faces, all_files (independent-candidate policy). All-files groups are built from the scan inventory when results load — one per scanned folder, path-ordered — and carry no selection semantics (bulk operations and selection rules never touch them).
- Keeper ranking: pixels → size → mtime → shallower path → shorter name.
- New exact/similar groups arrive pre-selected: everything except the suggested keeper (automatic rule).
- Selection rules: automatic, newest, oldest, largest, smallest, shortest path, deselect all, select candidates (independent only: selects reviewed).
- Effective-selection vetoes: Keep decisions > unreviewed independent candidates > last-survivor keeper restoration. Reclaimable bytes always reflect the effective selection.
- Similar groups wholly contained in an exact group are dropped; mixed sets survive.
- Group list sorted most-reclaimable-first, including during streaming.

From `foundations/review-session.md`:
- Session file: `~/.local/state/dedupe/review-session.json` (XDG_STATE_HOME respected), 0600/0700, atomic write, 64 MB load cap, version 1.
- Prune reasons (exact banner labels): no longer on disk, changed since the scan, outside the scanned folders, became a symbolic link, could not be read. Up to 20 example files reported.
- Survival rules after pruning: keep-one groups need ≥ 2 members, independent ≥ 1; pruned keeper → first remaining member becomes suggestion.
- Corrupt/oversize session: reported, not loaded; app starts clean.

From `foundations/actions-and-undo.md`:
- Actions: Trash (system trash; restores: per-candidate Non-Human/Faces/Files undo, and whole-action undo from the receipt via the result toast's Undo or `dedupe undo`), Quarantine (move, unique names, undoable via receipt), Isolate (copy by default; hardlink/symlink/move modes; `KEEP__` prefix; session folders under `_Dedupe Review`).
- Safety layers in order: effective selection → batch preflight (lstat identity: symlink refused, regular file, in roots, size/device/inode/mtime match; exact groups re-hashed against keeper) → keeper validation with re-hash tolerance for metadata drift → immediate per-file revalidation → receipt.
- Undo is all-or-nothing on preflight; restores in reverse order; always cross-volume; writes its own receipt. Dry-run receipts cannot be undone.
- File actions hold a lock: scans and actions never overlap; concurrent requests get a locked refusal.
- Receipts live in `~/.cache/dedupe/logs/`.
- Server death mid-action is the one genuinely unsafe window.

Naming decisions:

- The code's `ReviewGroup` is written "duplicate group" / "review category" per the glossary; `DuplicateGroup` is a legacy alias.
- `NO_HUMANS` kind is written "Non-Human" (the UI's word); `RANDOM_REVIEW` as "Random 50"; `ALL_FILES` as "Files" (the tab label) or "the all-files review".
- CLI invocation dialect vs UI task dialect: both use the same interrupt rows and cross-cutting order from the README.

From `ui/scan-setup.md`, `ui/group-list.md`, `ui/action-sheet.md`:

- Server model: one global lock; `scanning` and `acting` flags serialize everything (selections/actions refused during scans; scans/resume refused during actions).
- Every mutating request needs the `X-Dedupe-Token` CSRF header and a matching `scan_id`; stale scan id → "stale scan session; refresh results".
- Preview token: one-use, TTL 600 s (10 minutes), bound to (scan_id, action, scope, destination, sorted eligible paths). Stale verdicts: missing / expired / changed; each triggers an automatic re-preview, never a stale execute.
- UI Trash splits: low-resolution + random-review selections are quarantined into `_Dedupe Quarantine` beside the scan root; everything else goes to system Trash. Two receipts. Per-candidate trash of Non-Human/Faces/Files items goes to system Trash with a server-side restore (`deleted_files` map).
- Needs attention = member error OR session-deleted member OR not complete. Complete = ≥ member_count−1 selected (keep-one) or all members reviewed (independent).
- Member cards paginate at 50 per page. Group list sorted most-reclaimable-first.
- Keyboard: j/k/↓/↑ navigate shown groups; [/] attention groups (wrap, toast if none); u suggested selection; s rule chooser; a/A/D preview Trash (exact / similar / Low-res + Random review); r reveal focused card in Finder (in the lightbox: current file); Space toggles focused card; Enter lightbox; ←/→ Delete/Keep in low-res + random decision review, else card focus / lightbox nav; Esc closes lightbox/help; ? help. All inactive while typing in inputs.
- Tab close → POST /api/shutdown, 1.5 s grace; any request cancels it (reload survives). Server prints the URL and "Press CTRL+C to quit".
- Scan settings + recent folders persist in browser local storage. Quarantine dir remembered.
- After executed trash/quarantine: moved members dropped from groups; groups under minimum size dissolve; keeper re-picked if removed; result persisted.
- *Mark as distinct* (similar groups): pairwise distinct stored in hash cache, group removed now and from future scans until a file changes. *Mark all remaining as human* (Non-Human): manual-confirmed status stored in cache, members removed from the category; manual decisions outrank detector signatures but file identity still invalidates them.
- Thumbnails: only scanned files served; HEIC/TIFF transcoded; immutable cache headers. `/api/reveal` opens Finder (`open -R`) for allowed paths.
- Decision reviews (low-res, random): ← Delete / → Keep applies to the focused member; Keep clears duplicate picks across overlapping independent branches; newest arrow decision wins; Keep decisions for low-res are synced to the keep-decisions file on every decision.
- Bulk selection applies to *shown* groups only, re-derived server-side; bulk-selected independent candidates also become reviewed; min-faces criteria never match unanalyzed files.
- Scan cancel is cooperative ("Cancelling after current work item…"); a failed/cancelled scan restores the previous result and voids preview tokens; multiple folders default to parallel streams.

## Order of work

1. `foundations/` first, in this order: `scan-pipeline.md`, `duplicate-group.md`, `review-session.md`, `actions-and-undo.md`. Everything else links to them.
2. `ui/` next, all ten documents. This is the hardest part and the bulk of the experience. Read `src/dedupe/web/app.py` and `static/app.js` end to end before starting any of them, because the screens hand off to each other and the documents must agree on where one ends and the next begins. `scan-setup.md` owns the scan flow up to results landing; `group-list.md` owns browsing, filtering, and selection; `action-sheet.md` owns preview/confirm/execute; the five review documents own their categories' review interactions; `session-resume.md` owns the resumed-session banner.
3. The remaining `cli/` documents and `cross-cutting/`. These are independent of each other and can be drafted in parallel with subagents once the foundations and `ui/` documents exist to link to. Review every subagent result for consistency with the glossary and the established facts above before accepting it.
4. Consistency pass over the whole set: same term for the same thing everywhere, no two documents describing the same behavior differently, every relative link resolves (`python3 check-links.py .` from wherever the checker lives), every document has a verification footer, every glossary term used is defined.
5. Update the coverage table in `README.md` as you go: `drafted` when written, never `verified` (verification by hand is a separate pass).

## Working rules

- Commit after each document or coherent group of documents with a message of the form `docs: add {path}` or `docs: revise {path}`. No AI attribution lines in commit messages.
- Do not modify anything in `/Users/sethsaler/Documents/GitHub/dedupe`. It is read-only reference material.
- Do not add files outside the README's structure without updating the structure and coverage table to match.
- When a behavior cannot be determined from code and tests, write down what you could determine, put the rest in "Open questions", and move on. Do not guess and do not block.
- Depth bar: `cli/doctor.md` is roughly 150–200 lines for a small command. The `ui/` documents will be longer. Completeness matters more than length. Every state, every modifier, every cancel/interrupt row must be accounted for, even if the answer is "No effect."
- If you find that the README's structure is wrong for something you discover (a document that should be split, two that should merge), make the change, update the structure and coverage table, and note why in the commit message.

You are done when the coverage table has no `not started` rows, the consistency pass is complete, and everything is committed.
