// Exact-match review: byte-identical files need no visual comparison, only a
// survivor. The view shows one shared preview and a compact list of copies —
// pick the keeper and every other copy is marked for removal; a per-row
// Remove toggle keeps fine control (keeping more than one copy) possible.

import { api } from "./api.js";
import { applyResultControls, loadGroups, selectionFiltersActive, updateGroupListItem } from "./groups.js";
import { openLightbox } from "./lightbox.js";
import {
  currentGroup,
  lightboxItemFor,
  markGroupTouched,
  parentDirectory,
  parentFolderName,
  patchGroup,
} from "./model.js";
import { scheduleRender } from "./render.js";
import { state } from "./state.js";
import {
  $,
  basename,
  escapeHtml,
  formatBytes,
  formatMtime,
  setPreviewAspectRatio,
  toast,
  withMediaVersion,
} from "./util.js";

function previewUrl(member) {
  return withMediaVersion(
    `/api/thumbnail?path=${encodeURIComponent(member.path)}&variant=preview`,
    member.path,
    member.media_version,
  );
}

function thumbUrl(member) {
  return withMediaVersion(
    `/api/thumbnail?path=${encodeURIComponent(member.path)}`,
    member.path,
    member.media_version,
  );
}

function copyChipsHtml(g, member, isSelected, isSurvivor, keeper) {
  const diffDir = keeper
    && member.path !== keeper.path
    && parentDirectory(member.path) !== parentDirectory(keeper.path);
  const folder = diffDir ? parentFolderName(member.path) || "other folder" : "";
  return [
    member.protected ? '<span class="copy-chip keeping protected-label">Protected library</span>' : "",
    isSurvivor ? '<span class="copy-chip keeping">Keeping</span>' : "",
    member.path === g.suggested_keep ? '<span class="copy-chip suggested">Suggested</span>' : "",
    isSelected ? '<span class="copy-chip removing">Will be removed</span>' : "",
    diffDir
      ? `<span class="copy-chip diff-folder" title="In a different folder from the suggested keeper: ${escapeHtml(parentDirectory(member.path))}">in ${escapeHtml(folder)}</span>`
      : "",
    member.error ? `<span class="copy-chip error" title="${escapeHtml(member.error)}">Scan issue</span>` : "",
  ].join("");
}

function copyRowHtml(g, member, index, survivors, keeper, lightboxIndex) {
  const selected = new Set(g.selected_for_removal || []);
  const deleted = new Set(g.deleted_paths || []);
  const isSelected = selected.has(member.path);
  const isDeleted = deleted.has(member.path);
  const isSurvivor = !isSelected && !isDeleted;
  const soleSurvivor = isSurvivor && survivors.size === 1;
  const dims = Number.isFinite(member.width) && Number.isFinite(member.height)
    && member.width > 0 && member.height > 0
    ? `${member.width}×${member.height}`
    : "—";
  const chips = copyChipsHtml(g, member, isSelected, isSurvivor, keeper);
  const keepTitle = soleSurvivor
    ? "The surviving copy"
    : "Keep only this copy — every other copy is marked for removal";
  const thumb = thumbUrl(member);
  const mediaPreview = member.media_type === "video"
    ? `<video class="hover-video" poster="${escapeHtml(thumb)}" data-src="/api/media?path=${encodeURIComponent(member.path)}" muted loop playsinline preload="none"></video>`
    : `<img class="thumb-image ${member.media_type === "gif" ? "hover-gif" : ""}" src="${escapeHtml(thumb)}" ${member.media_type === "gif" ? `data-thumbnail="${escapeHtml(thumb)}" data-src="/api/media?path=${encodeURIComponent(member.path)}"` : ""} alt="Preview of ${escapeHtml(basename(member.path))}" loading="lazy" decoding="async" />`;
  const mediaBadge = ["video", "gif"].includes(member.media_type)
    ? '<span class="video-preview-badge" aria-hidden="true">▶</span>'
    : "";
  const hasDims = Number.isFinite(member.width) && Number.isFinite(member.height)
    && member.width > 0 && member.height > 0;
  const previewDims = hasDims
    ? ` data-preview-width="${member.width}" data-preview-height="${member.height}"`
    : "";
  const lockControls = Boolean(state.scanning || isDeleted);
  return `
    <article class="card copy-row ${isSelected ? "selected" : "keep"} ${isDeleted ? "deleted" : ""} ${index === state.memberFocus ? "focused" : ""}" data-path="${escapeHtml(member.path)}" data-index="${index}">
      <button class="copy-thumb thumb-wrap" data-path="${escapeHtml(member.path)}" data-index="${lightboxIndex}"${previewDims} type="button" aria-label="Open ${escapeHtml(basename(member.path))} in the comparison view">
        ${mediaPreview}${mediaBadge}
      </button>
      <div class="copy-info">
        <div class="name" title="${escapeHtml(member.path)}">${escapeHtml(basename(member.path))}</div>
        <div class="copy-chips"${chips ? "" : " hidden"}>${chips}</div>
        <div class="path" title="${escapeHtml(member.path)}">${escapeHtml(member.path)}</div>
        <div class="card-meta">
          <span>${formatBytes(member.size)}</span>
          <span>${dims}</span>
          <span title="Modified">${escapeHtml(formatMtime(member.mtime))}</span>
        </div>
      </div>
      <div class="copy-side">
        <button class="copy-keep" data-path="${escapeHtml(member.path)}" type="button" aria-pressed="${soleSurvivor}" title="${keepTitle}"${lockControls ? " disabled" : ""}>
          <span class="copy-radio" aria-hidden="true"></span>Keep
        </button>
        <label class="copy-remove" title="Mark this copy for removal">
          <input type="checkbox" class="sel-cb" data-path="${escapeHtml(member.path)}" ${isSelected ? "checked" : ""} ${member.protected || lockControls ? "disabled" : ""} />
          Remove
        </label>
        <button class="linkish reveal" data-path="${escapeHtml(member.path)}" type="button">Reveal</button>
      </div>
    </article>`;
}

