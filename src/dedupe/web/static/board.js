// Similar board: every Similar group as one compact row, so a library's
// near-duplicates are reviewed on one screen instead of one pair at a time.
// Rows start with the suggested selection (every copy but the keeper marked
// for removal). Clicks and keys only change selections — cheap, in-memory
// server updates — and nothing touches disk until the footer's single batch
// Trash, which goes through the usual preview-and-confirm sheet (one receipt,
// one Undo). "Not duplicates" marks a whole group distinct and collapses its
// row in place with an Undo.

import { api } from "./api.js";
import { effectiveSelection } from "./actions.js";
import {
  applyResultControls,
  groupMatchesSidebarFilters,
  loadGroups,
  renderGroupList,
  renderTabCounts,
} from "./groups.js";
import { openLightbox } from "./lightbox.js";
import { selectGroup } from "./members.js";
import {
  lightboxItemFor,
  markGroupTouched,
  memberDiffPills,
  patchGroup,
} from "./model.js";
import { scheduleRender } from "./render.js";
import { expandScanPanel } from "./settings.js";
import { state } from "./state.js";
import { $, basename, escapeHtml, formatBytes, toast, withMediaVersion } from "./util.js";

// Rows rendered per batch; scrolling near the end appends the next batch.
const BOARD_CHUNK = 30;
// A copy whose fingerprint agreement with the keeper is below this (or
// unknown) puts its group under "Needs a look".
const UNCERTAIN_PERCENT = 95;
const SIMILAR_VIEW_KEY = "dedupe.similarView";
const TILE_SIZE_KEY = "dedupe.boardTileSize";

function boardActive() {
  return state.kind === "similar" && state.similarView === "board";
}

function setSimilarView(view) {
  state.similarView = view;
  try {
    localStorage.setItem(SIMILAR_VIEW_KEY, view);
  } catch {
    /* private mode */
  }
}

// —— Derived rows ——

// The suggested keeper leads each row; trashed members are excluded so they
// never render as kept copies; the rest keep server order so a selection
// change never moves tiles around.
function orderedMembers(g) {
  const deleted = new Set(g?.deleted_paths || []);
  const members = (g?.members || []).filter((member) => !deleted.has(member.path));
  const keeper = members.find((member) => member.path === g.suggested_keep);
  return keeper ? [keeper, ...members.filter((member) => member !== keeper)] : [...members];
}

function needsALook(g) {
  return orderedMembers(g).some(
    (member) => member.path !== g.suggested_keep
      && !(member.similarity_percent != null && Number(member.similarity_percent) >= UNCERTAIN_PERCENT),
  );
}

function matchesBoardFilter(g) {
  const filter = state.boardFilter || "all";
  if (filter === "uncertain") return needsALook(g);
  if (filter === "strong") return orderedMembers(g).length >= 2 && !needsALook(g);
  if (filter === "semantic") return g.match_basis === "semantic";
  if (filter === "videos") {
    return g.media_type === "video" || orderedMembers(g).some((member) => member.media_type === "video");
  }
  return true;
}

// Stable discovery order for tie-breaking and restoring dismissed rows across
// filter/sort changes without splice-index drift.
let boardOrderScanId = null;
const boardOriginalOrder = new Map();
let boardNextOrder = 0;

// "Not duplicates" placeholders belong to the scan session they were made in.
function syncDismissedScope() {
  if (boardOrderScanId !== state.scanId) {
    boardOrderScanId = state.scanId;
    boardOriginalOrder.clear();
    boardNextOrder = 0;
  }
  for (const g of state.allGroups) {
    if (g?.kind === "similar" && !boardOriginalOrder.has(g.id)) {
      boardOriginalOrder.set(g.id, boardNextOrder++);
    }
  }
  if (state.boardDismissedScanId === state.scanId) return;
  state.boardDismissed.clear();
  state.boardDismissedScanId = state.scanId;
}

