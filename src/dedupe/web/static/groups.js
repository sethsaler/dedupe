// The group review list: fetch, filter, sort, windowed rendering.

import { api } from "./api.js";
import { groupComplete, groupNeedsAttention, groupSelectedCount, isIndependentReview } from "./model.js";
import { scheduleRender } from "./render.js";
import { selectGroup } from "./members.js";
import { GROUP_RENDER_CHUNK, state } from "./state.js";
import { $, basename, escapeHtml, formatBytes, toast } from "./util.js";

const GROUP_FETCH_PAGE = 250;
// At most this many sidebar rows live in the DOM; scrolling slides the window.
const GROUP_WINDOW_MAX = GROUP_RENDER_CHUNK * 5;

async function fetchAllGroups(token) {
  // Pages are only consistent within one groups_version; mid-scan the list
  // keeps growing (append-only), so restart when the version moves under us.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const collected = [];
    let offset = 0;
    let total = 0;
    let version = null;
    let stale = false;
    for (;;) {
      const page = await api(`/api/groups?kind=all&offset=${offset}&limit=${GROUP_FETCH_PAGE}`);
      if (token !== state.groupsLoadToken) return null;
      if (version === null) version = page.groups_version;
      else if (page.groups_version !== version) {
        stale = true;
        break;
      }
      const batch = page.groups || [];
      collected.push(...batch);
      total = Number.isFinite(Number(page.total)) ? Number(page.total) : collected.length;
      offset += batch.length;
      if (collected.length === batch.length && collected.length < total) {
        // Paint the first page immediately so streamed groups appear without delay.
        state.allGroups = collected;
        renderGroupList();
      }
      if (!batch.length || collected.length >= total) break;
    }
    if (!stale) {
      return collected;
    }
  }
  const fallback = await api(`/api/groups?kind=all`);
  if (token !== state.groupsLoadToken) return null;
  return fallback.groups || [];
}

function renderTabCounts() {
  const memberCount = (kind) => state.allGroups
    .filter((g) => g.kind === kind)
    .reduce((count, g) => count + (g.member_count || 0), 0);
  $("countAll").textContent = state.allGroups.length;
  $("countExact").textContent = state.allGroups.filter((g) => g.kind === "exact").length;
  $("countSimilar").textContent = state.allGroups.filter((g) => g.kind === "similar").length;
  $("countLowResolution").textContent = memberCount("low_resolution");
  $("countRandomReview").textContent = memberCount("random_review");
  $("countNoHumans").textContent = memberCount("no_humans");
  $("countFaces").textContent = memberCount("faces");
  const countAllFiles = $("countAllFiles");
  if (countAllFiles) countAllFiles.textContent = memberCount("all_files");
}

// The focused group survives a page reload: remembered in sessionStorage and
// restored on the first group-list load, if it still exists after the scan.
const FOCUS_KEY = "dedupe.focusedGroupId";
let focusRestorePending = (() => {
  try {
    return sessionStorage.getItem(FOCUS_KEY) || null;
  } catch {
    return null;
  }
})();

function rememberFocusedGroup(id) {
  try {
    if (id) sessionStorage.setItem(FOCUS_KEY, id);
    else sessionStorage.removeItem(FOCUS_KEY);
  } catch {
    /* ignore */
  }
}

async function loadGroups({ preserveSelection = false } = {}) {
  const token = ++state.groupsLoadToken;
  const all = await fetchAllGroups(token);
  if (all === null) return;
  state.allGroups = all;
  applyResultControls();
  renderTabCounts();

  if (state.groups.length) {
    $("emptyState").hidden = true;
    $("results").hidden = false;
  }

  scheduleRender({ groupList: true, selection: true });
  if (state.currentId) {
    const still = state.groups.find((g) => g.id === state.currentId);
    if (still) {
      // Mid-scan: keep list fresh but don't thrash an open detail view
      // (member set for a group is fixed once published).
      if (!preserveSelection) {
        await selectGroup(state.currentId, { silent: true });
      }
    } else {
      state.currentId = null;
      $("detailBody").hidden = true;
      $("detailEmpty").hidden = false;
    }
  } else if (state.groups.length && !$("results").hidden) {
    // Auto-select when nothing is selected: the group the user was looking at
    // before a reload, if it still exists, otherwise the first group.
    if (!$("detailEmpty").hidden) {
      let targetId = state.groups[0].id;
      if (focusRestorePending) {
        const wanted = focusRestorePending;
        focusRestorePending = null;
        if (state.groups.some((g) => g.id === wanted)) targetId = wanted;
      }
      await selectGroup(targetId, { silent: true });
    }
  }
}

// One group pushed by the SSE stream mid-scan: fold it into the local view
// without refetching the whole list.

