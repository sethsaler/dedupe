// Group predicates and lookups over the shared state.

import { state } from "./state.js";

function isDecisionReview(g) {
  return g?.kind === "low_resolution" || g?.kind === "random_review";
}

function isIndependentReview(g) {
  return g?.policy === "independent_candidates";
}

function isPagedIndependentReview(g) {
  return g?.kind === "no_humans" || g?.kind === "faces";
}

function currentGroup() {
  return state.allGroups.find((group) => group.id === state.currentId)
    || state.groups.find((group) => group.id === state.currentId);
}

function patchGroup(updated) {
  const idx = state.groups.findIndex((candidate) => candidate.id === updated.id);
  if (idx >= 0) state.groups[idx] = updated;
  const allIdx = state.allGroups.findIndex((candidate) => candidate.id === updated.id);
  if (allIdx >= 0) state.allGroups[allIdx] = updated;
}

function groupSelectedCount(g) {
  return (g.selected_for_removal || []).length;
}

function groupComplete(g) {
  if (isIndependentReview(g)) return (g.reviewed_paths || []).length >= (g.member_count || 0);
  return (g.selected_for_removal || []).length >= Math.max(0, (g.member_count || 0) - 1);
}

function groupNeedsAttention(g) {
  return (g.members || []).some((member) => member.error) || (g.deleted_paths || []).length > 0 || !groupComplete(g);
}

// Groups the user has actively re-selected in; until then their selection is
// the smart-select suggestion and the UI labels it as such.
function markGroupTouched(id) {
  if (id) state.touchedGroups.add(id);
}

export { isDecisionReview, isIndependentReview, isPagedIndependentReview, currentGroup, patchGroup, groupSelectedCount, groupComplete, groupNeedsAttention, markGroupTouched };