// Board rows: the shown Similar groups (search, filters, and sort come from
// the shared result controls), narrowed by the board's own filter, with
// matching "Not duplicates" placeholders sorted into their deterministic slots.
function boardEntries() {
  syncDismissedScope();
  const liveGroups = state.groups.filter(
    (g) => g.kind === "similar" && orderedMembers(g).length > 0 && matchesBoardFilter(g),
  );
  const liveIds = new Set(liveGroups.map((g) => g.id));
  const combined = liveGroups.map((group) => ({ group, dismissed: false }));
  for (const entry of state.boardDismissed.values()) {
    if (liveIds.has(entry.group.id)) continue;
    if (!matchesBoardFilter(entry.group)) continue;
    if (!groupMatchesSidebarFilters(entry.group)) continue;
    combined.push({ group: entry.group, dismissed: true });
  }
  const sort = state.boardSort || "opportunity";
  const newestDates = sort === "date"
    ? new Map(combined.map(({ group }) => [
      group.id,
      orderedMembers(group).reduce((latest, m) => Math.max(latest, m.mtime || 0), 0),
    ]))
    : null;
  combined.sort((a, b) => {
    const ga = a.group;
    const gb = b.group;
    let cmp = 0;
    if (sort === "reclaimable") {
      cmp = (gb.reclaimable_bytes || 0) - (ga.reclaimable_bytes || 0)
        || (gb.cleanup?.score || 0) - (ga.cleanup?.score || 0);
    } else if (sort === "date") {
      cmp = newestDates.get(gb.id) - newestDates.get(ga.id);
    } else {
      cmp = (gb.cleanup?.score || 0) - (ga.cleanup?.score || 0)
        || (gb.reclaimable_bytes || 0) - (ga.reclaimable_bytes || 0);
    }
    if (cmp !== 0) return cmp;
    return (boardOriginalOrder.get(ga.id) ?? 0) - (boardOriginalOrder.get(gb.id) ?? 0);
  });
  return combined;
}

function liveEntries(entries = boardEntries()) {
  return entries.filter((entry) => !entry.dismissed);
}

function groupById(id) {
  return state.groups.find((g) => g.id === id) || state.allGroups.find((g) => g.id === id);
}

function focusedGroup() {
  const g = groupById(state.boardFocus.id);
  return g && g.kind === "similar" ? g : null;
}

function focusedMember(g = focusedGroup()) {
  if (!g) return null;
  const members = orderedMembers(g);
  return members[Math.max(0, Math.min(members.length - 1, state.boardFocus.index))] || null;
}

// —— Rendering ——

function tileHtml(g, member, index, selected, rowFocused, liveMembers, keeperMember) {
  const remove = selected.has(member.path);
  const focused = rowFocused && index === state.boardFocus.index;
  const name = basename(member.path);
  const dims = member.width > 0 && member.height > 0 ? `${member.width}×${member.height}` : null;
  const percent = member.similarity_percent == null ? NaN : Number(member.similarity_percent);
  const turned = member.orientation_label ? ` · ${escapeHtml(member.orientation_label)}` : "";
  const chip = member.path === g.suggested_keep
    ? `<span class="board-chip keeper" title="${escapeHtml(g.cleanup?.keeper_reason || "Ranked by resolution, size, date, and path")}">Suggested keeper</span>`
    : g.match_basis === "video_version"
      ? `<span class="board-chip uncertain">${escapeHtml(member.match_label || "Matching video section")}</span>`
    : member.match_label
      ? `<span class="board-chip uncertain" title="EmbeddingGemma cosine similarity with the suggested keeper: same subject, not necessarily the same shot">${escapeHtml(member.match_label)}</span>`
      : Number.isFinite(percent)
      ? `<span class="board-chip${percent < UNCERTAIN_PERCENT ? " uncertain" : ""}" title="Fingerprint agreement with the suggested keeper, not a probability">${percent.toFixed(1).replace(/\.0$/, "")}% match${turned}</span>`
      : `<span class="board-chip uncertain">score unavailable${turned}</span>`;
  const pills = keeperMember && member.path !== keeperMember.path
    ? memberDiffPills(member, keeperMember)
    : [];
  const pillsHtml = pills.length
    ? `<div class="board-diff-pills">${pills.map((p) => `<span class="diff-pill ${escapeHtml(p.kind || "")}"${p.title ? ` title="${escapeHtml(p.title)}"` : ""}>${escapeHtml(p.label)}</span>`).join("")}</div>`
    : "";
  const keptOthers = liveMembers.some((other) => other.path !== member.path && !selected.has(other.path));
  const state_ = remove ? "marked for removal, press to keep" : "kept, press to mark for removal";
  const thumbSrc = withMediaVersion(
    `/api/thumbnail?path=${encodeURIComponent(member.path)}&variant=grid`,
    member.path,
    member.media_version,
  );
  return `
      <div class="board-tile ${remove ? "remove" : "keep"}${focused ? " focused" : ""}" data-path="${escapeHtml(member.path)}" data-index="${index}">
        <button class="thumb-wrap board-thumb" type="button" data-path="${escapeHtml(member.path)}" data-index="${index}"
                aria-pressed="${remove}" aria-label="${escapeHtml(name)}: ${state_}" tabindex="${focused ? 0 : -1}">
          <img class="thumb-image" src="${escapeHtml(thumbSrc)}" alt="" loading="lazy" decoding="async" draggable="false" />
          <span class="thumb-badge ${remove ? "remove" : "keep"}">${member.protected ? "Protected" : remove ? "Remove" : "Keep"}</span>
          ${member.media_type === "video" ? '<span class="board-video-badge">Video</span>' : ""}
        </button>
        <button class="board-expand" type="button" data-index="${index}" tabindex="0" title="Open in the comparison view (Enter)" aria-label="Open ${escapeHtml(name)} in the comparison view">⤢</button>
        <div class="board-tile-meta">${chip}<span>${[dims, formatBytes(member.size)].filter(Boolean).join(" · ")}</span></div>
        ${pillsHtml}
        <div class="board-tile-name" title="${escapeHtml(member.path)}">${escapeHtml(name)}</div>
        ${remove || keptOthers ? `<button class="linkish board-keep-only" type="button" tabindex="0" title="Keep this file and mark every other copy for removal (Shift+K)">Keep only this</button>` : ""}
      </div>`;
}

