// Selection rules, bulk selection, and the Trash action flow.

import { api } from "./api.js";
import { applyResultControls, loadGroups, selectionFiltersActive, updateGroupListItem } from "./groups.js";
import { renderMembers, selectGroup } from "./members.js";
import { confirmModal } from "./modal.js";
import { currentGroup, isIndependentReview, isPagedIndependentReview, markGroupTouched } from "./model.js";
import { scheduleRender } from "./render.js";
import { state } from "./state.js";
import { refreshStatus } from "./status.js";
import { $, basename, escapeHtml, formatBytes, toast } from "./util.js";

const SCOPE_KINDS = {
  exact: ["exact"],
  similar: ["similar"],
  review_suggestions: ["low_resolution", "random_review"],
};

function scopeKinds(scope) {
  return SCOPE_KINDS[scope] || [scope];
}

function scopeLabelFor(scope) {
  if (scope === "exact") return "Exact matches";
  if (scope === "similar") return "Similar matches";
  return "Low-res + Random review files";
}

function effectiveSelection(scope) {
  const kinds = scopeKinds(scope);
  const source = state.allGroups.length ? state.allGroups : state.groups;
  const protectedPaths = new Set();
  for (const g of source) {
    if (!isIndependentReview(g)) continue;
    const selectedPaths = new Set(g.selected_for_removal || []);
    for (const path of g.reviewed_paths || []) {
      if (!selectedPaths.has(path)) protectedPaths.add(path);
    }
  }
  const selected = new Map();
  for (const g of source) {
    if (!kinds.includes(g.kind)) continue;
    const sel = new Set(g.selected_for_removal || []);
    const reviewed = new Set(g.reviewed_paths || []);
    for (const m of g.members || []) {
      if (
        !protectedPaths.has(m.path)
        && sel.has(m.path)
        && (!isIndependentReview(g) || reviewed.has(m.path))
      ) {
        selected.set(m.path, m);
      }
    }
  }
  for (const g of source) {
    if (!kinds.includes(g.kind)) continue;
    if (isIndependentReview(g) || !(g.members || []).length) continue;
    if (g.members.every((m) => selected.has(m.path))) {
      selected.delete(g.suggested_keep || g.members[0].path);
    }
  }
  return [...selected.values()];
}

function updateSelectionSummary() {
  for (const [id, scope] of [
    ["btnTrashExact", "exact"],
    ["btnTrashSimilar", "similar"],
    ["btnTrashReview", "review_suggestions"],
  ]) {
      const button = $(id);
      const count = effectiveSelection(scope).length;
      button.disabled = state.actionBusy || count === 0;
    button.title = count
      ? `${count} selected ${scopeLabelFor(scope).toLowerCase()}`
      : `No ${scopeLabelFor(scope).toLowerCase()} selected`;
  }
}

async function applyRuleToCurrentGroup(rule, successMessage) {
  if (!state.currentId) return toast("Select a group first");
  try {
    const g = await api("/api/smart-select", {
      method: "POST",
      body: JSON.stringify({
        rule,
        group_id: state.currentId,
        scan_id: state.scanId,
      }),
    });
    const idx = state.groups.findIndex((x) => x.id === g.id);
    if (idx >= 0) state.groups[idx] = g;
    const aidx = state.allGroups.findIndex((x) => x.id === g.id);
    if (aidx >= 0) state.allGroups[aidx] = g;
    markGroupTouched(g.id);
    renderMembers(g);
    if (selectionFiltersActive() || !updateGroupListItem(g)) {
      scheduleRender({ groupList: true });
    } else {
      applyResultControls();
    }
    scheduleRender({ selection: true });
    toast(successMessage, "ok");
  } catch (e) {
    toast(e.message, "error");
  }
}

$("btnSelectSuggested").addEventListener("click", async () => {
  const current = currentGroup();
  const rule = current?.kind === "no_humans" || current?.kind === "faces"
    ? "select_candidates"
    : "automatic";
  const message = current?.kind === "no_humans"
    ? "Reviewed non-human candidates selected"
    : current?.kind === "faces"
      ? "Reviewed face candidates selected"
      : "Suggested selection applied";
  await applyRuleToCurrentGroup(rule, message);
});

