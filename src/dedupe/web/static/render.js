// Batched requestAnimationFrame rendering.

import { updateSelectionSummary } from "./actions.js";
import { finishStreamedGroupRender, renderGroupList } from "./groups.js";
import { renderMembers } from "./members.js";

// —— Batched rendering ——
const pendingRender = { groupList: false, streamedGroups: false, selection: false, members: null };
let renderHandle = null;

function flushRenders() {
  renderHandle = null;
  const members = pendingRender.members;
  const list = pendingRender.groupList;
  const streamedGroups = pendingRender.streamedGroups;
  const selection = pendingRender.selection;
  pendingRender.members = null;
  pendingRender.groupList = false;
  pendingRender.streamedGroups = false;
  pendingRender.selection = false;
  if (members) renderMembers(members);
  if (list) renderGroupList();
  if (streamedGroups) finishStreamedGroupRender();
  if (selection) updateSelectionSummary();
}

function scheduleRender({ groupList = false, streamedGroups = false, selection = false, members = null } = {}) {
  if (groupList || streamedGroups) pendingRender.groupList = true;
  if (streamedGroups) pendingRender.streamedGroups = true;
  if (selection) pendingRender.selection = true;
  if (members) pendingRender.members = members;
  if (renderHandle !== null) return;
  renderHandle =
    typeof requestAnimationFrame === "function"
      ? requestAnimationFrame(flushRenders)
      : setTimeout(flushRenders, 0);
}

export { scheduleRender };
