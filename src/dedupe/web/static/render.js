// Batched requestAnimationFrame rendering.

import { updateSelectionSummary } from "./actions.js";
import { renderGroupList } from "./groups.js";
import { renderMembers } from "./members.js";

// —— Batched rendering ——
const pendingRender = { groupList: false, selection: false, members: null };
let renderHandle = null;

function flushRenders() {
  renderHandle = null;
  const members = pendingRender.members;
  const list = pendingRender.groupList;
  const selection = pendingRender.selection;
  pendingRender.members = null;
  pendingRender.groupList = false;
  pendingRender.selection = false;
  if (members) renderMembers(members);
  if (list) renderGroupList();
  if (selection) updateSelectionSummary();
}

function scheduleRender({ groupList = false, selection = false, members = null } = {}) {
  if (groupList) pendingRender.groupList = true;
  if (selection) pendingRender.selection = true;
  if (members) pendingRender.members = members;
  if (renderHandle !== null) return;
  renderHandle =
    typeof requestAnimationFrame === "function"
      ? requestAnimationFrame(flushRenders)
      : setTimeout(flushRenders, 0);
}

export { scheduleRender };