function rowHtml(entry, position, total) {
  const g = entry.group;
  const members = orderedMembers(g);
  const deletedCount = (g.deleted_paths || []).length;
  const head = `
      <div class="board-row-head">
        <span class="board-row-num">${position}</span>
        <strong>${members.length} file${members.length === 1 ? "" : "s"}</strong>
        <span class="muted small">${escapeHtml(g.media_type || "")}${deletedCount ? ` · ${deletedCount} in Trash` : ""}</span>`;
  if (entry.dismissed) {
    return `
    <section class="board-row dismissed" data-id="${escapeHtml(g.id)}" aria-label="Group ${position} of ${total}: marked not duplicates">
      ${head}</div>
      <div class="board-dismissed-note">
        <span>Marked not duplicates — every file stays and this group won't come back</span>
        <button class="linkish board-undo-distinct" type="button">Undo</button>
      </div>
    </section>`;
  }
  const selected = new Set(g.selected_for_removal || []);
  const removeMembers = members.filter((member) => selected.has(member.path));
  const reclaim = removeMembers.reduce((sum, member) => sum + (member.size || 0), 0);
  const rowFocused = state.boardFocus.id === g.id;
  const keeperMember = members.find((member) => member.path === g.suggested_keep) || members[0];
  const keeperReason = g.cleanup?.keeper_reason;
  return `
    <section class="board-row${rowFocused ? " focused" : ""}" data-id="${escapeHtml(g.id)}"
             aria-label="Group ${position} of ${total}: ${members.length} files, ${removeMembers.length} marked for removal">
      ${head}
        <span class="board-row-reclaim">${removeMembers.length ? `${removeMembers.length} to Trash · ${formatBytes(reclaim)}` : "Keeping all"}</span>
        ${g.reviewed ? '<span class="board-chip reviewed">Reviewed</span>' : ""}
        ${keeperReason ? `<span class="board-keeper-reason muted small" title="${escapeHtml(keeperReason)}">${escapeHtml(keeperReason)}</span>` : ""}
      </div>
      <div class="board-tiles">${members.map((member, index) => tileHtml(g, member, index, selected, rowFocused, members, keeperMember)).join("")}</div>
      <div class="board-row-actions">
        <button class="btn ghost board-distinct" type="button" title="Different photos — keep every file and never group them again (n)">Not duplicates</button>
        <button class="btn ghost board-compare" type="button" title="Open this group in the side-by-side swipe review">Compare ↗</button>
      </div>
    </section>`;
}

function moreHtml(remaining) {
  return remaining > 0
    ? `<div class="board-more" id="boardMore"><button class="btn ghost" type="button">Show ${Math.min(remaining, BOARD_CHUNK)} more (${remaining} not shown)</button></div>`
    : "";
}

function updateBoardSummary(entries = boardEntries()) {
  const live = liveEntries(entries);
  const marked = effectiveSelection("similar");
  const bytes = marked.reduce((sum, member) => sum + (member.size || 0), 0);
  $("boardSummary").textContent = live.length
    ? `${live.length} group${live.length === 1 ? "" : "s"} · ${marked.length} ${marked.length === 1 ? "copy" : "copies"} marked for Trash · ${formatBytes(bytes)}`
    : "No similar groups to review";
  const categoryCount = state.allGroups.filter((g) => g.kind === "similar").length;
  const filtered = live.length < categoryCount;
  $("boardFiltered").hidden = !filtered;
  if (filtered) {
    $("boardFilteredText").textContent = `${live.length} of ${categoryCount} groups shown.`;
  }
  const bulkDisabled = Boolean(state.scanning || !live.length);
  if ($("btnBoardUseSuggested")) $("btnBoardUseSuggested").disabled = bulkDisabled;
  if ($("btnBoardMarkReviewed")) $("btnBoardMarkReviewed").disabled = bulkDisabled;
}