function updateExactMetaAndSummary(g) {
  const members = g.members || [];
  const selected = new Set(g.selected_for_removal || []);
  const deleted = new Set(g.deleted_paths || []);
  const selectedBytes = members
    .filter((member) => selected.has(member.path) && !deleted.has(member.path))
    .reduce((sum, member) => sum + (member.size || 0), 0);
  const keeperReason = g.cleanup?.keeper_reason
    ? ` · keep: ${g.cleanup.keeper_reason}`
    : "";
  $("detailMeta").textContent =
    `${formatBytes(selectedBytes)} marked for removal (${formatBytes(g.reclaimable_bytes)} reclaimable) · ${members.length} byte-identical copies · pick the one to keep, the rest are marked for removal${keeperReason}`;

  const selectionBase = `${selected.size} of ${members.length} selected for removal`;
  $("groupSelectionSummary").textContent =
    selected.size > 0 && !state.touchedGroups.has(g.id)
      ? `Suggested selection — ${selectionBase} · adjust freely`
      : selectionBase;
}

function syncExactLightboxItems(g) {
  const deleted = new Set(g.deleted_paths || []);
  state.lightboxItems = (g.members || [])
    .filter((member) => !deleted.has(member.path))
    .map((member) => lightboxItemFor(member, g));
}

function syncCopyRow(row, g, path, survivors = null, keeper = null) {
  if (!row || !g) return;
  const members = g.members || [];
  const member = members.find((item) => item.path === path);
  if (!member) return;
  const selected = new Set(g.selected_for_removal || []);
  const deleted = new Set(g.deleted_paths || []);
  const liveSurvivors = survivors || new Set(
    members.filter((item) => !selected.has(item.path) && !deleted.has(item.path)).map((item) => item.path),
  );
  const resolvedKeeper = keeper || members.find((item) => item.path === g.suggested_keep) || members[0];
  const isSelected = selected.has(path);
  const isDeleted = deleted.has(path);
  const isSurvivor = !isSelected && !isDeleted;
  const soleSurvivor = isSurvivor && liveSurvivors.size === 1;

  row.classList.toggle("selected", isSelected);
  row.classList.toggle("keep", !isSelected);
  row.classList.toggle("deleted", isDeleted);

  let chipsEl = row.querySelector(".copy-chips");
  const chipsMarkup = copyChipsHtml(g, member, isSelected, isSurvivor, resolvedKeeper);
  if (!chipsEl) {
    chipsEl = document.createElement("div");
    chipsEl.className = "copy-chips";
    row.querySelector(".copy-info .name")?.after(chipsEl);
  }
  chipsEl.innerHTML = chipsMarkup;
  chipsEl.hidden = !chipsMarkup;

  const keepBtn = row.querySelector(".copy-keep");
  if (keepBtn) {
    keepBtn.setAttribute("aria-pressed", String(soleSurvivor));
    keepBtn.title = soleSurvivor
      ? "The surviving copy"
      : "Keep only this copy — every other copy is marked for removal";
    keepBtn.disabled = Boolean(state.scanning || isDeleted);
  }

  const cb = row.querySelector(".sel-cb");
  if (cb) {
    cb.checked = isSelected;
    cb.disabled = Boolean(member.protected || state.scanning || isDeleted);
  }
}