function addStreamedGroup(g) {
  if (!g || !g.id) return;
  const existing = state.allGroups.findIndex((candidate) => candidate.id === g.id);
  if (existing >= 0) state.allGroups[existing] = g;
  else state.allGroups.push(g);
  applyResultControls();
  renderTabCounts();
  if (state.groups.length) {
    $("emptyState").hidden = true;
    $("results").hidden = false;
  }
  scheduleRender({ groupList: true, selection: true });
  if (
    !state.currentId
    && state.groups.length
    && !$("results").hidden
    && !$("detailEmpty").hidden
  ) {
    selectGroup(state.groups[0].id, { silent: true })
      .catch((e) => toast(e.message || String(e), "error"));
  }
}

function numericFilter(id) {
  const raw = ($(id).value || "").trim();
  if (!raw) return null;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

function pathMatcher() {
  const raw = ($("filterPathPattern").value || "").trim();
  if (!raw) return null;
  if (!/[*?]/.test(raw)) {
    const needle = raw.toLowerCase();
    return (path) => path.toLowerCase().includes(needle);
  }
  const expression = raw
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".");
  let pattern;
  try {
    pattern = new RegExp(expression, "i");
  } catch {
    return null;
  }
  return (path) => pattern.test(path);
}

function advancedFilters() {
  const minMb = numericFilter("filterMinMb");
  const maxMb = numericFilter("filterMaxMb");
  const filters = {
    minSize: minMb == null ? null : minMb * 1024 * 1024,
    maxSize: maxMb == null ? null : maxMb * 1024 * 1024,
    minWidth: numericFilter("filterMinWidth"),
    minHeight: numericFilter("filterMinHeight"),
    matchPath: pathMatcher(),
    faces: $("filterFaces").value === "any" ? null : $("filterFaces").value,
  };
  filters.active =
    filters.minSize != null ||
    filters.maxSize != null ||
    filters.minWidth != null ||
    filters.minHeight != null ||
    !!filters.matchPath ||
    filters.faces != null;
  return filters;
}

function memberMatchesFilters(member, filters) {
  if (filters.minSize != null && (member.size || 0) < filters.minSize) return false;
  if (filters.maxSize != null && (member.size || 0) > filters.maxSize) return false;
  if (filters.minWidth != null && (member.width || 0) < filters.minWidth) return false;
  if (filters.minHeight != null && (member.height || 0) < filters.minHeight) return false;
  if (filters.matchPath && !filters.matchPath(member.path)) return false;
  if (filters.faces) {
    // Files without a trusted face count never match: "No faces" must mean
    // the counter actually ran and found zero, not "never analyzed".
    if (member.face_count == null) return false;
    if (filters.faces === "has" && member.face_count < 1) return false;
    // Male matching needs the genderage pass; scans predating it report no
    // male count and never match.
    if (filters.faces === "has_male" && (member.male_face_count || 0) < 1) return false;
    if (filters.faces === "none" && member.face_count !== 0) return false;
  }
  return true;
}

function applyResultControls() {
  const query = ($("resultSearch").value || "").trim().toLowerCase();
  const selection = $("selectionFilter").value;
  const filters = advancedFilters();
  $("advancedFilterFlag").hidden = !filters.active;
  let groups = state.allGroups.filter((g) => state.kind === "all" || g.kind === state.kind);
  groups = groups.filter((g) => {
    const selected = groupSelectedCount(g) > 0;
    if (query && !(g.members || []).some((member) => member.path.toLowerCase().includes(query))) return false;
    if (selection === "selected" && !selected) return false;
    if (selection === "unselected" && selected) return false;
    if ($("issuesOnly").checked && !groupNeedsAttention(g)) return false;
    if ($("hideCompleted").checked && groupComplete(g)) return false;
    // Advanced filters keep a group when any one of its files qualifies.
    if (filters.active && !(g.members || []).some((member) => memberMatchesFilters(member, filters))) {
      return false;
    }
    return true;
  });
  const sort = $("resultSort").value;
  groups.sort((a, b) => {
    if (sort === "size") return (b.member_count || 0) - (a.member_count || 0);
    if (sort === "date") return Math.max(...(b.members || []).map((m) => m.mtime || 0), 0) - Math.max(...(a.members || []).map((m) => m.mtime || 0), 0);
    if (sort === "media") return String(a.media_type).localeCompare(String(b.media_type));
    return (b.reclaimable_bytes || 0) - (a.reclaimable_bytes || 0);
  });
  state.groups = groups;
  $("filteredCount").textContent = `${groups.length} of ${state.allGroups.length} groups shown`;
}

