// Crop review: images framed by black bars or screenshot interface. Each card shows the picture with
// the suggested crop drawn on it — the bars that would go are shaded — and
// one click applies it. The file is rewritten in place and the untouched
// original goes to the Trash, so every crop has an Undo. Skip declines a
// suggestion; it is remembered so the same file is not suggested again.

import { api } from "./api.js";
import { updateGroupListItem } from "./groups.js";
import { openLightbox } from "./lightbox.js";
import { currentGroup, patchGroup } from "./model.js";
import { scheduleRender } from "./render.js";
import { state } from "./state.js";
import { confirmModal } from "./modal.js";
import {
  $, basename, escapeHtml, formatBytes, hideToast, noteMediaVersion, sleep, toast, withMediaVersion,
} from "./util.js";

const busy = new Set();

function thumbUrl(member) {
  return withMediaVersion(`/api/thumbnail?path=${encodeURIComponent(member.path)}`, member.path);
}

function pendingMembers(g) {
  return (g.members || []).filter((member) => member.crop_state === "pending" && !member.protected);
}

// Interface bars are named for what they are on screen; other edges are bars.
const CHROME_NAMES = { top: "status bar", bottom: "navigation bar" };

function barsLabel(crop) {
  if (!crop) return "";
  const chrome = new Set(crop.chrome || []);
  const sides = [
    ["top", crop.top],
    ["bottom", crop.height - crop.bottom],
    ["left", crop.left],
    ["right", crop.width - crop.right],
  ].filter(([, px]) => px > 0);
  return sides
    .map(([side, px]) => (chrome.has(side) ? `${px}px ${CHROME_NAMES[side]} (${side})` : `${px}px ${side}`))
    .join(" · ");
}

function cropKind(crop) {
  const chrome = crop.chrome || [];
  if (!chrome.length) return "Black bars";
  const blackSides = (crop.left > 0) || (crop.right < crop.width)
    || (crop.top > 0 && !chrome.includes("top"))
    || (crop.bottom < crop.height && !chrome.includes("bottom"));
  return blackSides ? "Screenshot bars and black bars" : "Screenshot bars";
}

function evidenceFor(member) {
  const crop = member.crop;
  if (!crop) return "No crop suggestion";
  const removed = Math.round((crop.removed_fraction || 0) * 100);
  const size = `${crop.width}×${crop.height} → ${crop.crop_width}×${crop.crop_height}`;
  if (member.crop_state === "cropped") {
    return `Cropped to ${crop.crop_width}×${crop.crop_height} · original is in the Trash`;
  }
  if (member.crop_state === "skipped") {
    return `Skipped · ${size} would remove ${removed}% of the frame`;
  }
  return `${cropKind(crop)}: ${barsLabel(crop)} · ${size} (removes ${removed}%)`;
}

function cardHtml(g, member, index) {
  const name = basename(member.path);
  const state_ = member.crop_state || "pending";
  const crop = member.crop;
  // A cropped file is shown as it is now; otherwise the suggestion is drawn
  // over the original.
  const showsCrop = Boolean(crop) && state_ !== "cropped";
  const width = state_ === "cropped" ? crop?.crop_width : crop?.width;
  const height = state_ === "cropped" ? crop?.crop_height : crop?.height;
  const badge = state_ === "cropped"
    ? '<span class="crop-badge done">Cropped</span>'
    : state_ === "skipped"
      ? '<span class="crop-badge skipped">Skipped</span>'
      : "";
  const primary = member.protected
    ? '<span class="protected-label">Protected library</span>'
    : state_ === "cropped"
      ? `<button class="btn ghost crop-undo" data-path="${escapeHtml(member.path)}" type="button" ${member.crop_undo_available ? "" : "disabled title=\"The original's place in the Trash is unknown\""}>Undo crop</button>`
      : state_ === "skipped"
        ? `<button class="btn ghost crop-unskip" data-path="${escapeHtml(member.path)}" type="button" title="Suggest this crop again">Reconsider</button>`
        : `<button class="btn primary crop-apply" data-path="${escapeHtml(member.path)}" type="button" title="Crop now — the original goes to the Trash (c)">Crop</button>
           <button class="btn ghost crop-skip" data-path="${escapeHtml(member.path)}" type="button" title="Leave this image as it is and stop suggesting it (x)">Skip</button>`;
  return `
    <article class="card crop-card ${state_} ${index === state.memberFocus ? "focused" : ""}" data-path="${escapeHtml(member.path)}" data-index="${index}">
      <button class="thumb-wrap crop-thumb" type="button" data-path="${escapeHtml(member.path)}" aria-label="Open ${escapeHtml(name)} in the lightbox">
        <span class="crop-frame" data-width="${width || ""}" data-height="${height || ""}">
          <img class="crop-image" src="${thumbUrl(member)}" alt="Preview of ${escapeHtml(name)}" loading="lazy" decoding="async" />
          ${showsCrop ? '<span class="crop-box" aria-hidden="true"></span>' : ""}
        </span>
        ${badge}
      </button>
      <div class="card-body">
        <div class="name" title="${escapeHtml(member.path)}">${escapeHtml(name)}</div>
        <div class="path" title="${escapeHtml(member.path)}">${escapeHtml(member.path)}</div>
        <div class="card-meta">
          <span>${formatBytes(member.size)}</span>
          <span>${member.width && member.height ? `${member.width}×${member.height}` : "—"}</span>
        </div>
        <div class="evidence">${escapeHtml(evidenceFor(member))}</div>
        <div class="card-actions crop-actions">
          ${primary}
          <button class="linkish reveal" data-path="${escapeHtml(member.path)}" type="button">Reveal</button>
        </div>
      </div>
    </article>`;
}

