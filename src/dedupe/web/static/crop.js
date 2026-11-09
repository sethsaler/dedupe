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

function hasBlackBars(crop) {
  const chrome = crop.chrome || [];
  return (crop.left > 0) || (crop.right < crop.width)
    || (crop.top > 0 && !chrome.includes("top"))
    || (crop.bottom < crop.height && !chrome.includes("bottom"));
}

// Each removed edge with its rows; when interface and black bars mix, each
// edge says which it is.
function barsLabel(crop) {
  if (!crop) return "";
  const chrome = new Set(crop.chrome || []);
  const mixed = chrome.size > 0 && hasBlackBars(crop);
  const sides = [
    ["top", crop.top],
    ["bottom", crop.height - crop.bottom],
    ["left", crop.left],
    ["right", crop.width - crop.right],
  ].filter(([, px]) => px > 0);
  return sides
    .map(([side, px]) => {
      if (!mixed) return `${px}px ${side}`;
      return chrome.has(side) ? `${px}px interface (${side})` : `${px}px black (${side})`;
    })
    .join(" · ");
}

function cropKind(crop) {
  const chrome = crop.chrome || [];
  if (!chrome.length) return "Black bars";
  return hasBlackBars(crop) ? "Screenshot interface and black bars" : "Screenshot interface";
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
  // The frame always has the original's proportions, so applying or undoing
  // a crop never resizes the card: a cropped file is drawn inside the box it
  // was cut to and the bars it lost are left empty; otherwise the suggestion
  // is drawn over the original.
  const showsCrop = Boolean(crop) && state_ !== "cropped";
  const width = crop?.width || member.width;
  const height = crop?.height || member.height;
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
        <div class="evidence" title="${escapeHtml(evidenceFor(member))}">${escapeHtml(evidenceFor(member))}</div>
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
  const crop = member.crop;
  if (!crop) return;
  const box = card.querySelector(".crop-box");
  if (box) placeInFrame(box, crop);
  const image = card.querySelector(".crop-image");
  if (image && member.crop_state === "cropped") placeInFrame(image, crop);
}

// Position an element over the crop rectangle, in the original's coordinates.
function placeInFrame(element, crop) {
  element.style.left = `${(crop.left / crop.width) * 100}%`;
  element.style.top = `${(crop.top / crop.height) * 100}%`;
  element.style.width = `${(crop.crop_width / crop.width) * 100}%`;
  element.style.height = `${(crop.crop_height / crop.height) * 100}%`;
}

// What a card shows; a card is rebuilt only when this changes.
function cardSignature(member) {
  const crop = member.crop;
  return [
    member.crop_state || "pending",
    member.protected ? 1 : 0,
    member.crop_undo_available ? 1 : 0,
    member.size,
    crop ? [crop.left, crop.top, crop.right, crop.bottom, crop.width, crop.height].join(",") : "",
  ].join("|");
}

function buildCard(g, member, index) {
  const template = document.createElement("template");
  template.innerHTML = cardHtml(g, member, index).trim();
  const card = template.content.firstElementChild;
  card.dataset.signature = cardSignature(member);
  layoutCard(card, member);
  bindCard(card);
  return card;
}

// Swap a changed card in place. The previous picture stays on screen until
// the new one has loaded, so the card never flashes empty.
function replaceCard(previous, card) {
  const fresh = card.querySelector(".crop-image");
  const shown = previous.querySelector(".crop-image");
  const url = fresh?.getAttribute("src");
  if (fresh && shown && shown.complete && shown.naturalWidth && url !== shown.getAttribute("src")) {
    const placeholder = shown.cloneNode(false);
    placeholder.removeAttribute("loading");
    fresh.replaceWith(placeholder);
    const loader = new Image();
    const swap = () => {
      if (placeholder.isConnected) placeholder.replaceWith(fresh);
    };
    loader.addEventListener("load", swap, { once: true });
    loader.addEventListener("error", swap, { once: true });
    loader.src = url;
  }
  previous.replaceWith(card);
}