function syncExactSelectionState(g) {
  if (!g) return;
  const members = g.members || [];
  const selected = new Set(g.selected_for_removal || []);
  const deleted = new Set(g.deleted_paths || []);
  const survivors = new Set(
    members.filter((member) => !selected.has(member.path) && !deleted.has(member.path)).map((member) => member.path),
  );
  const keeper = members.find((member) => member.path === g.suggested_keep) || members[0];

  syncExactLightboxItems(g);
  updateExactMetaAndSummary(g);
  $("members").querySelectorAll(".copy-row[data-path]").forEach((row) => {
    syncCopyRow(row, g, row.dataset.path, survivors, keeper);
  });
}

// —— Serialized selection updates per group ——
const pendingExactSelection = new Map();
const exactSelectionInFlight = new Set();

function applyExactSelection(g, wanted) {
  if (state.scanning || state.actionBusy) {
    toast("Selections are locked while a scan or file action runs", "error");
    return false;
  }
  const selectedSet = new Set(wanted);
  const ordered = (g.members || [])
    .filter((member) => !member.protected && selectedSet.has(member.path))
    .map((member) => member.path);
  const optimistic = { ...g, selected_for_removal: ordered };
  state.selectToken += 1;
  markGroupTouched(g.id);
  patchGroup(optimistic);
  syncExactSelectionState(optimistic);
  if (selectionFiltersActive() || !updateGroupListItem(optimistic)) {
    scheduleRender({ groupList: true });
  } else {
    applyResultControls();
  }
  scheduleRender({ selection: true });
  pendingExactSelection.set(g.id, ordered);
  flushExactSelection(g.id);
  return true;
}

async function flushExactSelection(id) {
  if (exactSelectionInFlight.has(id)) return;
  exactSelectionInFlight.add(id);
  try {
    while (pendingExactSelection.has(id)) {
      const selected = pendingExactSelection.get(id);
      pendingExactSelection.delete(id);
      const updated = await api("/api/selection", {
        method: "POST",
        body: JSON.stringify({
          group_id: id,
          selected,
          scan_id: state.scanId,
        }),
      });
      if (!pendingExactSelection.has(id)) {
        patchGroup(updated);
        if (currentGroup()?.id === id && $("members").querySelector(".exact-review")) {
          syncExactSelectionState(updated);
        }
        if (selectionFiltersActive() || !updateGroupListItem(updated)) {
          scheduleRender({ groupList: true });
        } else {
          applyResultControls();
        }
        scheduleRender({ selection: true });
      }
    }
  } catch (error) {
    pendingExactSelection.delete(id);
    toast(error.message || "Could not update selection", "error");
    loadGroups().catch(() => {});
  } finally {
    exactSelectionInFlight.delete(id);
  }
}

function unloadVideos(container) {
  container.querySelectorAll("video").forEach((video) => {
    video.pause();
    video.removeAttribute("src");
    video.load();
  });
}