function groupItemHtml(g) {
  const active = g.id === state.currentId ? "active" : "";
  const sel = groupSelectedCount(g);
  const badgeLabel = {
    no_humans: "non-human",
    low_resolution: "low-res",
    random_review: "random",
    faces: "faces",
  }[g.kind] || (g.kind === "all_files" ? "all files" : g.kind);
  // All-Files groups are per scanned folder: name the folder so the sidebar
  // row doubles as the folder picker.
  const folderLabel = g.kind === "all_files" && g.root ? basename(g.root) : null;
  const reviewed = (g.reviewed_paths || []).length;
  const groupSummary = isIndependentReview(g)
    ? `${reviewed}/${g.member_count} reviewed${sel ? ` · ${sel} delete` : ""}`
    : `${formatBytes(g.reclaimable_bytes)} reclaimable`;
  // Status is never colour-only: the glyph and its label carry the same meaning.
  const attention = groupNeedsAttention(g);
  const stateLabel = attention ? "Needs review" : "Reviewed";
  const stateGlyph = attention ? "●" : "✔";
  return `
        <button class="group-item ${active} ${attention ? "attention" : "done"}" data-id="${g.id}" id="gopt-${g.id}" type="button" aria-current="${active ? "true" : "false"}">
          <div class="g-top">
            <span>${folderLabel ? `${escapeHtml(folderLabel)} · ` : ""}${g.member_count} files${isIndependentReview(g) ? "" : ` · ${escapeHtml(g.media_type)}`}</span>
            <span class="badge ${g.kind}">${badgeLabel}</span>
          </div>
          <div class="g-state"><span class="g-state-glyph" aria-hidden="true">${stateGlyph}</span>${stateLabel}</div>
          <div class="g-sub">
            <span>${groupSummary}</span>
            ${sel ? `<span class="sel-mark">${sel} selected</span>` : ""}
          </div>
        </button>
      `;
}

function groupMoreHtml() {
  const remaining = Math.max(
    0,
    state.groups.length - (state.groupListStart + state.groupListLimit),
  );
  if (!remaining) return "";
  return `
        <div class="group-more-wrap">
          <button class="btn ghost group-more" type="button">Show ${Math.min(remaining, GROUP_RENDER_CHUNK)} more (${remaining} hidden)</button>
        </div>`;
}

function groupEarlierHtml() {
  const earlier = state.groupListStart;
  if (!earlier) return "";
  return `
        <div class="group-more-wrap">
          <button class="btn ghost group-earlier" type="button">Show ${Math.min(earlier, GROUP_RENDER_CHUNK)} earlier (${earlier} above)</button>
        </div>`;
}

function syncEarlierSlot() {
  const slot = $("groupEarlier");
  if (slot) slot.innerHTML = groupEarlierHtml();
}

function wireGroupList() {
  const list = $("groupList");
  if (list.dataset.wired !== "1") {
    list.dataset.wired = "1";
    list.addEventListener("click", (event) => {
      const btn = event.target.closest(".group-item[data-id]");
      if (btn) selectGroup(btn.dataset.id).catch((e) => toast(e.message || String(e), "error"));
    });
    list.addEventListener("scroll", () => {
      if (list.scrollTop + list.clientHeight >= list.scrollHeight - 240) growGroupList();
      // Scrolling back up slides the window too; otherwise a long list hits a
      // hard edge and only the "Show earlier" button could rescue it.
      if (list.scrollTop <= 120 && state.groupListStart > 0) shrinkGroupListWindow();
    });
    const more = $("groupMore");
    more.dataset.wired = "1";
    more.addEventListener("click", (event) => {
      if (event.target.closest(".group-more")) growGroupList();
    });
    $("groupEarlier").addEventListener("click", (event) => {
      if (event.target.closest(".group-earlier")) shrinkGroupListWindow();
    });
  }
  return list;
}

function resetGroupListWindow() {
  state.groupListStart = 0;
  state.groupListLimit = GROUP_RENDER_CHUNK;
}

function growGroupList() {
  if (state.groupListStart + state.groupListLimit >= state.groups.length) return;
  const list = wireGroupList();
  if (state.groupListLimit < GROUP_WINDOW_MAX) {
    const from = state.groupListStart + state.groupListLimit;
    const to = Math.min(state.groups.length, from + GROUP_RENDER_CHUNK);
    state.groupListLimit = to - state.groupListStart;
    list.insertAdjacentHTML("beforeend", state.groups.slice(from, to).map(groupItemHtml).join(""));
    $("groupMore").innerHTML = groupMoreHtml();
    return;
  }
  // The window is full: slide it down, evicting the top chunk from the DOM.
  shiftGroupListWindow(state.groupListStart + GROUP_RENDER_CHUNK);
}

function rowGapPx(list) {
  return parseFloat(getComputedStyle(list).rowGap) || 0;
}