$("btnClearGroup").addEventListener("click", async () => {
  await applyRuleToCurrentGroup("deselect_all", "Group selection cleared");
});

$("btnSmartGroup").addEventListener("click", async () => {
  await applyRuleToCurrentGroup($("smartRule").value, "Selection rule applied to group");
});

$("btnSmartAll").addEventListener("click", async () => {
  try {
    await api("/api/smart-select", {
      method: "POST",
      body: JSON.stringify({ rule: $("smartRule").value, scan_id: state.scanId }),
    });
    state.allGroups.forEach((group) => markGroupTouched(group.id));
    await loadGroups();
    toast("Smart select applied to all groups", "ok");
  } catch (e) {
    toast(e.message, "error");
  }
});

// —— Bulk selection over the filtered view ——
async function runBulkSelection(operation, criteria = null, label = "") {
  const groupIds = state.groups.map((group) => group.id);
  if (!groupIds.length) return toast("No groups are shown in this filter");
  const independent = state.groups.filter(isIndependentReview).length;
  if (independent && operation !== "select_none") {
    const ok = await confirmModal({
      title: "Include independent review candidates?",
      confirmLabel: "Apply to all shown groups",
      danger: false,
      body: `<div class="review-sheet"><p><strong>${independent} independent review group${independent === 1 ? "" : "s"}</strong> ${independent === 1 ? "is" : "are"} in this view.</p><p>A bulk selection also marks those files reviewed, and every candidate can be selected. Nothing is deleted until you run and confirm an action.</p></div>`,
    });
    if (ok !== true) return;
  }
  try {
    const result = await api("/api/selection/bulk", {
      method: "POST",
      body: JSON.stringify({
        operation,
        group_ids: groupIds,
        criteria: criteria || {},
        scan_id: state.scanId,
      }),
    });
    groupIds.forEach(markGroupTouched);
    await loadGroups({ preserveSelection: true });
    if (state.currentId) await selectGroup(state.currentId, { silent: true });
    scheduleRender({ groupList: true, selection: true });
    const noun = result.changed_count === 1 ? "group" : "groups";
    toast(
      `${label || operation}: ${result.changed_count} ${noun} updated · ${result.selected_count} files selected`,
      "ok",
    );
  } catch (e) {
    toast(e.message, "error");
  }
}

$("btnBulkAll").addEventListener("click", () => runBulkSelection("select_all", null, "Select all"));
$("btnBulkNone").addEventListener("click", () => runBulkSelection("select_none", null, "Select none"));
$("btnBulkInvert").addEventListener("click", () => runBulkSelection("invert", null, "Invert"));

function syncBulkValueRow() {
  const rule = $("bulkCriteria").value;
  const needsValue = rule !== "smaller_than_keeper";
  $("bulkValueRow").hidden = !needsValue;
  $("bulkValue").placeholder = rule === "path_contains"
    ? "text or /folder/"
    : rule === "min_faces"
      ? "faces (e.g. 2)"
      : "MB";
}
$("bulkCriteria").addEventListener("change", syncBulkValueRow);
syncBulkValueRow();

$("btnBulkCriteria").addEventListener("click", () => {
  const rule = $("bulkCriteria").value;
  const raw = ($("bulkValue").value || "").trim();
  const criteria = {};
  if (rule === "smaller_than_keeper") {
    criteria.smaller_than_keeper = true;
  } else if (rule === "path_contains") {
    if (!raw) return toast("Enter the text a path must contain");
    criteria.path_contains = raw;
  } else if (rule === "min_faces") {
    const faces = Number(raw);
    if (!Number.isInteger(faces) || faces < 1) return toast("Enter a face count of 1 or more");
    criteria.min_faces = faces;
  } else {
    const megabytes = Number(raw);
    if (!Number.isFinite(megabytes) || megabytes < 0) return toast("Enter a size in MB");
    criteria[rule] = Math.round(megabytes * 1024 * 1024);
  }
  runBulkSelection("criteria", criteria, "Rule applied");
});

