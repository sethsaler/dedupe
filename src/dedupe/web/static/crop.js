// Crop review: images and videos framed by black bars or screenshot interface.
// Each card shows the picture (a video's poster frame) with the suggested crop
// drawn on it — the bars that would go are shaded — and one click applies it.
// The zone itself can be clicked to select it, dragged to move it, and
// resized from any edge or corner; the moved zone is saved with the review
// and is the crop a Crop (or Crop all) click cuts. Reset zone puts the
// detector's suggestion back.
// A card's zone can be toggled off to see the untouched picture (and every
// zone at once from the header), then back on to see what a crop removes.
// The file is rewritten in place (videos are re-encoded) and the untouched
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

// Zones the user has dragged, by path, shown until the server's copy of the
// group (which carries the same box as its effective crop) replaces them.
// The server is the store; this only keeps a drag ahead of its save.
const localAdjustments = new Map();
const adjustInFlight = new Map();
const adjustTimers = new Map();
let adjustmentScan = null;

function syncAdjustmentScan() {
  if (adjustmentScan === state.scanId) return;
  adjustmentScan = state.scanId;
  for (const timer of adjustTimers.values()) clearTimeout(timer);
  adjustTimers.clear();
  localAdjustments.clear();
  adjustInFlight.clear();
  document.querySelectorAll(".crop-card").forEach((card) => { delete card.dataset.signature; });
}
// A pointer press on a zone must not fall through to the thumbnail's click
// (which opens the lightbox): the press stamps its card's path here and the
// thumbnail handler swallows one click that follows it.
const suppressThumbClick = new Map();
const CROP_HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
// The smallest zone a drag can make, in picture pixels per side.
const MIN_CROP_PX = 8;

// Paths whose suggested crop zone is currently hidden, so the untouched
// picture can be compared against what a crop would remove. Visual only.
const hiddenZones = new Set();

const zoneToggleTitle = (hidden) => (hidden
  ? "Show the suggested crop zone — the shaded part is what a crop removes (v)"
  : "Hide the suggested crop zone to see the full picture (v)");
// Skip-reason panels left open, by path: annotating rebuilds the card, and
// the panel (with its note field) must not collapse out from under the user.
const openWhy = new Set();

function thumbUrl(member) {
  return withMediaVersion(`/api/thumbnail?path=${encodeURIComponent(member.path)}`, member.path);
}

function pendingMembers(g) {
  return (g.members || []).filter((member) => member.crop_state === "pending" && !member.protected);
}

function edgesOf(box) {
  return { left: box.left, top: box.top, right: box.right, bottom: box.bottom };
}

function sameEdges(a, b) {
  return Boolean(a && b)
    && a.left === b.left && a.top === b.top && a.right === b.right && a.bottom === b.bottom;
}

// A full crop box (the shape the server sends) from four edges of a frame.
function deriveBox(edges, frame) {
  const cropWidth = edges.right - edges.left;
  const cropHeight = edges.bottom - edges.top;
  const total = frame.width * frame.height;
  return {
    ...edges,
    width: frame.width,
    height: frame.height,
    crop_width: cropWidth,
    crop_height: cropHeight,
    removed_fraction: total > 0 ? Math.round((1 - (cropWidth * cropHeight) / total) * 10000) / 10000 : 0,
    chrome: (frame.chrome || []).filter(
      (edge) => (edge === "top" && edges.top > 0) || (edge === "bottom" && edges.bottom < frame.height),
    ),
  };
}

// The zone on screen for a member: an unsaved drag wins over the server's
// effective crop, which already folds in any saved adjustment.
function effectiveBox(member) {
  return localAdjustments.get(member?.path) || member?.crop || null;
}

function isAdjusted(member) {
  if (!member) return false;
  const local = localAdjustments.get(member.path);
  if (local && member.crop_suggested) return !sameEdges(local, member.crop_suggested);
  return Boolean(member.crop_adjusted);
}

