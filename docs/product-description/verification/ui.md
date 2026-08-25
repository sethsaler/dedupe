# Verification: ui

How to run this file: bring up `.venv/bin/dedupe ui` from the source repo and scan the scratch fixture (see [README](README.md#devices-and-conditions)). Reset between sections with Discard saved review unless the section is about resume. Device column: `keyboard`, `mouse`, `tabs` (two tabs, one browser), `disk`.

## ui/scan-setup.md

| ID | P | Device | Claim | Setup | Steps | Expected | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SCANSET-01 | P1 | mouse | Native folder picker appends the chosen path ([Start](../ui/scan-setup.md#start)). | Empty path field. | 1. Click **Choose…**.<br>2. Pick the scratch folder. | The field is filled with the folder's path; scan starts on **Scan**. | — |
| SCANSET-02 | P1 | keyboard | Empty path list is rejected ([Start](../ui/scan-setup.md#start)). | No results loaded; empty field. | 1. Press **Scan**. | No scan starts; an error ("paths required" surfaced as toast/error text). | — |
| SCANSET-03 | P1 | mouse | Progress phases appear in order with live counts and ETA ([While extended](../ui/scan-setup.md#while-extended)). | The scratch fixture. | 1. Start a scan.<br>2. Watch the progress panel through the run. | Phases: walking folders → cache hits → merged processing line with per-stage text → done summary; counts update live. | — |
| SCANSET-04 | P1 | mouse | Cancel stops the scan and restores previous results ([Cancel and interrupt](../ui/scan-setup.md#cancel-and-interrupt)). | A completed scan loaded; start a second scan over a bigger folder. | 1. Start the second scan.<br>2. Press **Cancel** mid-run. | Message becomes "Cancelling after current work item…"; when halted, the previous results are shown again. | — |
| SCANSET-05 | P2 | mouse | Selections are locked while a scan runs ([While extended](../ui/scan-setup.md#while-extended)). | A scan in progress with streamed groups visible. | 1. Try toggling a checkbox or applying a bulk rule. | The change is refused ("locked during active work" toast); state unchanged. | — |
| SCANSET-06 | P2 | mouse | Scan options and recent folders persist across visits ([Modifiers](../ui/scan-setup.md#modifiers)). | Change a threshold and add a recent folder. | 1. Change the image threshold to 7; scan or navigate away.<br>2. Reload the page. | Threshold shows 7; the folder appears as a recent chip (browser local storage). | — |
| SCANSET-07 | P2 | mouse | Exclusion globs remove matching files ([Start](../ui/scan-setup.md#start)). | Scratch folder with a `thumbs` subfolder. | 1. Add exclusion `thumbs`.<br>2. Scan. | Files under `thumbs` are absent from results ("Files scanned" reflects it). | — |
| SCANSET-08 | P3 | tabs | Reload during a scan re-attaches to the running scan ([Cancel and interrupt](../ui/scan-setup.md#cancel-and-interrupt)). | A scan in progress. | 1. Reload the browser tab quickly.<br>2. Watch the page. | The page shows the running scan and its progress; the server did not shut down (the reload cancelled the 1.5 s grace). | — |

## ui/group-list.md

| ID | P | Device | Claim | Setup | Steps | Expected | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GROUPS-01 | P1 | keyboard | `j`/`k` navigate shown groups; `[`/`]` jump needs-attention groups ([While extended](../ui/group-list.md#while-extended)). | Results with several groups, some incomplete. | 1. Press `j`, `k` repeatedly.<br>2. Press `]`, `[`. | Focus moves through the shown list; attention keys visit only ● groups and wrap; a toast when none remain. | — |
| GROUPS-02 | P1 | keyboard | `Space` toggles the focused member's checkbox; `u` applies the suggested selection ([While extended](../ui/group-list.md#while-extended)). | A duplicate group open. | 1. Focus a member card with `←`/`→`, press `Space`.<br>2. Press `u`. | Checkbox toggles and persists; `u` restores the automatic selection (keeper kept). | — |
| GROUPS-03 | P1 | mouse | Bulk operations never select a keeper and keep one survivor per duplicate group ([While extended](../ui/group-list.md#while-extended)). | Several duplicate groups shown. | 1. Apply bulk **select all**.<br>2. Inspect each group's checkboxes and the selection summary. | Every group: suggested keeper unchecked, all others checked; the summary counts match the effective selection. | — |
| GROUPS-04 | P1 | mouse | Advanced filters keep a group when any member matches ([Start](../ui/group-list.md#start)). | A group with one file > 5 MB and one < 5 MB. | 1. Set Advanced filter min size 5 MB.<br>2. Inspect the list and the group's members. | The group stays visible; inside it, both members still show (filter narrows the list, not members). | — |
| GROUPS-05 | P2 | keyboard | Needs attention = member error OR deleted member OR not complete ([Edge cases](../ui/group-list.md#edge-cases)). | One complete group, one incomplete, one with a deleted candidate. | 1. Check glyphs on each card.<br>2. Compare with the definition. | ● on the incomplete and deleted-member groups; ✔ on the complete one. | — |
| GROUPS-06 | P2 | mouse | Mark as distinct removes a similar group now and in future scans ([While extended](../ui/group-list.md#while-extended)). | A similar group open. | 1. Click **Mark as distinct**, confirm.<br>2. Rescan the same folder. | Group gone immediately; absent from the fresh scan until a member changes. | — |
| GROUPS-07 | P2 | mouse | Hidden groups keep selections; bulk applies to shown only ([Edge cases](../ui/group-list.md#edge-cases)). | Select in a group, then filter it out. | 1. Make a selection in group A.<br>2. Filter so A is hidden.<br>3. Bulk select-all on the shown groups; clear the filter. | Group A's selection unchanged; shown groups affected. | — |
| GROUPS-08 | P3 | mouse | Member cards paginate at 50 per page ([While extended](../ui/group-list.md#start)). | An independent group with > 50 members (or a large random/low-res scan). | 1. Open the group.<br>2. Use the page control. | 50 cards per page with a count summary and page control. | — |

## ui/lightbox.md

| ID | P | Device | Claim | Setup | Steps | Expected | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| LIGHTBOX-01 | P1 | keyboard | Enter opens the lightbox on the focused member; Esc closes without side effects ([Summary](../ui/lightbox.md)). | A group open. | 1. Press `Enter`.<br>2. Press `Esc`. | Overlay shows the member large; closing returns to the identical list state (selections unchanged). | — |
| LIGHTBOX-02 | P2 | keyboard | Arrows step through the group's members inside the lightbox ([The interaction](../ui/lightbox.md#the-interaction-event-by-event)). | Lightbox open on a 3-member group. | 1. Press `→` twice, `←` once. | Members advance/retreat in the group's order; outside-lightbox navigation keys do not fire. | — |

## ui/action-sheet.md

| ID | P | Device | Claim | Setup | Steps | Expected | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SHEET-01 | P1 | mouse | The preview counts down from 10:00 and shows per-category counts ([Start](../ui/action-sheet.md#start)). | A selection across categories. | 1. Press **Trash** (or `a`).<br>2. Read the sheet. | Numbers per category + total bytes; "Verified against the current selection · preview valid for 10:00" counting down. | — |
| SHEET-02 | P1 | mouse | Selection changes after previewing force a re-preview on Confirm ([While extended](../ui/action-sheet.md#while-extended)). | A sheet open with a valid token. | 1. Close nothing; elsewhere deselect one file (second tab or by closing the sheet, changing, re-opening — as the flow allows).<br>2. Confirm with the stale token path. | The execute is refused with "selection changed since the preview…"; the sheet re-previews with refreshed numbers; nothing moved. | — |
| SHEET-03 | P1 | mouse | Executed Trash removes moved files from groups and dissolves empty groups ([Complete](../ui/action-sheet.md#complete)). | A 2-member exact group fully selected except keeper… select the remaining removable member. | 1. Confirm Trash.<br>2. Inspect the group list. | The moved file is gone; the group dissolves (1 member < 2); result toast/report lists the outcome. | — |
| SHEET-04 | P2 | mouse | Escape/Cancel discards the preview and token ([End without changing anything](../ui/action-sheet.md#end-without-changing-anything)). | A sheet open. | 1. Press Escape. | Sheet closes; nothing moves; reopening previews fresh. | — |
| SHEET-05 | P2 | mouse | Quarantine requires a directory and reports it in the sheet ([Start](../ui/action-sheet.md#start)). | A selection. | 1. Choose Quarantine with an empty directory field → confirm.<br>2. Set a directory → preview again. | First attempt refused; second shows the destination and executes on Confirm. | — |
| SHEET-06 | P2 | mouse | Isolate offers mode/scope/kind options and a reveal link ([Start](../ui/action-sheet.md#start)). | A selection with duplicate groups. | 1. Choose Isolate, keep default mode copy.<br>2. Confirm; use the reveal control. | `_Dedupe Review/session-…` created with KEEP__-prefixed keepers; Finder opens it. | — |

## ui/low-res-review.md

| ID | P | Device | Claim | Setup | Steps | Expected | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| LOWRES-01 | P1 | keyboard | `←` deletes and `→` keeps the focused candidate, advancing ([While extended](../ui/low-res-review.md#while-extended)). | Low-res tab with candidates. | 1. Press `←` then `→` on successive candidates.<br>2. Read the summary line. | First candidate selected-for-removal, second reviewed-unselected; "N of M reviewed · K selected" updates. | pass (e2e, 2026-08-24: also covered revisit-and-correct) |
| LOWRES-02 | P1 | disk | A Keep writes a durable keep decision; the file does not resurface ([What a Keep commits](../ui/low-res-review.md#while-extended)). | A kept low-res candidate. | 1. Keep one candidate.<br>2. Check `~/.local/state/dedupe/keep-decisions.json`.<br>3. Rescan. | The path appears in the file; the Low-res tab no longer lists it. | — |
| LOWRES-03 | P2 | keyboard | Selecting a kept candidate withdraws the decision ([What a Keep commits](../ui/low-res-review.md#while-extended)). | A kept candidate from LOWRES-02. | 1. Toggle it selected again.<br>2. Inspect the keep-decisions file. | The entry is cleared. | — |
| LOWRES-04 | P2 | keyboard | Keep withdraws the file from duplicate-group selections ([Overlapping groups](../ui/low-res-review.md#while-extended)). | A file in both Low-res and an exact group (two identical small images). | 1. Keep it in Low-res.<br>2. Open the exact group. | The file is no longer selected in the exact group even though the automatic rule had selected it. | — |

## ui/random-review.md

| ID | P | Device | Claim | Setup | Steps | Expected | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| RANDOM-01 | P1 | keyboard | Same `←`/`→` mechanics as low-res ([While extended](../ui/random-review.md#while-extended)). | Random 50 tab dealt. | 1. Decide two candidates with `←` and `→`. | Selections/reviewed update as in LOWRES-01. | — |
| RANDOM-02 | P1 | disk | Keeps are forgetful: no keep decision is written ([Keeps are deliberately forgetful](../ui/random-review.md#while-extended)). | A kept random candidate. | 1. Keep one candidate.<br>2. Inspect keep-decisions.json. | No entry for the kept file. | — |
| RANDOM-03 | P2 | mouse | A fresh scan deals a new sample ([Start](../ui/random-review.md#start)). | Two consecutive scans of a folder with > 50 files. | 1. Scan twice, noting the sample each time. | The two samples differ (with overwhelming probability for > 50 files). | — |

## ui/no-person-review.md

| ID | P | Device | Claim | Setup | Steps | Expected | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| NONHUMAN-01 | P1 | mouse | Nothing arrives selected; per-candidate Trash shows the heuristic warning ([While extended](../ui/no-person-review.md#while-extended)). | A scan with no-person enabled over people-free images. | 1. Open the Non-Human tab.<br>2. Press a candidate's delete button. | Nothing selected on arrival; the confirmation carries "Non-Human detection is heuristic and may miss people." | — |
| NONHUMAN-02 | P1 | mouse | Mark all remaining as human empties the category and persists ([While extended](../ui/no-person-review.md#while-extended)). | Candidates present. | 1. Click **Mark all remaining as human**, confirm.<br>2. Rescan. | Category empties ("N files marked as human" toast); a rescan without changes surfaces none of them. | — |
| NONHUMAN-03 | P1 | mouse | Per-candidate undo restores a trashed candidate ([While extended](../ui/no-person-review.md#while-extended)). | A candidate just trashed this session. | 1. Press its undo control. | File restored to its original path; the card returns. | — |
| NONHUMAN-04 | P1 | tabs | Per-candidate undo survives a server restart ([While extended](../ui/no-person-review.md#while-extended)). | Trash a candidate, restart the server. | 1. Trash a candidate.<br>2. Ctrl+C the server, relaunch, reload.<br>3. Inspect the candidate's card. | The undo control is still present and restores the file; the trash map rode in the session save. | — |
| NONHUMAN-05 | P2 | disk | A corrupt/missing YuNet model surfaces no candidates (fail-closed) ([Start](../ui/no-person-review.md#start)). | Move the YuNet model file aside (restore after). | 1. Rename the bundled YuNet model file.<br>2. Scan with no-person enabled.<br>3. Restore the file. | Non-Human category empty; `doctor` shows "not ready". | — |

## ui/faces-review.md

| ID | P | Device | Claim | Setup | Steps | Expected | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| FACES-01 | P1 | mouse | Candidates ordered by face count, busiest first; counts shown with male badges ([Start](../ui/faces-review.md#start)). | A scan with faces enabled over photos with known face counts. | 1. Open the Faces tab.<br>2. Inspect order and badges. | Highest face count first; badges "N faces", "N males"; tooltip mentions heuristic. | — |
| FACES-02 | P1 | mouse | The Faces filter positions match their exact semantics ([The Faces filter](../ui/faces-review.md#while-extended)). | Files with 0, 1, and 2+ faces; one unanalyzed file. | 1. Set each of the four filter positions.<br>2. Inspect which groups remain. | "1+ faces" ≥1; "1+ male faces" needs male count ≥1; "No faces (0)" only exact zeros (unanalyzed never match). | — |
| FACES-03 | P2 | mouse | Bulk min-faces rule never selects unanalyzed files ([Bulk selection by face count](../ui/faces-review.md#while-extended)). | Mixed analyzed/unanalyzed candidates. | 1. Apply bulk criteria min_faces 1. | Only files with a recorded count ≥ 1 selected. | — |
| FACES-04 | P2 | mouse | Delete confirmation carries the miscount warning ([Per-candidate Trash and undo](../ui/faces-review.md#while-extended)). | A faces candidate. | 1. Press its delete button. | Confirmation shows "Face counting is heuristic and may miscount." | — |

## ui/session-resume.md

| ID | P | Device | Claim | Setup | Steps | Expected | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| RESUME-01 | P1 | mouse | Auto-resume on startup installs the saved review with a banner ([Summary](../ui/session-resume.md)). | A saved session with prunable files (modify one file before restart). | 1. Restart the server.<br>2. Open the page. | Results loaded (no scan needed); banner reports pruned counts per reason and the "What was dropped?" list. | — |
| RESUME-02 | P1 | mouse | Discard saved review deletes the file and starts clean ([The simple case](../ui/session-resume.md#the-simple-case)). | A saved session present. | 1. Click **Discard saved review**. | Session file deleted; the page returns to empty scan setup. | — |
| RESUME-03 | P2 | mouse | Resume is locked during scans and actions ([Cancel and interrupt](../ui/session-resume.md#cancel-and-interrupt)). | A scan running. | 1. Attempt the resume/discard controls. | Refused with a locked message. | — |