$("btnToggleDeleted").addEventListener("click", () => {
  const current = currentGroup();
  if (!isPagedIndependentReview(current)) return;
  state.showDeleted = !state.showDeleted;
  state.memberPage = 0;
  state.memberFocus = 0;
  state.trashedInPlace.clear();
  renderMembers(current);
});

$("btnMarkRemainingHuman").addEventListener("click", async () => {
  const remaining = state.allGroups
    .filter((group) => group.kind === "no_humans")
    .reduce((count, group) => {
      const deleted = new Set(group.deleted_paths || []);
      return count + (group.members || []).filter((member) => !deleted.has(member.path)).length;
    }, 0);
  if (!remaining) return;
  const noun = remaining === 1 ? "file" : "files";
  const ok = await confirmModal({
    title: "Mark remaining as human?",
    body: `<div class="review-sheet"><p>Mark <strong>${remaining} remaining ${noun}</strong> as containing humans?</p><p>They will not appear in future Non-Human scans unless the files change.</p></div>`,
    confirmLabel: "Mark as human",
    danger: false,
  });
  if (ok !== true) return;
  try {
    const result = await api("/api/non-human/mark-remaining-human", {
      method: "POST",
      body: JSON.stringify({ scan_id: state.scanId }),
    });
    await loadGroups();
    toast(`${result.marked_count} ${noun} marked as human`, "ok");
  } catch (e) {
    toast(e.message, "error");
  }
});

$("btnMarkDistinct").addEventListener("click", async () => {
  const current = state.allGroups.find((group) => group.id === state.currentId);
  if (!current || current.kind !== "similar") return;
  const ok = await confirmModal({
    title: "Mark as distinct?",
    body: `<div class="review-sheet"><p>Mark these <strong>${current.member_count} files</strong> as distinct?</p><p>This group will stay hidden in future scans unless one of the files changes.</p></div>`,
    confirmLabel: "Mark as distinct",
    danger: false,
  });
  if (ok !== true) return;
  try {
    await api("/api/similar/mark-distinct", {
      method: "POST",
      body: JSON.stringify({ group_id: current.id, scan_id: state.scanId }),
    });
    await loadGroups();
    toast("Similar files marked as distinct", "ok");
  } catch (e) {
    toast(e.message, "error");
  }
});

// —— Actions ——
const MAX_PREVIEW_REFRESHES = 2;

function previewNoticeHtml(notice) {
  return notice ? `<p class="preview-notice">${escapeHtml(notice)}</p>` : "";
}

// Disable the action bar and announce progress while a file action is in
// flight; preflight re-hashing and the synchronous execute can take minutes,
// and after the scan there is no polling to notice the server's acting flag.
function setActionBusy(busy, label = "") {
  state.actionBusy = busy;
  document.querySelectorAll("#actionBar button").forEach((button) => {
    button.disabled = busy;
  });
  $("actionBar").setAttribute("aria-busy", busy ? "true" : "false");
  const note = $("actionBusyNote");
  if (note) {
    note.hidden = !busy;
    note.textContent = label;
  }
}

// —— Action undo (restore an executed Trash from its receipts) ——