function wireExactDelegation(root) {
  root.addEventListener("click", async (event) => {
    const g = currentGroup();
    if (!g || g.kind !== "exact") return;

    const keepBtn = event.target.closest(".copy-keep");
    if (keepBtn) {
      const keepPath = keepBtn.dataset.path;
      const deleted = new Set(g.deleted_paths || []);
      const picks = (g.members || [])
        .filter((member) => !member.protected && !deleted.has(member.path) && member.path !== keepPath)
        .map((member) => member.path);
      applyExactSelection(g, picks);
      return;
    }

    const revealBtn = event.target.closest(".reveal");
    if (revealBtn) {
      event.stopPropagation();
      try {
        await api(`/api/reveal?path=${encodeURIComponent(revealBtn.dataset.path)}&open=1`);
      } catch (error) {
        toast(error.message, "error");
      }
      return;
    }

    const thumbBtn = event.target.closest(".copy-thumb");
    if (thumbBtn) {
      const row = thumbBtn.closest(".copy-row");
      const path = row?.dataset.path;
      const lightboxIndex = state.lightboxItems.findIndex((item) => item.path === path);
      state.memberFocus = Number(row?.dataset.index || 0);
      openLightbox(lightboxIndex >= 0 ? lightboxIndex : 0);
      return;
    }

    if (event.target.closest(".exact-preview")) {
      const keeperPath = g.suggested_keep || g.members?.[0]?.path;
      const index = state.lightboxItems.findIndex((item) => item.path === keeperPath);
      openLightbox(index >= 0 ? index : 0);
      return;
    }

    const row = event.target.closest(".copy-row");
    if (row && !event.target.closest("input, button, label, a")) {
      state.memberFocus = Number(row.dataset.index);
      root.querySelectorAll(".copy-row").forEach((other) => other.classList.remove("focused"));
      row.classList.add("focused");
    }
  });

  // Capturing change listener also catches Space's non-bubbling synthetic
  // change event from keyboard.js.
  root.addEventListener("change", (event) => {
    const cb = event.target.closest?.(".sel-cb");
    if (!cb) return;
    const g = currentGroup();
    if (!g || g.kind !== "exact") return;
    if (state.scanning || state.actionBusy) {
      cb.checked = !cb.checked;
      toast("Selections are locked while a scan or file action runs", "error");
      return;
    }
    const next = new Set(g.selected_for_removal || []);
    if (cb.checked) next.add(cb.dataset.path);
    else next.delete(cb.dataset.path);
    if (!applyExactSelection(g, next)) {
      cb.checked = !cb.checked;
    }
  }, true);

  root.addEventListener("pointerover", (event) => {
    const wrap = event.target.closest?.(".thumb-wrap");
    if (!wrap || wrap.contains(event.relatedTarget)) return;
    const video = wrap.querySelector(".hover-video");
    if (video) {
      video.muted = true;
      if (!video.src) video.src = video.dataset.src;
      video.play().catch(() => {
        /* The static poster remains when the browser cannot play this codec. */
      });
    }
    const gif = wrap.querySelector(".hover-gif");
    if (gif && gif.dataset.src) gif.src = gif.dataset.src;
  });

  root.addEventListener("pointerout", (event) => {
    const wrap = event.target.closest?.(".thumb-wrap");
    if (!wrap || wrap.contains(event.relatedTarget)) return;
    const video = wrap.querySelector(".hover-video");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    const gif = wrap.querySelector(".hover-gif");
    if (gif && gif.dataset.thumbnail) gif.src = gif.dataset.thumbnail;
  });

  root.addEventListener("loadedmetadata", (event) => {
    const video = event.target;
    if (video?.classList?.contains("hover-video")) {
      setPreviewAspectRatio(video.closest(".thumb-wrap"), video.videoWidth, video.videoHeight);
    }
  }, true);
}

function renderExactReview(g) {
  const box = $("members");
  unloadVideos(box);
  box.classList.remove("triage-grid");
  for (const id of ["memberPagination", "memberPaginationBottom", "memberSort", "memberModified", "memberPeople"]) {
    const el = $(id);
    if (el) el.hidden = true;
  }

  const members = g.members || [];
  const selected = new Set(g.selected_for_removal || []);
  const deleted = new Set(g.deleted_paths || []);
  const survivors = new Set(
    members.filter((member) => !selected.has(member.path) && !deleted.has(member.path)).map((member) => member.path),
  );
  const keeper = members.find((member) => member.path === g.suggested_keep) || members[0];

  syncExactLightboxItems(g);
  updateExactMetaAndSummary(g);

  const lightboxIndexByPath = new Map(
    state.lightboxItems.map((item, lightboxIndex) => [item.path, lightboxIndex]),
  );

  const preview = keeper
    ? `<button class="exact-preview" type="button" aria-label="Open the identical preview in the comparison view">
         <img src="${escapeHtml(previewUrl(keeper))}" alt="Preview shared by every copy" decoding="async" />
       </button>`
    : "";
  const keeperReason = g.cleanup?.keeper_reason
    ? `<span class="keeper-reason">Suggested keeper: ${escapeHtml(g.cleanup.keeper_reason)}</span>`
    : "";
  box.innerHTML = `
    <div class="exact-review">
      <div class="exact-banner">
        ${preview}
        <div class="exact-banner-copy">
          <strong>${members.length} identical copies</strong>
          <span class="muted">Byte-identical SHA-256 match — only the locations differ. Choose the copy that stays; the rest move to Trash when you run the action below.</span>
          ${keeperReason}
        </div>
      </div>
      <div class="copy-list">
        ${members.map((member, index) => copyRowHtml(g, member, index, survivors, keeper, lightboxIndexByPath.get(member.path) ?? index)).join("")}
      </div>
    </div>`;

  const reviewRoot = box.querySelector(".exact-review");
  if (reviewRoot) wireExactDelegation(reviewRoot);

  box.querySelectorAll(".copy-thumb").forEach((thumb) => {
    setPreviewAspectRatio(thumb, thumb.dataset.previewWidth, thumb.dataset.previewHeight);
  });
}

export { renderExactReview, syncCopyRow, syncExactSelectionState };