// Inline style attributes are blocked by the page's CSP; geometry is applied
// through the CSSOM instead.
function layoutCard(card, member) {
  const frame = card.querySelector(".crop-frame");
  const width = Number(frame?.dataset.width);
  const height = Number(frame?.dataset.height);
  if (frame && width > 0 && height > 0) {
    frame.style.setProperty("--crop-aspect", `${width} / ${height}`);
    frame.classList.toggle("portrait", height > width);
  }
  const box = card.querySelector(".crop-box");
  const crop = member.crop;
  if (box && crop) {
    box.style.left = `${(crop.left / crop.width) * 100}%`;
    box.style.top = `${(crop.top / crop.height) * 100}%`;
    box.style.width = `${(crop.crop_width / crop.width) * 100}%`;
    box.style.height = `${(crop.crop_height / crop.height) * 100}%`;
  }
}

function updateCropMeta(g) {
  const members = g.members || [];
  const cropped = members.filter((m) => m.crop_state === "cropped").length;
  const skipped = members.filter((m) => m.crop_state === "skipped").length;
  const pending = pendingMembers(g).length;
  $("detailMeta").textContent =
    `${pending} to review · ${cropped} cropped · ${skipped} skipped · originals go to the Trash, so every crop can be undone`;
  const all = $("btnCropAll");
  all.hidden = pending === 0;
  all.textContent = `Crop all ${pending} remaining`;
}

function renderCropReview(g) {
  const box = $("members");
  box.classList.remove("triage-grid", "focus-review");
  box.classList.add("crop-grid");
  $("memberPagination").hidden = true;
  $("memberPaginationBottom").hidden = true;
  const members = g.members || [];
  state.memberFocus = Math.max(0, Math.min(members.length - 1, state.memberFocus));
  state.lightboxItems = members.map((member) => ({
    path: member.path,
    mediaType: member.media_type,
    kind: g.kind,
    size: member.size,
    width: member.width,
    height: member.height,
    mtime: member.mtime,
  }));
  updateCropMeta(g);
  if (!members.length) {
    box.innerHTML = '<div class="triage-empty">No images to crop.</div>';
    return;
  }
  box.innerHTML = members.map((member, index) => cardHtml(g, member, index)).join("");
  box.querySelectorAll(".crop-card").forEach((card) => {
    const member = members[Number(card.dataset.index)];
    layoutCard(card, member);
    card.addEventListener("click", (event) => {
      if (event.target.closest("button, a, input, label")) return;
      focusCard(Number(card.dataset.index));
    });
  });
  box.querySelectorAll(".crop-thumb").forEach((thumb) => {
    thumb.addEventListener("click", () => {
      const index = Number(thumb.closest(".crop-card").dataset.index);
      state.memberFocus = index;
      openLightbox(index);
    });
  });
  box.querySelectorAll(".crop-apply").forEach((button) => {
    button.addEventListener("click", () => applyCrop(currentGroup(), button.dataset.path));
  });
  box.querySelectorAll(".crop-skip").forEach((button) => {
    button.addEventListener("click", () => skipCrop(currentGroup(), button.dataset.path, true));
  });
  box.querySelectorAll(".crop-unskip").forEach((button) => {
    button.addEventListener("click", () => skipCrop(currentGroup(), button.dataset.path, false));
  });
  box.querySelectorAll(".crop-undo").forEach((button) => {
    button.addEventListener("click", () => undoCrop(currentGroup(), button.dataset.path));
  });
  box.querySelectorAll(".reveal").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        await api(`/api/reveal?path=${encodeURIComponent(button.dataset.path)}&open=1`);
      } catch (error) {
        toast(error.message, "error");
      }
    });
  });
}

function focusCard(index) {
  const cards = [...$("members").querySelectorAll(".crop-card")];
  if (!cards.length) return;
  state.memberFocus = Math.max(0, Math.min(cards.length - 1, index));
  cards.forEach((card) => card.classList.remove("focused"));
  const card = cards[state.memberFocus];
  card.classList.add("focused");
  card.scrollIntoView({ block: "nearest" });
}

function applyUpdate(updated) {
  patchGroup(updated);
  for (const member of updated.members || []) {
    // Size changes with every crop and undo, so it versions the thumbnail.
    noteMediaVersion(member.path, `${member.size}-${member.crop_state}`);
  }
  if (currentGroup()?.id === updated.id) renderCropReview(updated);
  updateGroupListItem(updated);
  scheduleRender({ groupList: true, selection: true });
}