async function undoAction(receipts, { attempt = 0 } = {}) {
  setActionBusy(true, "Verifying the restore against the files on disk…");
  let preview;
  try {
    preview = await api("/api/action/undo", {
      method: "POST",
      body: JSON.stringify({ receipts, scan_id: state.scanId, dry_run: true }),
    });
  } catch (e) {
    setActionBusy(false);
    toast(e.message || String(e), "error");
    return;
  }
  setActionBusy(false);

  const restorable = (preview.items || []).filter((item) => !item.error);
  const blocked = (preview.items || []).filter((item) => item.error);
  const totalBytes = restorable.reduce((sum, item) => sum + (item.size || 0), 0);
  const blockedNote = blocked.length
    ? `<p><strong>${blocked.length} cannot be restored:</strong> ${escapeHtml(blocked[0].error)} (${escapeHtml(basename(blocked[0].destination || blocked[0].path))})${blocked.length > 1 ? ` · and ${blocked.length - 1} more` : ""}</p><p>Nothing restores while any file is blocked — resolve it, then press Undo again.</p>`
    : "";
  const ok = await confirmModal({
    title: `Restore ${restorable.length} file${restorable.length === 1 ? "" : "s"}?`,
    body: `<div class="review-sheet"><p><strong>${restorable.length} files · ${formatBytes(totalBytes)}</strong> return to their original locations on disk.</p>${blockedNote}<p>The current review is not re-populated — restored files reappear on the next scan.</p></div>`,
    confirmLabel: "Restore files",
    danger: false,
    validitySeconds: Number(preview.preview_expires_in) || null,
  });
  if (ok === "expired") {
    if (attempt >= MAX_PREVIEW_REFRESHES) {
      toast("Undo preview keeps expiring — press Undo again when ready", "error");
      return;
    }
    toast("Undo preview expired — re-checking the files…");
    return undoAction(receipts, { attempt: attempt + 1 });
  }
  if (!ok) return;

  setActionBusy(true, "Restoring files…");
  try {
    const res = await api("/api/action/undo", {
      method: "POST",
      body: JSON.stringify({
        receipts,
        scan_id: state.scanId,
        dry_run: false,
        preview_token: preview.preview_token,
      }),
    });
    toast(
      res.fail_count
        ? `Restored ${res.success_count}, ${res.fail_count} failed`
        : `Restored ${res.success_count} file${res.success_count === 1 ? "" : "s"} to their original locations`,
      res.fail_count ? "error" : "ok",
    );
    setActionBusy(false);
  } catch (e) {
    const stale = e.data?.preview_stale
      || /preview expired|selection changed|fresh preview/i.test(e.message);
    if (stale && attempt < MAX_PREVIEW_REFRESHES) {
      // Never restore on a stale token: re-run the dry run and re-confirm.
      toast(`${e.message} — re-checking now…`);
      return undoAction(receipts, { attempt: attempt + 1 });
    }
    setActionBusy(false);
    toast(e.message || String(e), "error");
  }
}