const boardObserver = new IntersectionObserver((items) => {
  if (items.some((item) => item.isIntersecting)) showMoreRows();
}, { rootMargin: "0px 0px 600px 0px" });

function observeMore() {
  boardObserver.disconnect();
  const more = $("boardMore");
  if (more) boardObserver.observe(more);
}

function renderBoard() {
  const active = boardActive();
  const wasActive = $("results").classList.contains("board-mode");
  $("results").classList.toggle("board-mode", active);
  $("similarBoard").hidden = !active;
  if (!active) {
    boardObserver.disconnect();
    if (wasActive) scheduleRender({ selection: true });
    return;
  }
  scheduleRender({ selection: true });
  if ($("boardSearch") !== document.activeElement) $("boardSearch").value = $("resultSearch").value;
  $("boardFilter").value = state.boardFilter;
  if ($("boardSort")) $("boardSort").value = state.boardSort || "opportunity";
  const entries = boardEntries();
  updateBoardSummary(entries);
  const rows = $("boardRows");
  const live = liveEntries(entries);
  if (!live.length && !entries.length) {
    const categoryHasGroups = state.allGroups.some((g) => g.kind === "similar");
    rows.innerHTML = `<div class="board-empty">
      <strong>${categoryHasGroups ? "No matching groups" : state.scanning ? "Waiting for results" : "No similar groups"}</strong>
      <p class="muted">${categoryHasGroups ? "Try a different search or filter." : state.scanning ? "Similar groups appear here as the scan finds them." : "This scan found no near-duplicate photos or videos."}</p>
      ${!categoryHasGroups && !state.scanning ? '<button class="btn ghost" type="button" data-board-empty="setup">Change scan setup</button>' : ""}
    </div>`;
    boardObserver.disconnect();
    return;
  }
  if (!live.some((entry) => entry.group.id === state.boardFocus.id)) {
    state.boardFocus = { id: live[0]?.group.id ?? null, index: 0 };
  }
  const focusIndex = entries.findIndex((entry) => entry.group.id === state.boardFocus.id);
  state.boardLimit = Math.max(state.boardLimit, focusIndex + 1);
  const hadFocus = Boolean(document.activeElement?.closest?.("#boardRows"));
  rows.innerHTML = entries
    .slice(0, state.boardLimit)
    .map((entry, index) => rowHtml(entry, index + 1, entries.length))
    .join("") + moreHtml(entries.length - state.boardLimit);
  observeMore();
  if (hadFocus) focusTile({ scroll: false });
}

function showMoreRows() {
  if (!boardActive()) return;
  const entries = boardEntries();
  const from = state.boardLimit;
  if (from >= entries.length) return;
  state.boardLimit = Math.min(entries.length, from + BOARD_CHUNK);
  $("boardMore")?.remove();
  $("boardRows").insertAdjacentHTML(
    "beforeend",
    entries.slice(from, state.boardLimit)
      .map((entry, index) => rowHtml(entry, from + index + 1, entries.length))
      .join("") + moreHtml(entries.length - state.boardLimit),
  );
  observeMore();
}

function patchRowInPlace(node, g, entries) {
  const members = orderedMembers(g);
  const tiles = [...node.querySelectorAll(".board-tile")];
  if (
    node.classList.contains("dismissed")
    || tiles.length !== members.length
    || tiles.some((tile, index) => tile.dataset.path !== members[index].path)
  ) {
    return false;
  }
  const selected = new Set(g.selected_for_removal || []);
  const removeMembers = members.filter((member) => selected.has(member.path));
  const reclaim = removeMembers.reduce((sum, member) => sum + (member.size || 0), 0);
  const posNum = node.querySelector(".board-row-num")?.textContent || "1";
  node.setAttribute(
    "aria-label",
    `Group ${posNum} of ${entries.length}: ${members.length} files, ${removeMembers.length} marked for removal`,
  );
  const reclaimEl = node.querySelector(".board-row-reclaim");
  if (reclaimEl) {
    reclaimEl.textContent = removeMembers.length
      ? `${removeMembers.length} to Trash · ${formatBytes(reclaim)}`
      : "Keeping all";
  }
  const headEl = node.querySelector(".board-row-head");
  if (headEl) {
    const existingRev = headEl.querySelector(".board-chip.reviewed");
    if (g.reviewed && !existingRev) {
      const chip = document.createElement("span");
      chip.className = "board-chip reviewed";
      chip.textContent = "Reviewed";
      (reclaimEl || headEl).after(chip);
    } else if (!g.reviewed && existingRev) {
      existingRev.remove();
    }
  }
  tiles.forEach((tile, index) => {
    const member = members[index];
    const remove = selected.has(member.path);
    const keptOthers = members.some((other) => other.path !== member.path && !selected.has(other.path));
    tile.classList.toggle("remove", remove);
    tile.classList.toggle("keep", !remove);
    const thumb = tile.querySelector(".board-thumb");
    if (thumb) {
      const stateLabel = remove ? "marked for removal, press to keep" : "kept, press to mark for removal";
      thumb.setAttribute("aria-pressed", String(remove));
      thumb.setAttribute("aria-label", `${basename(member.path)}: ${stateLabel}`);
    }
    const badge = tile.querySelector(".thumb-badge");
    if (badge) {
      badge.classList.toggle("remove", remove);
      badge.classList.toggle("keep", !remove);
      badge.textContent = member.protected ? "Protected" : remove ? "Remove" : "Keep";
    }
    const keepOnlyBtn = tile.querySelector(".board-keep-only");
    const wantKeepOnly = remove || keptOthers;
    if (wantKeepOnly && !keepOnlyBtn) {
      const btn = document.createElement("button");
      btn.className = "linkish board-keep-only";
      btn.type = "button";
      btn.tabIndex = 0;
      btn.title = "Keep this file and mark every other copy for removal (Shift+K)";
      btn.textContent = "Keep only this";
      tile.append(btn);
    } else if (!wantKeepOnly && keepOnlyBtn) {
      if (document.activeElement === keepOnlyBtn) thumb?.focus({ preventScroll: true });
      keepOnlyBtn.remove();
    }
  });
  return true;
}

