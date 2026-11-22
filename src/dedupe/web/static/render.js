// Batched requestAnimationFrame rendering.

import { updateSelectionSummary } from "./actions.js";
import { finishStreamedGroupRender, renderGroupList } from "./groups.js";
import { renderMembers } from "./members.js";

// —— Batched rendering ——
const pendingRender = { groupList: false, streamedGroups: false, selection: false, members: null };
let renderHandle = null;
let flushing = false;

function hasPendingWork() {
  return Boolean(
    pendingRender.groupList
      || pendingRender.streamedGroups
      || pendingRender.selection
      || pendingRender.members,
  );
}

function flushRenders() {
  renderHandle = null;
  flushing = true;
  try {
    const members = pendingRender.members;
    const list = pendingRender.groupList;
    const streamedGroups = pendingRender.streamedGroups;
    pendingRender.members = null;
    pendingRender.groupList = false;
    pendingRender.streamedGroups = false;
    if (members) renderMembers(members);
    if (list) renderGroupList();
    if (streamedGroups) finishStreamedGroupRender();
    const selection = pendingRender.selection;
    pendingRender.selection = false;
    if (selection) updateSelectionSummary();
  } finally {
    flushing = false;
    if (hasPendingWork()) scheduleRender();
  }
}

function scheduleRender({ groupList = false, streamedGroups = false, selection = false, members = null } = {}) {
  if (groupList || streamedGroups) pendingRender.groupList = true;
  if (streamedGroups) pendingRender.streamedGroups = true;
  if (selection) pendingRender.selection = true;
  if (members) pendingRender.members = members;
  if (!hasPendingWork() || flushing || renderHandle !== null) return;
  renderHandle =
    typeof requestAnimationFrame === "function"
      ? requestAnimationFrame(flushRenders)
      : setTimeout(flushRenders, 0);
}

export { scheduleRender };
