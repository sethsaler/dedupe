# The duplicate group

## Summary

The duplicate group is the object the whole product revolves around: a set of media files the scan believes belong together, plus a selection of which of them to remove. Exact and similar groups are true duplicates where one file should survive; the review categories (low-resolution, random, non-human, faces) are lists of independent candidates judged one by one. This document owns what a group is, how the *suggested keeper* is chosen, what the *selection* means, and the rules that keep at least one file alive in every duplicate group. Where groups appear and how the user moves among them is [Group list](../ui/group-list.md); what happens to a selection is [Actions and undo](actions-and-undo.md).

## Anatomy of a group

Every group has:

- **A kind:** `exact`, `similar`, `low_resolution`, `random_review`, `no_humans`, or `faces`.
- **Members:** the media files, always shown sorted by path.
- **A dominant media type:** image, GIF, video, or mixed — the type most of its members share.
- **A selection:** the members currently marked for removal.
- **A review policy**, which the kind determines:
  - *Keep one* — exact and similar groups. One file survives; the rest are candidates for removal.
  - *Independent candidates* — low-resolution, random, non-human, and faces groups. Each file is judged on its own; any number, including all or none, may be selected.
- **A suggested keeper** (keep-one groups only): the member Dedupe recommends surviving.
- **Reviewed paths** (independent groups): which candidates the user has explicitly looked at.

The group's id is stable for its membership: the same kind with the same members always produces the same id, so selections and [review sessions](review-session.md) can refer to groups across restarts.

## The suggested keeper

In a keep-one group the suggested keeper is the member ranked best by, in order: more pixels, larger file size, newer modification time, shallower path, shorter filename. The UI highlights it in green. The intent is that the highest-quality copy in the most canonical location survives.

A rule can change which member is the keeper — see [Selection rules](#selection-rules) — but there is always exactly one suggested keeper in a keep-one group, and the UI and every bulk operation treat it as protected.

> Technical note: similar groups are built around their best-ranked member rather than by chaining fuzzy matches. Every automatically selected member was compared directly with the file the UI recommends keeping, so removing the selection never deletes a file that only resembled a file that resembled the keeper.

## The initial selection

New exact and similar groups arrive **pre-selected**: every member except the suggested keeper is already marked for removal (the *automatic* rule). The user's work is to review that suggestion — deselecting anything worth keeping — not to build a selection from scratch. Independent groups arrive with nothing selected and nothing reviewed.

## Selection rules

The `u` key (one group) and the bulk-selection rules (all shown groups) apply one of these rules:

| Rule | Effect on a keep-one group | Effect on an independent group |
| --- | --- | --- |
| Automatic | Keep the suggested keeper; select all others. | No effect except clearing the suggested keeper. |
| Newest | Keep the most recently modified member; select the rest. | No effect. |
| Oldest | Keep the oldest member; select the rest. | No effect. |
| Largest | Keep the member with the most pixels (then bytes); select the rest. | No effect. |
| Smallest | Keep the member with the fewest pixels (then bytes); select the rest. | No effect. |
| Shortest path | Keep the member with the shallowest, shortest path; select the rest. | No effect. |
| Deselect all | Clear the selection; nothing will be removed. | Clear the selection. |
| Select candidates | No effect. | Select every candidate that has been reviewed. |

Every rule that keeps one member keeps exactly one and selects all the others. Bulk operations in the UI (select all, select none, invert, and the size/path rules) are re-derived on the server with the same guarantee: **a duplicate group never ends up with its keeper selected, so at least one member always survives** — no matter what the browser asked for.

## What actually acts: the effective selection

When an action is confirmed, the selection is reduced to the *effective* selection by three veto rules, in this order:

1. **Keep decisions veto.** A file that is reviewed-and-unselected in any independent group has a durable [Keep decision](../glossary.md); it is removed from every selection, including automatic duplicate picks from an exact or similar group it also belongs to.
2. **Independent candidates must be reviewed.** In an independent group, a selected member only counts if it is also in the group's reviewed paths. Selecting without reviewing does nothing at action time.
3. **Last survivor.** If every member of a keep-one group ended up selected, the suggested keeper is put back.

The reclaimable-bytes figures everywhere in the UI are computed from the effective selection, so what the user sees is what an action would actually move.

**Reclaimable space per group.** For a keep-one group it is the total member size minus the suggested keeper's size. For an independent group it is the combined size of members that are both selected and reviewed (and, for non-human groups, whose no-person verdict is still current).

## How groups are formed

- **Exact groups:** files with identical SHA-256, discovered through the size → partial hash → full hash funnel (see [Scan pipeline](scan-pipeline.md#how-each-detection-works)).
- **Similar groups:** clustered around their best-ranked member from verified pairwise matches; a candidate set entirely contained in an exact group is dropped (it would say nothing new), but a set that mixes exact copies with a re-encoded copy survives as a similar group.
- **Independent groups:** one collection per review category per scan — all low-resolution candidates in one group (smallest first), all non-human candidates in one (newest first), all faces candidates in one (highest face count first, then newest), and the random sample in one.

The whole group list is kept sorted with the most reclaimable space first, both in the final result and while groups stream in during a scan.

## Interactions with other systems

**Files on disk.** Groups exist only in memory and in the [review session](review-session.md) file once selections are saved; the members themselves are untouched until an [action](actions-and-undo.md) runs.

**Safety and undo.** The keeper-protection rules above are the first layer of the safety model; the second is [revalidation](../glossary.md) at action time, owned by [Actions and undo](actions-and-undo.md).

**Review sessions.** A saved session stores selections and reviewed paths against group ids; loading a session re-applies them to a fresh scan's groups where ids match.

**Optional dependencies.** Group formation for videos needs ffmpeg; non-human and faces groups need OpenCV. See [Optional dependencies](../cross-cutting/optional-dependencies.md).

**Concurrency and resource limits.** None visible at the group level; grouping is fast compared to hashing.

**macOS specifics.** None beyond path handling.

**Configuration and defaults.** Detection thresholds shape which groups exist ([Scan pipeline](scan-pipeline.md)); the keeper ranking and selection rules are fixed and not configurable.

## Edge cases

- A group with fewer than two members is never created; singletons are dropped at construction.
- The same file can appear in an exact group, a similar group, and an independent category at once; the veto rules resolve the conflict at action time, never at selection time.
- If a keep rule ties (two members with identical mtime, for example), the choice is deterministic but arbitrary — the first member the comparison favors wins.
- Applying a selection rule to an independent group clears any stale suggested keeper instead of selecting anything.
- Reclaimable bytes for a non-human candidate drop to zero if its detection verdict becomes stale, even while it stays selected and reviewed.

## Open questions and verification

- The pre-selected arrival of exact/similar groups (everything but the keeper selected) is the default in code; whether a first-time user experiences this as a suggestion or as a decision already made for them is a product question.
- Keeper-protection during bulk selection is enforced server-side; the browser's own checkboxes can appear to include the keeper until the server's re-derivation lands. The visible reconciliation is to be confirmed in [Group list](../ui/group-list.md).

Verified against dedupe commit `2a6cede`.