async function post(path, body) {
  let lastError;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    try {
      return await api(path, { method: "POST", body: JSON.stringify(body) });
    } catch (error) {
      lastError = error;
      if (error.status !== 409 || !/locked during active work/i.test(error.message || "")) throw error;
      await sleep(80 * (attempt + 1));
    }
  }
  throw lastError;
}

// After a decision the next undecided image takes focus, so c / x can run
// down the pile without the mouse.
function advanceFocus(updated, path) {
  const members = updated.members || [];
  const from = members.findIndex((member) => member.path === path);
  const next = members.findIndex((member, index) => index > from && member.crop_state === "pending");
  const fallback = members.findIndex((member) => member.crop_state === "pending");
  focusCard(next >= 0 ? next : fallback >= 0 ? fallback : from);
}

// The sticky "Cropped … · Undo" toast goes stale once that crop is undone or
// a bulk crop reports its own result, so it is dismissed rather than queued behind.
let cropUndoToastPath = null;

const cropUndoMessage = (path) => `Cropped ${basename(path)} · original moved to Trash`;

function dismissCropUndoToast(path = null) {
  const shown = cropUndoToastPath;
  if (shown === null || (path !== null && path !== shown)) return;
  cropUndoToastPath = null;
  // Only hide it while it is still the toast on screen.
  if (!$("toast").hidden && $("toastMessage")?.textContent === cropUndoMessage(shown)) hideToast();
}

async function applyCrop(g, path) {
  if (!g || !path || busy.has(path)) return;
  busy.add(path);
  try {
    const updated = await post("/api/crop/apply", { group_id: g.id, path, scan_id: state.scanId });
    applyUpdate(updated);
    advanceFocus(updated, path);
    dismissCropUndoToast();
    toast(cropUndoMessage(path), "ok", {
      actionLabel: "Undo",
      onAction: () => undoCrop(currentGroup() || updated, path),
    });
    cropUndoToastPath = path;
  } catch (error) {
    toast(error.message || "Could not crop that image", "error");
  } finally {
    busy.delete(path);
  }
}

async function skipCrop(g, path, skip) {
  if (!g || !path || busy.has(path)) return;
  busy.add(path);
  try {
    const updated = await post("/api/crop/skip", { group_id: g.id, path, scan_id: state.scanId, skip });
    applyUpdate(updated);
    if (skip) advanceFocus(updated, path);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    busy.delete(path);
  }
}

async function undoCrop(g, path) {
  if (!g || !path) return;
  while (busy.has(path)) await sleep(40);
  busy.add(path);
  try {
    const updated = await post("/api/crop/undo", { group_id: g.id, path, scan_id: state.scanId });
    applyUpdate(updated);
    dismissCropUndoToast(path);
    toast(`Restored the original ${basename(path)}`, "ok");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    busy.delete(path);
  }
}

async function cropAllRemaining() {
  const g = currentGroup();
  if (!g || g.kind !== "crop") return;
  const pending = pendingMembers(g);
  if (!pending.length) return;
  const ok = await confirmModal({
    title: `Crop ${pending.length} image${pending.length === 1 ? "" : "s"}?`,
    body: `<p>Each image is cropped to its suggested frame and rewritten in place. The originals go to the Trash, and each crop can be undone from its card.</p><p class="muted">Skipped and already cropped images are left alone.</p>`,
    confirmLabel: `Crop ${pending.length}`,
    danger: false,
  });
  if (ok !== true) return;
  try {
    $("btnCropAll").disabled = true;
    const updated = await post("/api/crop/apply-all", { group_id: g.id, scan_id: state.scanId });
    applyUpdate(updated);
    dismissCropUndoToast();
    const failed = (updated.failures || []).length;
    toast(
      `Cropped ${updated.cropped_count} image${updated.cropped_count === 1 ? "" : "s"}`
        + (failed ? ` · ${failed} could not be cropped (${updated.failures[0].error})` : ""),
      failed ? "error" : "ok",
    );
  } catch (error) {
    toast(error.message, "error");
  } finally {
    $("btnCropAll").disabled = false;
  }
}

$("btnCropAll")?.addEventListener("click", cropAllRemaining);

// Keyboard: c crops the focused image, x skips it, ←/→ move between cards.
function handleCropKey(event) {
  const g = currentGroup();
  if (!g || g.kind !== "crop") return false;
  const members = g.members || [];
  const member = members[state.memberFocus];
  if (event.key === "c" && member?.crop_state === "pending") {
    applyCrop(g, member.path);
    return true;
  }
  if (event.key === "x" && member?.crop_state === "pending") {
    skipCrop(g, member.path, true);
    return true;
  }
  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    focusCard(state.memberFocus + (event.key === "ArrowRight" ? 1 : -1));
    return true;
  }
  return false;
}

export { renderCropReview, handleCropKey, updateCropMeta };