// Repaint one row in place (selection changes), keeping thumbnails & focus intact.
function refreshBoardRow(g) {
  if (!boardActive() || !g) return;
  const entries = boardEntries();
  const node = $("boardRows").querySelector(`.board-row[data-id="${CSS.escape(g.id)}"]`);
  if (node && !patchRowInPlace(node, g, entries)) {
    const position = entries.findIndex((entry) => entry.group.id === g.id);
    const hadFocus = node.contains(document.activeElement);
    node.outerHTML = rowHtml({ group: g, dismissed: false }, Math.max(1, position + 1), entries.length);
    if (hadFocus) focusTile({ scroll: false });
  }
  updateBoardSummary(entries);
}

// Move the focus classes and roving tabindex to state.boardFocus.
function focusTile({ scroll = true } = {}) {
  const rows = $("boardRows");
  rows.querySelectorAll(".board-row.focused").forEach((row) => row.classList.remove("focused"));
  rows.querySelectorAll(".board-tile.focused").forEach((tile) => {
    tile.classList.remove("focused");
    tile.querySelector(".board-thumb")?.setAttribute("tabindex", "-1");
  });
  const row = rows.querySelector(`.board-row[data-id="${CSS.escape(state.boardFocus.id || "")}"]`);
  if (!row) return;
  row.classList.add("focused");
  const tiles = row.querySelectorAll(".board-tile");
  const tile = tiles[Math.max(0, Math.min(tiles.length - 1, state.boardFocus.index))];
  if (!tile) return;
  tile.classList.add("focused");
  const thumb = tile.querySelector(".board-thumb");
  if (!thumb) return;
  thumb.setAttribute("tabindex", "0");
  thumb.focus({ preventScroll: true });
  if (scroll) row.scrollIntoView({ block: "nearest" });
}

// —— Selection (optimistic, coalesced per group) ——

const pendingSelection = new Map(); // group id → latest wanted selection
const selectionInFlight = new Map(); // group id → Promise

function selectionLocked() {
  if (!(state.scanning || state.actionBusy)) return false;
  toast("Selections are locked while a scan or file action runs", "error");
  return true;
}

function setRowSelection(g, wanted) {
  if (selectionLocked()) return;
  const selected = new Set(wanted);
  const ordered = (g.members || []).map((member) => member.path).filter((path) => selected.has(path));
  const optimistic = { ...g, selected_for_removal: ordered };
  patchGroup(optimistic);
  markGroupTouched(g.id);
  refreshBoardRow(optimistic);
  scheduleRender({ selection: true });
  pendingSelection.set(g.id, ordered);
  flushSelection(g.id);
}

function flushSelection(id) {
  if (selectionInFlight.has(id)) return selectionInFlight.get(id);
  const promise = (async () => {
    try {
      while (pendingSelection.has(id)) {
        const selected = pendingSelection.get(id);
        pendingSelection.delete(id);
        const updated = await api("/api/selection", {
          method: "POST",
          body: JSON.stringify({ group_id: id, selected, scan_id: state.scanId }),
        });
        // A newer click is queued: its optimistic state stays on screen.
        if (!pendingSelection.has(id)) {
          patchGroup(updated);
          refreshBoardRow(updated);
          scheduleRender({ selection: true });
        }
      }
    } catch (error) {
      pendingSelection.delete(id);
      toast(error.message || "Could not save that selection", "error");
      loadGroups().catch(() => {});
    } finally {
      selectionInFlight.delete(id);
    }
  })();
  selectionInFlight.set(id, promise);
  return promise;
}