// Videos re-encode to 4:2:0, whose crop offsets and sizes are even; snap
// outward exactly as the server does so the zone on screen is the zone cut.
function snapEdges(member, edges, frame) {
  if (member?.media_type !== "video") return edges;
  const out = { ...edges };
  out.left -= out.left % 2;
  out.top -= out.top % 2;
  if ((out.right - out.left) % 2) out.right = out.right < frame.width ? out.right + 1 : out.right - 1;
  if ((out.bottom - out.top) % 2) out.bottom = out.bottom < frame.height ? out.bottom + 1 : out.bottom - 1;
  return out;
}

// The zone after dragging ``start`` by (dx, dy) picture pixels: "move"
// slides it inside the frame; a handle name drags its named edge(s).
function draggedBox(member, start, handle, dx, dy) {
  const frame = start;
  const minW = Math.max(1, Math.min(MIN_CROP_PX, frame.width));
  const minH = Math.max(1, Math.min(MIN_CROP_PX, frame.height));
  const clamp = (value, low, high) => Math.max(low, Math.min(high, Math.round(value)));
  let edges = edgesOf(start);
  if (handle === "move") {
    const shiftX = clamp(dx, -start.left, frame.width - start.right);
    const shiftY = clamp(dy, -start.top, frame.height - start.bottom);
    edges = {
      left: start.left + shiftX, right: start.right + shiftX,
      top: start.top + shiftY, bottom: start.bottom + shiftY,
    };
  } else {
    if (handle.includes("w")) edges.left = clamp(start.left + dx, 0, start.right - minW);
    if (handle.includes("e")) edges.right = clamp(start.right + dx, start.left + minW, frame.width);
    if (handle.includes("n")) edges.top = clamp(start.top + dy, 0, start.bottom - minH);
    if (handle.includes("s")) edges.bottom = clamp(start.bottom + dy, start.top + minH, frame.height);
  }
  return deriveBox(snapEdges(member, edges, frame), frame);
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

function evidenceFor(member, crop = effectiveBox(member)) {
  if (!crop) return "No crop suggestion";
  const removed = Math.round((crop.removed_fraction || 0) * 100);
  const size = `${crop.width}×${crop.height} → ${crop.crop_width}×${crop.crop_height}`;
  if (member.crop_state === "cropped") {
    return `Cropped to ${crop.crop_width}×${crop.crop_height} · original is in the Trash`;
  }
  if (member.crop_state === "skipped") {
    return `Skipped · ${size} would remove ${removed}% of the frame`;
  }
  // A video crop re-encodes the whole clip; say so before the click, not after.
  const reencode = member.media_type === "video" ? " · cropping re-encodes the video" : "";
  const adjusted = member.crop_suggested && !sameEdges(crop, member.crop_suggested)
    ? "Adjusted zone · "
    : "";
  return `${adjusted}${cropKind(crop)}: ${barsLabel(crop)} · ${size} (removes ${removed}%)${reencode}`;
}

// Why was this suggestion skipped? Optional and collapsed: the skip lands
// with one click either way, and a reason picked afterwards is logged with
// the rejected box for future detector tuning.
function skipWhyHtml(g, member) {
  if ((member.crop_state || "pending") !== "skipped") return "";
  const reasons = g.crop_skip_reasons || [];
  const chosen = reasons.find((r) => r.id === member.crop_skip_reason);
  const summary = chosen ? `Skipped: ${chosen.label}` : "Why was this skipped?";
  const chips = reasons.map((r) => `
      <button class="chip crop-skip-reason ${r.id === member.crop_skip_reason ? "chosen" : ""}"
        type="button" data-reason="${escapeHtml(r.id)}"
        aria-pressed="${r.id === member.crop_skip_reason}">${escapeHtml(r.label)}</button>`).join("");
  return `
    <details class="crop-skip-why" ${openWhy.has(member.path) ? "open" : ""}>
      <summary>${escapeHtml(summary)}</summary>
      <div class="crop-skip-chips">${chips}</div>
      <input class="crop-skip-note" type="text" maxlength="500"
        placeholder="Optional note (Enter to save)"
        value="${escapeHtml(member.crop_skip_note || "")}" />
      ${chosen || member.crop_skip_note ? '<button class="linkish crop-skip-clear" type="button">Clear reason</button>' : ""}
    </details>`;
}

const cropBoxLabel = (name) => `Crop zone for ${name}. Drag to move it, drag an edge or corner to `
  + "resize it. With the zone focused, arrow keys move it and Shift+arrow keys resize it.";

function cropBoxHtml(member, name, editable) {
  if (!editable) return '<span class="crop-box" aria-hidden="true"></span>';
  const handles = CROP_HANDLES
    .map((handle) => `<span class="crop-handle ${handle}" data-handle="${handle}" aria-hidden="true"></span>`)
    .join("");
  return `<span class="crop-box editable" tabindex="0" role="group" aria-label="${escapeHtml(cropBoxLabel(name))}" title="Drag to move · drag an edge or corner to resize">${handles}</span>`;
}

function cardHtml(g, member, index) {
  const name = basename(member.path);
  const state_ = member.crop_state || "pending";
  const crop = effectiveBox(member);
  // The frame always has the original's proportions, so applying or undoing
  // a crop never resizes the card: a cropped file is drawn inside the box it
  // was cut to and the bars it lost are left empty; otherwise the suggestion
  // is drawn over the original.
  const showsCrop = Boolean(crop) && state_ !== "cropped";
  const editable = showsCrop && !member.protected;
  const adjusted = editable && isAdjusted(member);
  const zoneHidden = showsCrop && hiddenZones.has(member.path);
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
           <button class="btn ghost crop-skip" data-path="${escapeHtml(member.path)}" type="button" title="Leave this file as it is and stop suggesting it (x)">Skip</button>`;
  return `
    <article class="card crop-card ${state_} ${zoneHidden ? "crop-zone-hidden" : ""} ${index === state.memberFocus ? "focused" : ""}" data-path="${escapeHtml(member.path)}" data-index="${index}">
      <button class="thumb-wrap crop-thumb" type="button" data-path="${escapeHtml(member.path)}" aria-label="Open ${escapeHtml(name)} in the lightbox">
        <span class="crop-frame" data-width="${width || ""}" data-height="${height || ""}">
          <img class="crop-image" src="${thumbUrl(member)}" alt="Preview of ${escapeHtml(name)}" loading="lazy" decoding="async" />
          ${showsCrop ? cropBoxHtml(member, name, editable) : ""}
        </span>
        ${badge}
      </button>
      <div class="card-body">
        <div class="name" title="${escapeHtml(member.path)}">${escapeHtml(name)}</div>
        <div class="path" title="${escapeHtml(member.path)}">${escapeHtml(member.path)}</div>
        <div class="card-meta">
          <span>${formatBytes(member.size)}</span>
          <span>${member.width && member.height ? `${member.width}×${member.height}` : "—"}</span>
          ${member.media_type === "video" ? `<span>▶ Video${member.duration != null ? ` · ${Number(member.duration).toFixed(1)}s` : ""}</span>` : ""}
        </div>
        <div class="evidence" title="${escapeHtml(evidenceFor(member))}">${escapeHtml(evidenceFor(member))}</div>
        <div class="card-actions crop-actions">
          ${primary}
          ${adjusted ? `<button class="btn ghost crop-reset" data-path="${escapeHtml(member.path)}" type="button" title="Put the suggested crop zone back">Reset zone</button>` : ""}
          ${showsCrop ? `<button class="btn ghost crop-zone-toggle" data-path="${escapeHtml(member.path)}" type="button" aria-pressed="${!zoneHidden}" title="${zoneToggleTitle(zoneHidden)}">${zoneHidden ? "Show zone" : "Hide zone"}</button>` : ""}
          <button class="linkish reveal" data-path="${escapeHtml(member.path)}" type="button">Reveal</button>
        </div>
        ${skipWhyHtml(g, member)}
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
  const crop = effectiveBox(member);
  if (!crop) return;
  const box = card.querySelector(".crop-box");
  if (box) placeInFrame(box, crop);
  const image = card.querySelector(".crop-image");
  if (image && member.crop_state === "cropped") placeInFrame(image, member.crop || crop);
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
  const crop = effectiveBox(member);
  const suggested = member.crop_suggested;
  return [
    member.crop_state || "pending",
    member.protected ? 1 : 0,
    member.crop_undo_available ? 1 : 0,
    member.size,
    member.crop_skip_reason || "",
    member.crop_skip_note || "",
    hiddenZones.has(member.path) ? 1 : 0,
    isAdjusted(member) ? 1 : 0,
    crop ? [crop.left, crop.top, crop.right, crop.bottom, crop.width, crop.height].join(",") : "",
    suggested ? [suggested.left, suggested.top, suggested.right, suggested.bottom].join(",") : "",
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
  const keepZoneFocus = previous.contains(document.activeElement)
    && document.activeElement?.classList?.contains("crop-box");
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
  if (keepZoneFocus) card.querySelector(".crop-box.editable")?.focus({ preventScroll: true });
}

function bindCard(card) {
  card.addEventListener("click", (event) => {
    if (event.target.closest("button, a, input, label")) return;
    focusCard(Number(card.dataset.index));
  });
  card.querySelector(".crop-thumb")?.addEventListener("click", () => {
    // A click that began on the crop zone selects or drags the zone; only a
    // click on the picture around it opens the lightbox.
    const stamped = suppressThumbClick.get(card.dataset.path);
    if (stamped && Date.now() - stamped < 800) {
      suppressThumbClick.delete(card.dataset.path);
      return;
    }
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
  card.querySelector(".crop-zone-toggle")?.addEventListener("click", (event) => {
    toggleCropZone(event.currentTarget.dataset.path);
  });
  card.querySelector(".crop-reset")?.addEventListener("click", (event) => {
    resetCropZone(event.currentTarget.dataset.path);
  });
  const boxEl = card.querySelector(".crop-box.editable");
  if (boxEl) bindCropBox(card, boxEl);
  const path = card.dataset.path;
  card.querySelector(".crop-skip-why")?.addEventListener("toggle", (event) => {
    if (event.currentTarget.open) openWhy.add(path);
    else openWhy.delete(path);
  });
  card.querySelectorAll(".crop-skip-reason").forEach((chip) => {
    chip.addEventListener("click", () => {
      const member = (currentGroup()?.members || []).find((m) => m.path === path);
      const reason = member?.crop_skip_reason === chip.dataset.reason ? null : chip.dataset.reason;
      annotateSkip(currentGroup(), path, reason, card.querySelector(".crop-skip-note")?.value || null);
    });
  });
  card.querySelector(".crop-skip-note")?.addEventListener("change", (event) => {
    const member = (currentGroup()?.members || []).find((m) => m.path === path);
    annotateSkip(currentGroup(), path, member?.crop_skip_reason || null, event.target.value || null);
  });
  card.querySelector(".crop-skip-clear")?.addEventListener("click", () => {
    annotateSkip(currentGroup(), path, null, null);
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

function syncEvidence(card, member, box) {
  const evidence = card.querySelector(".evidence");
  if (!evidence) return;
  const text = evidenceFor(member, box);
  evidence.textContent = text;
  evidence.title = text;
}

// Click, drag, and resize for one card's zone. The zone lives inside the
// thumbnail button, so a press on it claims the gesture (and stamps the
// suppression the thumbnail's click handler checks) instead of enlarging.
function bindCropBox(card, boxEl) {
  boxEl.addEventListener("focus", () => {
    state.memberFocus = Number(card.dataset.index);
    markFocused();
  });
  boxEl.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    const path = card.dataset.path;
    const member = (currentGroup()?.members || []).find((m) => m.path === path);
    const start = member && effectiveBox(member);
    const frame = card.querySelector(".crop-frame");
    if (!member || !start || !frame || busy.has(path) || hiddenZones.has(path)) return;
    event.preventDefault();
    event.stopPropagation();
    suppressThumbClick.set(path, Date.now());
    state.memberFocus = Number(card.dataset.index);
    markFocused();
    try {
      boxEl.setPointerCapture(event.pointerId);
    } catch {
      /* capture is a nicety; window-level moves below still track */
    }
    try {
      boxEl.focus({ preventScroll: true });
    } catch {
      /* a span inside a button may refuse focus in some browsers */
    }
    const handle = event.target.closest("[data-handle]")?.dataset.handle || "move";
    const rect = frame.getBoundingClientRect();
    const startX = event.clientX;
    const startY = event.clientY;
    let current = start;
    card.classList.add("crop-dragging");
    boxEl.dataset.dragHandle = handle;

    const onMove = (moveEvent) => {
      if (rect.width <= 0 || rect.height <= 0) return;
      const dx = ((moveEvent.clientX - startX) / rect.width) * start.width;
      const dy = ((moveEvent.clientY - startY) / rect.height) * start.height;
      const next = draggedBox(member, start, handle, dx, dy);
      if (sameEdges(next, current)) return;
      current = next;
      localAdjustments.set(path, current);
      placeInFrame(boxEl, current);
      syncEvidence(card, member, current);
    };
    const onEnd = () => {
      boxEl.removeEventListener("pointermove", onMove);
      boxEl.removeEventListener("pointerup", onEnd);
      boxEl.removeEventListener("pointercancel", onEnd);
      card.classList.remove("crop-dragging");
      delete boxEl.dataset.dragHandle;
      suppressThumbClick.set(path, Date.now());
      if (sameEdges(current, start)) {
        // A plain click: the zone is selected; its card already took focus.
        if (sameEdges(current, member.crop)) localAdjustments.delete(path);
        return;
      }
      void saveAdjustment(path, current);
    };
    boxEl.addEventListener("pointermove", onMove);
    boxEl.addEventListener("pointerup", onEnd);
    boxEl.addEventListener("pointercancel", onEnd);
  });
  // A zone can be Tabbed to; Enter/Space there must not also press the
  // enclosing thumbnail button.
  boxEl.addEventListener("click", (event) => {
    event.stopPropagation();
    suppressThumbClick.set(card.dataset.path, Date.now());
  });
}

// Store a dragged zone (or, with null, the suggestion again). The reply is
// the whole updated group, applied like any other crop decision.
async function saveAdjustment(path, box, g = currentGroup(), scanId = state.scanId) {
  syncAdjustmentScan();
  if (!g || !path || state.scanId !== scanId) return false;
  const timer = adjustTimers.get(path);
  if (timer) clearTimeout(timer);
  adjustTimers.delete(path);
  // Serialize writes for this file: ignoring an older reply alone would still
  // let an older request overwrite the newer box on the server.
  const previous = adjustInFlight.get(path);
  const run = (async () => {
    if (previous) await previous;
    if (state.scanId !== scanId) return false;
    try {
      const updated = await post("/api/crop/adjust", {
        group_id: g.id, path, scan_id: scanId,
        box: box ? edgesOf(box) : null,
      });
      if (state.scanId !== scanId) return false;
      if (localAdjustments.get(path) === box) localAdjustments.delete(path);
      // Replies contain an entire group snapshot. Another card may have saved
      // since that snapshot was taken; only acknowledge this request's member.
      const group = state.allGroups.find((item) => item.id === g.id)
        || state.groups.find((item) => item.id === g.id);
      if (group) {
        const focused = document.activeElement?.closest(".crop-card")?.dataset.path;
        const hadBoxFocus = document.activeElement?.classList?.contains("crop-box");
        applyUpdate({ ...group, members: group.members.map((member) => {
          const saved = updated.members.find((item) => item.path === path);
          if (member.path !== path || !saved || member.crop_state === "cropped") return member;
          return { ...member, crop: saved.crop, crop_adjusted: saved.crop_adjusted,
            crop_suggested: saved.crop_suggested };
        }) });
        if (hadBoxFocus && focused) {
          document.querySelector(`.crop-card[data-path="${CSS.escape(focused)}"] .crop-box.editable`)
            ?.focus({ preventScroll: true });
        }
      }
      return true;
    } catch (error) {
      if (state.scanId === scanId) {
        toast(error.message || "Could not save that crop zone", "error");
      }
      // Keep the unsaved box visible and available for retry.
      return false;
    }
  })();
  adjustInFlight.set(path, run);
  try {
    return await run;
  } finally {
    if (adjustInFlight.get(path) === run) adjustInFlight.delete(path);
  }
}

function resetCropZone(path) {
  if (!path || busy.has(path)) return;
  const member = currentGroup()?.members.find((item) => item.path === path);
  if (!member) return;
  // Keep reset visible even if an earlier save arrives while reset is queued.
  const suggestion = member.crop_suggested || member.crop;
  localAdjustments.set(path, suggestion);
  void saveAdjustment(path, null).then((saved) => {
    if (saved && localAdjustments.get(path) === suggestion) {
      localAdjustments.delete(path);
      if (currentGroup()) renderCropReview(currentGroup());
    }
  });
  renderCropReview(currentGroup());
}

// Arrow-key editing for a focused zone: plain arrows move it, Shift+arrows
// resize it from its bottom-right corner, both by 1% of the picture (at
// least a pixel). Saves are debounced so held keys send one zone, not fifty.
function nudgeCropZone(boxEl, event) {
  const card = boxEl.closest(".crop-card");
  const path = card?.dataset.path;
  const member = (currentGroup()?.members || []).find((m) => m.path === path);
  const start = member && effectiveBox(member);
  if (!card || !member || !start || busy.has(path)) return false;
  state.memberFocus = Number(card.dataset.index);
  markFocused();
  const stepX = Math.max(1, Math.round(start.width * 0.01));
  const stepY = Math.max(1, Math.round(start.height * 0.01));
  const dx = event.key === "ArrowRight" ? stepX : event.key === "ArrowLeft" ? -stepX : 0;
  const dy = event.key === "ArrowDown" ? stepY : event.key === "ArrowUp" ? -stepY : 0;
  const next = event.shiftKey
    ? draggedBox(member, start, dx !== 0 ? "e" : "s", dx, dy)
    : draggedBox(member, start, "move", dx, dy);
  if (!sameEdges(next, start)) {
    localAdjustments.set(path, next);
    placeInFrame(boxEl, next);
    syncEvidence(card, member, next);
    const timer = adjustTimers.get(path);
    if (timer) clearTimeout(timer);
    const group = currentGroup();
    const scanId = state.scanId;
    adjustTimers.set(path, setTimeout(() => void saveAdjustment(path, next, group, scanId), 350));
  }
  return true;
}

// Members still showing a suggested zone (cropped files have none to hide).
function zonedMembers(g) {
  return (g?.members || []).filter(
    (member) => member.crop && (member.crop_state || "pending") !== "cropped",
  );
}

// Reflect one card's zone visibility without rebuilding it (no image reload).
function syncZoneCard(card, member) {
  const hidden = hiddenZones.has(member.path);
  card.classList.toggle("crop-zone-hidden", hidden);
  const toggle = card.querySelector(".crop-zone-toggle");
  if (toggle) {
    toggle.textContent = hidden ? "Show zone" : "Hide zone";
    toggle.setAttribute("aria-pressed", String(!hidden));
    toggle.title = zoneToggleTitle(hidden);
  }
  card.dataset.signature = cardSignature(member);
}

function syncCropZonesButton(g) {
  const btn = $("btnToggleCropZones");
  if (!btn) return;
  const zoned = zonedMembers(g);
  btn.hidden = !g || g.kind !== "crop" || !zoned.length;
  if (btn.hidden) return;
  const anyVisible = zoned.some((member) => !hiddenZones.has(member.path));
  btn.textContent = anyVisible ? "Hide crop zones" : "Show crop zones";
  btn.setAttribute("aria-pressed", String(anyVisible));
}

function toggleCropZone(path) {
  if (!path) return;
  if (hiddenZones.has(path)) hiddenZones.delete(path);
  else hiddenZones.add(path);
  const member = (currentGroup()?.members || []).find((m) => m.path === path);
  const card = document.querySelector(`.crop-card[data-path="${CSS.escape(path)}"]`);
  if (card && member) syncZoneCard(card, member);
  syncCropZonesButton(currentGroup());
}

function toggleAllCropZones() {
  const g = currentGroup();
  if (!g || g.kind !== "crop") return;
  const zoned = zonedMembers(g);
  const hideAll = zoned.some((member) => !hiddenZones.has(member.path));
  for (const member of zoned) {
    if (hideAll) hiddenZones.add(member.path);
    else hiddenZones.delete(member.path);
    const card = document.querySelector(`.crop-card[data-path="${CSS.escape(member.path)}"]`);
    if (card) syncZoneCard(card, member);
  }
  syncCropZonesButton(g);
}

$("btnToggleCropZones")?.addEventListener("click", toggleAllCropZones);

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
  syncCropZonesButton(g);
}

function renderCropReview(g) {
  syncAdjustmentScan();
  const box = $("members");
  // Hidden zones belong to the review on screen; a different pile starts
  // with every zone showing.
  if (box.dataset.cropGroup && box.dataset.cropGroup !== g.id) {
    hiddenZones.clear();
  }
  // A completed crop has spent its local zone. Only the save that owns an
  // edit may otherwise clear it, even when a poll happens to echo its edges.
  for (const member of g.members || []) {
    const local = localAdjustments.get(member.path);
    if (local && member.crop_state === "cropped" && !adjustInFlight.has(member.path)) {
      localAdjustments.delete(member.path);
    }
  }
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
    box.innerHTML = '<div class="triage-empty">No files to crop.</div>';
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
      // Never rebuild a card mid-drag: its zone holds the pointer capture.
      if (card.classList.contains("crop-dragging")) return;
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

// A video crop re-encodes the whole clip and can take minutes; the button
// says so while it runs instead of looking idle.
function markCropping(path, cropping) {
  const card = document.querySelector(`.crop-card[data-path="${CSS.escape(path)}"]`);
  const button = card?.querySelector(".crop-apply");
  if (!button) return;
  button.disabled = cropping;
  button.textContent = cropping ? "Cropping…" : "Crop";
}

async function applyCrop(g, path, { fromKeyboard = false } = {}) {
  if (!g || !path || busy.has(path)) return;
  const scanId = state.scanId;
  busy.add(path);
  markCropping(path, true);
  try {
    const timer = adjustTimers.get(path);
    if (timer) clearTimeout(timer);
    adjustTimers.delete(path);
    if (adjustInFlight.has(path)) await adjustInFlight.get(path);
    if (state.scanId !== scanId || currentGroup()?.id !== g.id) return;
    const local = localAdjustments.get(path);
    if (local && !await saveAdjustment(path, local)) return;
    if (state.scanId !== scanId || currentGroup()?.id !== g.id) return;
    const member = (currentGroup()?.members || []).find((m) => m.path === path);
    const zone = localAdjustments.get(path) || (isAdjusted(member) ? member.crop : null);
    const updated = await post("/api/crop/apply", {
      group_id: g.id, path, scan_id: state.scanId, ...(zone ? { box: edgesOf(zone) } : {}),
    });
    applyUpdate(updated);
    advanceFocus(updated, path, { reveal: fromKeyboard });
    dismissCropUndoToast();
    toast(cropUndoMessage(path), "ok", {
      actionLabel: "Undo",
      onAction: () => undoCrop(currentGroup() || updated, path),
    });
    cropUndoToastPath = path;
  } catch (error) {
    toast(error.message || "Could not crop that file", "error");
  } finally {
    busy.delete(path);
    markCropping(path, false);
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

async function annotateSkip(g, path, reason, note) {
  if (!g || !path || busy.has(path)) return;
  busy.add(path);
  try {
    const updated = await post("/api/crop/annotate-skip", {
      group_id: g.id, path, scan_id: state.scanId, reason, note,
    });
    applyUpdate(updated);
  } catch (error) {
    toast(error.message || "Could not save that reason", "error");
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
  const videos = pending.filter((member) => member.media_type === "video").length;
  const ok = await confirmModal({
    title: `Crop ${pending.length} file${pending.length === 1 ? "" : "s"}?`,
    body: `<p>Each file is cropped to the zone shown on its card (dragged zones included) and rewritten in place. The originals go to the Trash, and each crop can be undone from its card.</p>${videos ? `<p>${videos} video${videos === 1 ? "" : "s"} will be re-encoded, which can take a while.</p>` : ""}<p class="muted">Skipped and already cropped files are left alone.</p>`,
    confirmLabel: `Crop ${pending.length}`,
    danger: false,
  });
  if (ok !== true) return;
  const scanId = state.scanId;
  if (pending.some((member) => busy.has(member.path))) return;
  for (const member of pending) busy.add(member.path);
  try {
    $("btnCropAll").disabled = true;
    // Freeze edits first, then drain each file's queue and save any newer
    // debounced (or previously failed) edit before applying the whole pile.
    for (const member of pending) {
      const path = member.path;
      const timer = adjustTimers.get(path);
      if (timer) clearTimeout(timer);
      adjustTimers.delete(path);
      if (adjustInFlight.has(path)) await adjustInFlight.get(path);
      if (state.scanId !== scanId || currentGroup()?.id !== g.id) return;
      const local = localAdjustments.get(path);
      if (local && !await saveAdjustment(path, local)) return;
    }
    if (state.scanId !== scanId || currentGroup()?.id !== g.id) return;
    const updated = await post("/api/crop/apply-all", { group_id: g.id, scan_id: state.scanId });
    applyUpdate(updated);
    dismissCropUndoToast();
    const failed = (updated.failures || []).length;
    toast(
      `Cropped ${updated.cropped_count} file${updated.cropped_count === 1 ? "" : "s"}`
        + (failed ? ` · ${failed} could not be cropped (${updated.failures[0].error})` : ""),
      failed ? "error" : "ok",
    );
  } catch (error) {
    toast(error.message, "error");
  } finally {
    for (const member of pending) busy.delete(member.path);
    $("btnCropAll").disabled = false;
  }
}

$("btnCropAll")?.addEventListener("click", cropAllRemaining);

// Keyboard: c crops the focused file, x skips it, v toggles its crop
// zone, ←/→ move between cards. On a focused zone itself, arrows move the
// zone and Shift+arrows resize it instead.
function handleCropKey(event) {
  const g = currentGroup();
  if (!g || g.kind !== "crop") return false;
  const members = g.members || [];
  const member = members[state.memberFocus];
  const boxEl = event.target?.closest?.(".crop-box.editable");
  if (boxEl && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
    return nudgeCropZone(boxEl, event);
  }
  if (event.key === "c" && member?.crop_state === "pending") {
    applyCrop(g, member.path, { fromKeyboard: true });
    return true;
  }
  if (event.key === "x" && member?.crop_state === "pending") {
    skipCrop(g, member.path, true, { fromKeyboard: true });
    return true;
  }
  if (event.key === "v" && member?.crop && member.crop_state !== "cropped") {
    toggleCropZone(member.path);
    return true;
  }
  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    focusCard(state.memberFocus + (event.key === "ArrowRight" ? 1 : -1), { reveal: true });
    return true;
  }
  return false;
}

export { renderCropReview, handleCropKey, updateCropMeta };