function bindCard(card) {
  card.addEventListener("click", (event) => {
    if (event.target.closest("button, a, input, label")) return;
    focusCard(Number(card.dataset.index));
  });
  card.querySelector(".crop-thumb")?.addEventListener("click", () => {
    state.memberFocus = Number(card.dataset.index);
    openLightbox(state.memberFocus);
  });
  card.querySelector(".crop-apply")?.addEventListener("click", (event) => {
    applyCrop(currentGroup(), event.currentTarget.dataset.path);
  });
  card.querySelector(".crop-skip")?.addEventListener("click", (event) => {
    skipCrop(currentGroup(), event.currentTarget.dataset.path, true);
  });
  card.querySelector(".crop-unskip")?.addEventListener("click", (event) => {
    skipCrop(currentGroup(), event.currentTarget.dataset.path, false);
  });
  card.querySelector(".crop-undo")?.addEventListener("click", (event) => {
    undoCrop(currentGroup(), event.currentTarget.dataset.path);
  });
  card.querySelector(".reveal")?.addEventListener("click", async (event) => {
    event.stopPropagation();
    try {
      await api(`/api/reveal?path=${encodeURIComponent(event.currentTarget.dataset.path)}&open=1`);
    } catch (error) {
      toast(error.message, "error");
    }
  });
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
    delete box.dataset.cropGroup;
    return;
  }
  // Re-rendering the same review (after a crop, a skip, an undo, a status
  // poll) patches only the cards that changed. Rebuilding the whole grid
  // would reload every picture and let the page jump while they arrive.
  const cards = [...box.querySelectorAll(":scope > .crop-card")];
  const samePile = box.dataset.cropGroup === g.id
    && cards.length === members.length
    && cards.every((card, index) => card.dataset.path === members[index].path);
  if (samePile) {
    cards.forEach((card, index) => {
      const member = members[index];
      if (card.dataset.signature !== cardSignature(member)) {
        replaceCard(card, buildCard(g, member, index));
      }
    });
    markFocused();
    return;
  }
  box.replaceChildren(...members.map((member, index) => buildCard(g, member, index)));
  box.dataset.cropGroup = g.id;
}

function markFocused() {
  $("members").querySelectorAll(":scope > .crop-card").forEach((card) => {
    card.classList.toggle("focused", Number(card.dataset.index) === state.memberFocus);
  });
}

// Only keyboard moves scroll, and only when the card is out of view: a click
// must never move the grid out from under the pointer.
function focusCard(index, { reveal = false } = {}) {
  const cards = [...$("members").querySelectorAll(":scope > .crop-card")];
  if (!cards.length) return;
  state.memberFocus = Math.max(0, Math.min(cards.length - 1, index));
  markFocused();
  if (reveal) revealCard(cards[state.memberFocus]);
}

function revealCard(card) {
  const rect = card.getBoundingClientRect();
  const view = card.closest("main")?.getBoundingClientRect()
    ?? { top: 0, bottom: window.innerHeight };
  const thumb = card.querySelector(".crop-thumb")?.getBoundingClientRect() ?? rect;
  // Enough of the picture is showing to work on it: leave the scroll alone.
  if (thumb.top >= view.top && thumb.top + thumb.height / 2 <= view.bottom) return;
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
function advanceFocus(updated, path, { reveal = false } = {}) {
  const members = updated.members || [];
  const from = members.findIndex((member) => member.path === path);
  const next = members.findIndex((member, index) => index > from && member.crop_state === "pending");
  const fallback = members.findIndex((member) => member.crop_state === "pending");
  focusCard(next >= 0 ? next : fallback >= 0 ? fallback : from, { reveal });
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

async function applyCrop(g, path, { fromKeyboard = false } = {}) {
  if (!g || !path || busy.has(path)) return;
  busy.add(path);
  try {
    const updated = await post("/api/crop/apply", { group_id: g.id, path, scan_id: state.scanId });
    applyUpdate(updated);
    advanceFocus(updated, path, { reveal: fromKeyboard });
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

async function skipCrop(g, path, skip, { fromKeyboard = false } = {}) {
  if (!g || !path || busy.has(path)) return;
  busy.add(path);
  try {
    const updated = await post("/api/crop/skip", { group_id: g.id, path, scan_id: state.scanId, skip });
    applyUpdate(updated);
    if (skip) advanceFocus(updated, path, { reveal: fromKeyboard });
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
    applyCrop(g, member.path, { fromKeyboard: true });
    return true;
  }
  if (event.key === "x" && member?.crop_state === "pending") {
    skipCrop(g, member.path, true, { fromKeyboard: true });
    return true;
  }
  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    focusCard(state.memberFocus + (event.key === "ArrowRight" ? 1 : -1), { reveal: true });
    return true;
  }
  return false;
}

export { renderCropReview, handleCropKey, updateCropMeta };