async function flushPendingBoardSelections() {
  while (pendingSelection.size > 0 || selectionInFlight.size > 0) {
    for (const id of [...pendingSelection.keys()]) {
      flushSelection(id);
    }
    await Promise.allSettled([...selectionInFlight.values()]);
  }
}

function toggleMember(g, path) {
  const members = orderedMembers(g);
  if (members.some((member) => member.path === path && member.protected)) {
    toast("This file is in a protected library");
    return;
  }
  const selected = new Set(g.selected_for_removal || []);
  if (selected.has(path)) {
    selected.delete(path);
  } else {
    const othersKept = members.some((member) => member.path !== path && !selected.has(member.path));
    if (!othersKept) {
      toast("Each group keeps at least one file — use “Keep only this” on another copy instead");
      return;
    }
    selected.add(path);
  }
  setRowSelection(g, selected);
}

function keepOnly(g, path) {
  setRowSelection(
    g,
    orderedMembers(g)
      .filter((member) => !member.protected && member.path !== path)
      .map((member) => member.path),
  );
}

// —— Whole-group decisions ——

let distinctBusy = false;

async function markNotDuplicates(g) {
  if (distinctBusy || selectionLocked()) return;
  distinctBusy = true;
  pendingSelection.delete(g.id);
  if (selectionInFlight.has(g.id)) {
    await Promise.allSettled([selectionInFlight.get(g.id)]);
  }
  const entriesBefore = boardEntries();
  const index = entriesBefore.findIndex((entry) => entry.group.id === g.id);
  try {
    await api("/api/similar/mark-distinct", {
      method: "POST",
      body: JSON.stringify({ group_id: g.id, scan_id: state.scanId, source: "board" }),
    });
  } catch (error) {
    toast(error.message || "Could not mark the group", "error");
    return;
  } finally {
    distinctBusy = false;
  }
  state.boardDismissed.set(g.id, { group: g, index: Math.max(0, index) });
  // Focus moves on to the next group still under review.
  const liveBefore = liveEntries(entriesBefore);
  const at = liveBefore.findIndex((entry) => entry.group.id === g.id);
  const next = liveBefore[at + 1] || liveBefore[at - 1];
  const wasFocused = state.boardFocus.id === g.id;
  if (wasFocused) state.boardFocus = { id: next?.group.id ?? null, index: 0 };

  state.allGroups = state.allGroups.filter((group) => group.id !== g.id);
  state.groups = state.groups.filter((group) => group.id !== g.id);
  if (state.currentId === g.id) state.currentId = null;

  renderTabCounts();
  applyResultControls({ fromRender: true });
  const entriesAfter = boardEntries();
  const node = $("boardRows").querySelector(`.board-row[data-id="${CSS.escape(g.id)}"]`);
  if (node) {
    const position = Math.max(1, entriesAfter.findIndex((entry) => entry.group.id === g.id) + 1);
    node.outerHTML = rowHtml({ group: g, dismissed: true }, position, entriesAfter.length);
  } else {
    renderBoard();
  }
  updateBoardSummary(entriesAfter);
  if (wasFocused && state.boardFocus.id) focusTile({ scroll: false });
  scheduleRender({ selection: true });
  // The collapsed row carries the Undo, so the toast stays transient — a run
  // of "n" presses must not stack sticky toasts.
  toast("Not duplicates — every file stays; undo from the row", "ok");
}

function insertRestoredGroup(group) {
  if (!group?.id) return;
  const order = boardOriginalOrder.get(group.id) ?? Number.MAX_SAFE_INTEGER;
  const existingIndex = state.allGroups.findIndex((item) => item.id === group.id);
  if (existingIndex >= 0) {
    state.allGroups[existingIndex] = group;
    return;
  }
  const insertAt = state.allGroups.findIndex(
    (item) => (boardOriginalOrder.get(item.id) ?? Number.MAX_SAFE_INTEGER) > order,
  );
  if (insertAt < 0) state.allGroups.push(group);
  else state.allGroups.splice(insertAt, 0, group);
}