async function runDelete(scope, options = {}) {
  const { attempt = 0, notice = "" } = options;
  const scopeLabel = scopeLabelFor(scope);
  const count = effectiveSelection(scope).length;
  if (count === 0) {
    toast(`No ${scopeLabel.toLowerCase()} selected for removal`);
    return;
  }

  setActionBusy(true, "Verifying the selection against the files on disk…");
  let preview;
  try {
    preview = await api("/api/action", {
      method: "POST",
      body: JSON.stringify({
        action: "trash",
        dry_run: true,
        quarantine_dir: null,
        scan_id: state.scanId,
        kinds: scope,
      }),
    });
  } catch (error) {
    setActionBusy(false);
    toast(`Could not verify selection: ${error.message}`, "error");
    return;
  }

  if (preview.success_count === 0) {
    toast(`No verified ${scopeLabel.toLowerCase()} are eligible for deletion`);
    try {
      await loadGroups();
    } catch (e) {
      toast(e.message || String(e), "error");
    }
    setActionBusy(false);
    return;
  }

  const selectedMembers = effectiveSelection(scope);
  const totalBytes = selectedMembers.reduce((sum, member) => sum + (member.size || 0), 0);
  const skippedWarning = preview.fail_count
    ? `<p><strong>${preview.success_count} eligible</strong> · ${preview.fail_count} skipped (stale/unavailable)</p>`
    : "";
  const heuristicWarning = scope === "similar"
    ? '<p class="heuristic-warning"><strong>Review carefully:</strong> Similar matching is heuristic, not a guarantee.</p>'
    : "";
  // Low-res/Random review selections quarantine beside the scan root instead
  // of the system Trash — lead with that split so the destination is never a
  // surprise discovered after the fact.
  const reviewQuarantineCount = preview.review_quarantine_count || 0;
  const reviewQuarantineNote = reviewQuarantineCount
    ? `<p><strong>${reviewQuarantineCount} Low-res/Random review file${reviewQuarantineCount === 1 ? "" : "s"}</strong> will move to <code>${escapeHtml(preview.review_quarantine_dir)}</code> instead of system Trash.</p>`
    : "";
  const reviewScope = scope === "review_suggestions";
  const titleScope = scope === "exact"
    ? "exact matches"
    : scope === "similar"
      ? "similar matches"
      : "Low-res + Random review files";
  // Duplicate groups always retain a keeper; independent review candidates
  // can all be selected, and they quarantine instead of going to system Trash.
  const destinationNote = reviewScope
    ? "<p>Every selected file moves to the quarantine folder named above — not the system Trash. The saved receipt can restore them.</p>"
    : "<p>At least one file is always kept in every duplicate group. Selected files go to system Trash and can be restored there.</p>";
  const ok = await confirmModal({
    title: `Delete all selected ${titleScope}?`,
    body: `<div class="review-sheet">${previewNoticeHtml(notice)}${reviewQuarantineNote}<p><strong>${preview.success_count} unique files · ${formatBytes(totalBytes)}</strong></p>${skippedWarning}${destinationNote}${heuristicWarning}</div>`,
    confirmLabel: reviewScope ? "Move to Quarantine" : "Move to Trash",
    danger: true,
    validitySeconds: Number(preview.preview_expires_in) || null,
  });
  if (ok === "expired") {
    if (attempt >= MAX_PREVIEW_REFRESHES) {
      setActionBusy(false);
      toast("Preview keeps expiring — try again when you are ready to confirm", "error");
      return;
    }
    toast("Preview expired — re-checking the current selection…");
    return runDelete(scope, {
      attempt: attempt + 1,
      notice: "The previous preview expired. These numbers were just re-verified — confirm them again.",
    });
  }
  if (!ok) {
    setActionBusy(false);
    return;
  }

  setActionBusy(true, "Moving files to Trash…");
  try {
    const res = await api("/api/action", {
      method: "POST",
      body: JSON.stringify({
        action: "trash",
        dry_run: false,
        quarantine_dir: null,
        scan_id: state.scanId,
        kinds: scope,
        preview_token: preview.preview_token,
      }),
    });
    let msg = res.fail_count > 0
      ? `Done: ${res.success_count} ok, ${res.fail_count} skipped`
      : `Done: ${res.success_count} ok, ${res.fail_count} failed`;
    if (res.review_quarantine_count) {
      msg += ` · ${res.review_quarantine_count} in _Dedupe Quarantine`;
    }
    if (res.log_path) msg += ` · receipt saved`;
    if (res.fail_count) {
      const failed = res.items?.find((item) => item.error);
      if (failed) {
        msg += ` · ${basename(failed.path)}: ${failed.error}`;
      }
    }
    // Undo restores files to their original paths from the action's
    // receipts; the toast is sticky so the restore never times out.
    const undoReceipts = res.log_paths?.length
      ? res.log_paths
      : (res.log_path ? [res.log_path] : []);
    toast(
      msg,
      res.fail_count ? "" : "ok",
      undoReceipts.length
        ? { actionLabel: "Undo", onAction: () => undoAction(undoReceipts) }
        : {},
    );
    await loadGroups();
    await refreshStatus();
    setActionBusy(false);
  } catch (e) {
    const stale = e.data?.preview_stale
      || /preview expired|selection changed|fresh preview/i.test(e.message);
    if (stale && attempt < MAX_PREVIEW_REFRESHES) {
      // Never execute on a stale token: re-run the dry run and re-confirm.
      toast(`${e.message} — re-checking now…`);
      return runDelete(scope, {
        attempt: attempt + 1,
        notice: `${e.message.charAt(0).toUpperCase()}${e.message.slice(1)}. These numbers were just re-verified — confirm them again.`,
      });
    }
    setActionBusy(false);
    toast(e.message, "error");
  }
}

$("btnTrashExact").addEventListener("click", () => runDelete("exact"));
$("btnTrashSimilar").addEventListener("click", () => runDelete("similar"));
$("btnTrashReview").addEventListener("click", () => runDelete("review_suggestions"));

export { updateSelectionSummary, applyRuleToCurrentGroup, runDelete };