function shiftGroupListWindow(newStart) {
  const list = wireGroupList();
  const evictedCount = Math.max(0, newStart - state.groupListStart);
  const rows = [...list.querySelectorAll(".group-item")].slice(0, evictedCount);
  const evictedHeight = rows.reduce((sum, node) => sum + node.offsetHeight, 0)
    + rowGapPx(list) * rows.length;
  state.groupListStart = Math.max(
    0,
    Math.min(newStart, Math.max(0, state.groups.length - state.groupListLimit)),
  );
  renderGroupList();
  if (evictedHeight) list.scrollTop = Math.max(0, list.scrollTop - evictedHeight);
}

function shrinkGroupListWindow() {
  if (!state.groupListStart) return;
  const list = wireGroupList();
  const previousStart = state.groupListStart;
  state.groupListStart = Math.max(0, previousStart - GROUP_RENDER_CHUNK);
  renderGroupList();
  // Keep the viewport on the same rows: scroll past the newly added top chunk.
  const addedCount = previousStart - state.groupListStart;
  const rows = [...list.querySelectorAll(".group-item")].slice(0, addedCount);
  list.scrollTop = rows.reduce((sum, node) => sum + node.offsetHeight, 0)
    + rowGapPx(list) * rows.length;
}

function renderGroupList() {
  applyResultControls();
  const list = wireGroupList();
  if (!state.groups.length) {
    state.groupListStart = 0;
    list.innerHTML = `<div class="group-empty">No groups in this filter.</div>`;
    $("groupMore").innerHTML = "";
    syncEarlierSlot();
    return;
  }
  // Only the window [groupListStart, groupListStart + groupListLimit) lives in
  // the DOM; the rest streams in on scroll / slides via the window controls.
  if (state.groupListStart >= state.groups.length) state.groupListStart = 0;
  state.groupListLimit = Math.max(
    GROUP_RENDER_CHUNK,
    Math.min(
      state.groupListLimit,
      GROUP_WINDOW_MAX,
      state.groups.length - state.groupListStart,
    ),
  );
  const scrollTop = list.scrollTop;
  list.innerHTML = state.groups
    .slice(state.groupListStart, state.groupListStart + state.groupListLimit)
    .map(groupItemHtml)
    .join("");
  $("groupMore").innerHTML = groupMoreHtml();
  syncEarlierSlot();
  list.scrollTop = Math.min(scrollTop, Math.max(0, list.scrollHeight - list.clientHeight));
}

function updateGroupListItem(g) {
  const node = $("groupList").querySelector(`.group-item[data-id="${g.id}"]`);
  if (!node) return false;
  const holder = document.createElement("div");
  holder.innerHTML = groupItemHtml(g).trim();
  const fresh = holder.firstElementChild;
  if (!fresh) return false;
  node.className = fresh.className;
  node.setAttribute("aria-current", fresh.getAttribute("aria-current"));
  node.innerHTML = fresh.innerHTML;
  return true;
}

function selectionFiltersActive() {
  return (
    $("selectionFilter").value !== "all" ||
    $("issuesOnly").checked ||
    $("hideCompleted").checked
  );
}

function markGroupListActive(id) {
  const list = $("groupList");
  list.querySelectorAll(".group-item.active").forEach((node) => {
    node.classList.remove("active");
    node.setAttribute("aria-current", "false");
  });
  const node = list.querySelector(`.group-item[data-id="${id}"]`);
  if (node) {
    node.classList.add("active");
    node.setAttribute("aria-current", "true");
  }
  return node;
}

function ensureGroupVisible(id) {
  const index = state.groups.findIndex((g) => g.id === id);
  if (index < 0) return;
  if (index >= state.groupListStart && index < state.groupListStart + state.groupListLimit) return;
  if (index < state.groupListStart) {
    state.groupListStart = Math.max(
      0,
      Math.floor(index / GROUP_RENDER_CHUNK) * GROUP_RENDER_CHUNK,
    );
  } else {
    let limit = Math.ceil((index + 1 - state.groupListStart) / GROUP_RENDER_CHUNK) * GROUP_RENDER_CHUNK;
    if (limit > GROUP_WINDOW_MAX) {
      state.groupListStart = Math.max(0, index + 1 - GROUP_WINDOW_MAX);
      limit = GROUP_WINDOW_MAX;
    }
    state.groupListLimit = Math.min(state.groups.length - state.groupListStart, limit);
  }
  renderGroupList();
}

export { loadGroups, addStreamedGroup, applyResultControls, renderGroupList, resetGroupListWindow, updateGroupListItem, selectionFiltersActive, markGroupListActive, ensureGroupVisible, renderTabCounts, rememberFocusedGroup };