async function undoNotDuplicates(id) {
  if (!state.boardDismissed.has(id)) return;
  let res;
  try {
    res = await api("/api/similar/restore-group", {
      method: "POST",
      body: JSON.stringify({ group_id: id, scan_id: state.scanId }),
    });
  } catch (error) {
    toast(error.message || "Could not undo", "error");
    if (error.status === 404 || error.status === 409) {
      state.boardDismissed.delete(id);
      renderBoard();
    }
    return;
  }
  state.boardDismissed.delete(id);
  state.boardFocus = { id, index: 0 };
  if (res?.group) {
    insertRestoredGroup(res.group);
    renderTabCounts();
    applyResultControls({ fromRender: true });
    renderBoard();
    scheduleRender({ selection: true });
  } else {
    await loadGroups();
  }
  toast("Back on the board — these files can match again", "ok");
}

// —— Navigation ——

function openRowLightbox(g, index) {
  // The lightbox's remove toggle acts on the current group.
  state.currentId = g.id;
  state.lightboxItems = orderedMembers(g).map((member) => lightboxItemFor(member, g));
  openLightbox(index);
}

function openCompare(g) {
  setSimilarView("swipe");
  state.currentId = g.id;
  renderGroupList();
  selectGroup(g.id).catch((error) => toast(error.message || String(error), "error"));
}

function moveRow(delta) {
  const entries = boardEntries();
  const live = liveEntries(entries);
  if (!live.length) return;
  const at = live.findIndex((entry) => entry.group.id === state.boardFocus.id);
  const next = live[Math.max(0, Math.min(live.length - 1, (at < 0 ? 0 : at + delta)))];
  state.boardFocus = {
    id: next.group.id,
    index: Math.min(state.boardFocus.index, Math.max(0, orderedMembers(next.group).length - 1)),
  };
  const position = entries.indexOf(next);
  if (position >= state.boardLimit) {
    state.boardLimit = position + 1;
    renderBoard();
  }
  focusTile();
}

function moveTile(delta) {
  const g = focusedGroup();
  if (!g) return;
  const count = orderedMembers(g).length;
  state.boardFocus = {
    id: g.id,
    index: Math.max(0, Math.min(Math.max(0, count - 1), state.boardFocus.index + delta)),
  };
  focusTile();
}

function reveal(member) {
  if (!member) return;
  api(`/api/reveal?path=${encodeURIComponent(member.path)}&open=1`)
    .catch((error) => toast(error.message || String(error), "error"));
}

// Board keys; returns true when the key was handled.
function handleBoardKey(e) {
  const g = focusedGroup();
  switch (e.key) {
    case "j":
    case "ArrowDown":
      moveRow(1);
      return true;
    case "k":
    case "ArrowUp":
      moveRow(-1);
      return true;
    case "ArrowLeft":
      moveTile(-1);
      return true;
    case "ArrowRight":
      moveTile(1);
      return true;
    case " ": {
      const member = focusedMember(g);
      if (g && member) toggleMember(g, member.path);
      return true;
    }
    case "K": {
      const member = focusedMember(g);
      if (g && member) keepOnly(g, member.path);
      return true;
    }
    case "u":
      if (g) keepOnly(g, g.suggested_keep || orderedMembers(g)[0]?.path);
      return true;
    case "n":
    case "N":
      if (g) markNotDuplicates(g);
      return true;
    case "Enter":
      if (g) openRowLightbox(g, state.boardFocus.index);
      return true;
    case "c":
      if (g) openCompare(g);
      return true;
    case "r":
    case "R":
      reveal(focusedMember(g));
      return true;
    default:
      return false;
  }
}

// —— Wiring ——

$("boardRows").addEventListener("click", (event) => {
  if (event.target.closest('[data-board-empty="setup"]')) {
    expandScanPanel({ focusId: "scanPreset" });
    return;
  }
  const row = event.target.closest(".board-row[data-id]");
  if (!row) {
    if (event.target.closest("#boardMore button")) showMoreRows();
    return;
  }
  if (event.target.closest(".board-undo-distinct")) {
    undoNotDuplicates(row.dataset.id);
    return;
  }
  const g = groupById(row.dataset.id);
  if (!g || row.classList.contains("dismissed")) return;
  const tile = event.target.closest(".board-tile");
  if (tile) state.boardFocus = { id: g.id, index: Number(tile.dataset.index) || 0 };
  else if (state.boardFocus.id !== g.id) state.boardFocus = { id: g.id, index: 0 };
  if (event.target.closest(".board-thumb")) {
    toggleMember(g, tile.dataset.path);
  } else if (event.target.closest(".board-keep-only")) {
    keepOnly(g, tile.dataset.path);
  } else if (event.target.closest(".board-expand")) {
    openRowLightbox(g, Number(tile.dataset.index) || 0);
  } else if (event.target.closest(".board-distinct")) {
    markNotDuplicates(g);
  } else if (event.target.closest(".board-compare")) {
    openCompare(g);
  } else {
    focusTile({ scroll: false });
  }
});

// The board's search box mirrors the shared result search, so the sidebar
// and the board always filter the same way.
$("boardSearch").addEventListener("input", (event) => {
  $("resultSearch").value = event.target.value;
  $("resultSearch").dispatchEvent(new Event("input"));
});
$("boardClearFilters").addEventListener("click", () => {
  state.boardFilter = "all";
  $("btnClearFilters").click();
});
$("boardFilter").addEventListener("change", (event) => {
  state.boardFilter = event.target.value;
  state.boardLimit = BOARD_CHUNK;
  applyResultControls();
  renderBoard();
});
$("boardSort")?.addEventListener("change", (event) => {
  state.boardSort = event.target.value;
  state.boardLimit = BOARD_CHUNK;
  renderBoard();
});

$("btnBoardUseSuggested")?.addEventListener("click", async () => {
  if (selectionLocked()) return;
  const shown = liveEntries().map((entry) => entry.group);
  if (!shown.length) return toast("No shown groups to update");
  const eligible = shown.filter(
    (g) => g.match_basis !== "semantic" && g.match_basis !== "video_version",
  );
  if (!eligible.length) {
    return toast("Look-alike and video-version groups require manual review");
  }
  const btn = $("btnBoardUseSuggested");
  btn.disabled = true;
  try {
    for (const g of eligible) pendingSelection.delete(g.id);
    await flushPendingBoardSelections();
    await api("/api/smart-select", {
      method: "POST",
      body: JSON.stringify({
        rule: "automatic",
        group_ids: eligible.map((g) => g.id),
        scan_id: state.scanId,
      }),
    });
    eligible.forEach((g) => markGroupTouched(g.id));
    await loadGroups({ preserveSelection: true });
    renderBoard();
    scheduleRender({ selection: true });
    toast(
      `Applied suggested selection to ${eligible.length} group${eligible.length === 1 ? "" : "s"}`,
      "ok",
    );
  } catch (error) {
    toast(error.message || "Could not reset selections", "error");
  } finally {
    updateBoardSummary();
  }
});

$("btnBoardMarkReviewed")?.addEventListener("click", async () => {
  if (selectionLocked()) return;
  const shown = liveEntries().map((entry) => entry.group).filter((g) => !g.reviewed);
  if (!shown.length) {
    return toast("All shown groups are already marked reviewed", "ok");
  }
  const btn = $("btnBoardMarkReviewed");
  btn.disabled = true;
  try {
    await flushPendingBoardSelections();
    const updatedGroups = await Promise.all(
      shown.map((g) =>
        api("/api/group/reviewed", {
          method: "POST",
          body: JSON.stringify({
            group_id: g.id,
            reviewed: true,
            scan_id: state.scanId,
          }),
        })),
    );
    updatedGroups.forEach(patchGroup);
    applyResultControls({ fromRender: true });
    renderBoard();
    scheduleRender({ selection: true });
    toast(
      `Marked ${updatedGroups.length} group${updatedGroups.length === 1 ? "" : "s"} reviewed`,
      "ok",
    );
  } catch (error) {
    toast(error.message || "Could not mark groups reviewed", "error");
  } finally {
    updateBoardSummary();
  }
});

try {
  const size = Number(localStorage.getItem(TILE_SIZE_KEY));
  if (size >= 120 && size <= 280) $("boardTileSize").value = String(size);
} catch {
  /* private mode */
}
$("boardRows").style.setProperty("--board-tile", `${$("boardTileSize").value}px`);
$("boardTileSize").addEventListener("input", (event) => {
  $("boardRows").style.setProperty("--board-tile", `${event.target.value}px`);
  try {
    localStorage.setItem(TILE_SIZE_KEY, event.target.value);
  } catch {
    /* private mode */
  }
});

document.querySelectorAll("[data-similar-view]").forEach((button) => {
  button.addEventListener("click", () => {
    const view = button.dataset.similarView;
    if (view === "board") return;
    const g = focusedGroup() || state.groups.find((group) => group.kind === "similar");
    setSimilarView(view);
    if (g) {
      state.currentId = g.id;
      renderGroupList();
      selectGroup(g.id).catch((error) => toast(error.message || String(error), "error"));
    } else {
      renderGroupList();
    }
  });
});

// Detail header's "Board" button: back to the board, on the group just viewed.
$("btnSimilarBoard").addEventListener("click", () => {
  setSimilarView("board");
  if (state.currentId) state.boardFocus = { id: state.currentId, index: 0 };
  if (state.kind !== "similar") {
    document.querySelector('.tab[data-kind="similar"]')?.click();
    return;
  }
  renderGroupList();
  requestAnimationFrame(() => focusTile());
});

export {
  boardActive,
  renderBoard,
  refreshBoardRow,
  handleBoardKey,
  flushPendingBoardSelections,
};
