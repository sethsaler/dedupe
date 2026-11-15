// The large crop editor: one file's zone on a picture as big as the window.
// A card's thumbnail is a few hundred pixels across, so a pixel of pointer
// travel there is five or ten of the picture; here the same zone is drawn on
// the lightbox-sized preview, with a number field for each side, a button
// that keeps a side whole, and the level switch. It edits the very zone the
// card shows — the server stays the store, through crop.js — and Crop or
// Skip moves straight on to the next file still to review.

import {
  CROP_HANDLES, CROP_LEVELS, applyCrop, bindZone, chooseCropLevel, coversFrame, deriveBox,
  draggedBox, effectiveBox, evidenceFor, hiddenZones, levelSwitchHtml, localAdjustments,
  nudgedBox, placeZone, queueZone, renderCropReview, resetCropZone, sameEdges, skipCrop,
  snapEdges, toggleCropZone, zoneBox,
} from "./crop.js";
import { currentGroup } from "./model.js";
import { $, basename, isOverlayOpen, promoteToastTopLayer, syncAppInert, trapTabKey, withMediaVersion } from "./util.js";

const SIDES = ["top", "right", "bottom", "left"];
// The smallest zone the fields can make, matching a drag.
const MIN_ZONE_PX = 8;

let editorPath = null;
let built = false;
let working = false;
let previousFocus = null;
let aspectMode = "free";
const previewOnly = new Set();

const editor = () => $("cropEditor");
const fieldFor = (side) => $(`ce${side[0].toUpperCase()}${side.slice(1)}`);

function cropEditorOpen() {
  return isOverlayOpen(editor());
}

function editorMember(g = currentGroup()) {
  if (!editorPath || g?.kind !== "crop") return null;
  return (g.members || []).find((member) => member.path === editorPath) || null;
}

function editable(member) {
  return Boolean(member) && member.crop_state === "pending" && !member.protected
    && Boolean(zoneBox(member));
}

function reviewable(g) {
  return (g?.members || []).filter(editable);
}

function previewUrl(path) {
  return withMediaVersion(`/api/thumbnail?path=${encodeURIComponent(path)}&variant=preview`, path);
}

// Pixels the zone removes from each side of the picture.
function removedFrom(box) {
  return {
    top: box.top,
    right: box.width - box.right,
    bottom: box.height - box.bottom,
    left: box.left,
  };
}

// The picture as large as the stage allows, at its own proportions.
function layoutFrame(box) {
  const stage = $("ceStage");
  const frame = $("ceFrame");
  const room = stage.getBoundingClientRect();
  if (!box || room.width <= 0 || room.height <= 0) return;
  const scale = Math.min(room.width / box.width, room.height / box.height);
  frame.style.width = `${Math.max(1, Math.floor(box.width * scale))}px`;
  frame.style.height = `${Math.max(1, Math.floor(box.height * scale))}px`;
}

function targetAspectRatio(mode, frame) {
  if (mode === "original" && frame?.height > 0) return frame.width / frame.height;
  if (mode === "16:9") return 16 / 9;
  if (mode === "4:3") return 4 / 3;
  if (mode === "1:1") return 1;
  return null;
}

function syncAspectButtons() {
  editor()?.querySelectorAll("#ceAspect [data-aspect]").forEach((button) => {
    const chosen = button.dataset.aspect === aspectMode;
    button.classList.toggle("chosen", chosen);
    button.setAttribute("aria-pressed", String(chosen));
  });
}

function aspectBoxFromSize(member, frame, anchor, handle, w, h, ratio) {
  const minW = Math.min(MIN_ZONE_PX, frame.width);
  const minH = Math.min(MIN_ZONE_PX, frame.height);
  const even = member?.media_type === "video" ? 2 : 1;
  const roundStep = (v) => Math.max(even, Math.round(v / even) * even);

  let left = anchor.left;
  let right = anchor.right;
  let top = anchor.top;
  let bottom = anchor.bottom;

  const maxW = handle.includes("w")
    ? right
    : handle.includes("e")
      ? frame.width - left
      : frame.width;
  const maxH = handle.includes("n")
    ? bottom
    : handle.includes("s")
      ? frame.height - top
      : frame.height;

  let cw = Math.max(minW, Math.min(maxW, w));
  let ch = cw / ratio;
  if (ch > maxH) {
    ch = maxH;
    cw = ch * ratio;
  }
  if (ch < minH) {
    ch = Math.min(maxH, minH);
    cw = Math.min(maxW, ch * ratio);
  }
  cw = roundStep(cw);
  ch = Math.max(minH, Math.min(maxH, roundStep(cw / ratio)));
  cw = Math.max(minW, Math.min(maxW, roundStep(ch * ratio)));

  if (handle.includes("w")) left = right - cw;
  else if (handle.includes("e")) right = left + cw;
  else {
    const cx = (left + right) / 2;
    left = Math.max(0, Math.min(frame.width - cw, Math.round(cx - cw / 2)));
    right = left + cw;
  }

  if (handle.includes("n")) top = bottom - ch;
  else if (handle.includes("s")) bottom = top + ch;
  else {
    const cy = (top + bottom) / 2;
    top = Math.max(0, Math.min(frame.height - ch, Math.round(cy - ch / 2)));
    bottom = top + ch;
  }

  return deriveBox(snapEdges(member, { left, top, right, bottom }, frame), frame);
}

function dragEditorBox(member, start, handle, dx, dy, snap) {
  const ratio = targetAspectRatio(aspectMode, start);
  if (!ratio || handle === "move") {
    return draggedBox(member, start, handle, dx, dy, snap);
  }
  const raw = draggedBox(member, start, handle, dx, dy, snap);
  if (handle === "e" || handle === "w") {
    const w = raw.crop_width;
    return aspectBoxFromSize(member, start, start, handle, w, Math.round(w / ratio), ratio);
  }
  if (handle === "n" || handle === "s") {
    const h = raw.crop_height;
    return aspectBoxFromSize(member, start, start, handle, Math.round(h * ratio), h, ratio);
  }
  const dw = Math.abs(raw.crop_width - start.crop_width) / Math.max(1, start.crop_width);
  const dh = Math.abs(raw.crop_height - start.crop_height) / Math.max(1, start.crop_height);
  const w = dw >= dh ? raw.crop_width : Math.round(raw.crop_height * ratio);
  const h = dw >= dh ? Math.round(raw.crop_width / ratio) : raw.crop_height;
  return aspectBoxFromSize(member, start, start, handle, w, h, ratio);
}

function applyAspectPreset(mode) {
  aspectMode = mode;
  syncAspectButtons();
  const member = editorMember();
  const box = member && zoneBox(member);
  if (!box || working || mode === "free") return;
  const ratio = targetAspectRatio(mode, box);
  if (!ratio) return;
  let w = box.crop_width;
  let h = Math.round(w / ratio);
  if (h > box.crop_height) {
    h = box.crop_height;
    w = Math.round(h * ratio);
  }
  const next = aspectBoxFromSize(member, box, box, "center", w, h, ratio);
  if (!sameEdges(next, box)) {
    previewOnly.delete(member.path);
    queueZone(member.path, next);
    paint(member, next);
    renderCropReview(currentGroup());
  }
}

function paint(member, box) {
  placeZone($("ceFrame"), $("ceBox"), box);
  const hidden = hiddenZones.has(member.path);
  $("ceFrame").classList.toggle("crop-zone-hidden", hidden);
  const toggleBtn = $("ceToggleZone");
  if (toggleBtn) {
    toggleBtn.textContent = hidden ? "Show zone" : "Hide zone";
    toggleBtn.setAttribute("aria-pressed", String(!hidden));
  }
  syncAspectButtons();
  const removed = removedFrom(box);
  for (const side of SIDES) {
    const field = fieldFor(side);
    if (document.activeElement !== field) field.value = String(removed[side]);
    field.max = String(side === "top" || side === "bottom" ? box.height : box.width);
    $("cropEditor").querySelector(`.ce-keep[data-side="${side}"]`).disabled = removed[side] === 0;
  }
  const whole = coversFrame(box);
  $("ceSummary").textContent = whole
    ? "Nothing is cut yet — drag an edge of the zone inward, or type the pixels to remove from a side."
    : evidenceFor(member, box);
  $("ceApply").disabled = whole || working;
  // Reset has something to restore when the zone is not the shown level's.
  const suggested = member.crop_suggested;
  $("ceReset").disabled = suggested ? sameEdges(box, suggested) : whole;
}

function draw() {
  const g = currentGroup();
  const member = editorMember(g);
  if (!member) return;
  const box = zoneBox(member);
  const name = basename(member.path);
  $("ceName").textContent = name;
  $("ceName").title = member.path;
  const order = reviewable(g);
  const at = order.indexOf(member);
  $("ceCount").textContent = `${at + 1} of ${order.length} to review`;
  $("cePrev").disabled = at <= 0;
  $("ceNext").disabled = at < 0 || at >= order.length - 1;
  const image = $("ceImage");
  const url = previewUrl(member.path);
  if (image.getAttribute("src") !== url) {
    image.setAttribute("src", url);
    image.alt = `Preview of ${name}`;
  }
  $("ceBox").setAttribute(
    "aria-label",
    `Crop zone for ${name}. Drag to move it, drag an edge or corner to resize it. Arrow keys move `
      + "it, Shift+arrow keys move its bottom and right edges, Alt+arrow keys its top and left edges.",
  );
  const levels = $("ceLevels");
  const html = levelSwitchHtml(member, name);
  if (levels.dataset.html !== html) {
    levels.dataset.html = html;
    levels.innerHTML = html;
    levels.querySelectorAll(".crop-level-btn").forEach((button) => {
      button.addEventListener("click", () => {
        chooseCropLevel(currentGroup(), editorPath, button.dataset.level);
      });
    });
  }
  $("ceApply").textContent = working ? "Cropping…" : "Crop";
  $("ceSkip").disabled = working;
  layoutFrame(box);
  // A zone mid-drag is ahead of any reply or poll that lands meanwhile.
  if (!editor().classList.contains("crop-dragging")) paint(member, box);
}

function computeSideBox(member, start, side, pixels) {
  const clamp = (value, low, high) => Math.max(low, Math.min(high, Math.round(value)));
  const amount = Number.isFinite(pixels) ? pixels : 0;
  const edges = { left: start.left, top: start.top, right: start.right, bottom: start.bottom };
  const minW = Math.min(MIN_ZONE_PX, start.width);
  const minH = Math.min(MIN_ZONE_PX, start.height);
  if (side === "top") edges.top = clamp(amount, 0, start.bottom - minH);
  if (side === "bottom") edges.bottom = clamp(start.height - amount, start.top + minH, start.height);
  if (side === "left") edges.left = clamp(amount, 0, start.right - minW);
  if (side === "right") edges.right = clamp(start.width - amount, start.left + minW, start.width);
  return deriveBox(snapEdges(member, edges, start), start);
}

// Live local preview while typing in a side field (no save until change/blur).
function previewSide(side, pixels) {
  const member = editorMember();
  const start = member && zoneBox(member);
  if (!start || working || !Number.isFinite(pixels)) return;
  const next = computeSideBox(member, start, side, pixels);
  previewOnly.add(member.path);
  localAdjustments.set(member.path, next);
  paint(member, next);
  const card = document.querySelector(`.crop-card[data-path="${CSS.escape(member.path)}"]`);
  if (card) placeZone(card.querySelector(".crop-frame"), card.querySelector(".crop-box"), next);
}

// Set how much one side removes, in picture pixels (typed, or 0 from Keep).
function setSide(side, pixels) {
  const member = editorMember();
  const start = member && zoneBox(member);
  if (!start || working) return;
  const hadPreview = previewOnly.delete(member.path);
  const next = computeSideBox(member, start, side, pixels);
  const committed = effectiveBox(member);
  if (hadPreview || !sameEdges(next, start) || (committed && !sameEdges(next, committed))) {
    queueZone(member.path, next);
  }
  // Repaint either way: a clamped or snapped number replaces the one typed.
  const field = fieldFor(side);
  if (document.activeElement === field) field.value = String(removedFrom(next)[side]);
  renderCropReview(currentGroup());
}

// One arrow key in the editor: a screen pixel's worth of picture, so the
// zone can be placed more finely here than on a card.
function nudge(event) {
  const member = editorMember();
  const start = member && zoneBox(member);
  if (!start || working) return false;
  const shown = $("ceFrame").getBoundingClientRect().width || start.width;
  const even = member.media_type === "video" ? 2 : 1;
  const step = Math.max(even, Math.round(start.width / shown));
  const next = nudgedBox(member, start, event, step, step);
  if (!sameEdges(next, start)) {
    queueZone(member.path, next);
    renderCropReview(currentGroup());
  }
  return true;
}

function step(delta) {
  const g = currentGroup();
  const order = reviewable(g);
  const at = order.indexOf(editorMember(g));
  const next = order[at + delta];
  if (!next) return;
  aspectMode = "free";
  editorPath = next.path;
  draw();
}

async function decide(crop) {
  const g = currentGroup();
  const path = editorPath;
  if (!g || !path || working) return;
  working = true;
  draw();
  try {
    if (crop) await applyCrop(g, path);
    else await skipCrop(g, path, true);
  } finally {
    working = false;
    // The decision's own render already moved the editor on (or closed it).
    if (cropEditorOpen()) draw();
  }
}

function build() {
  if (built) return;
  built = true;
  const box = $("ceBox");
  const thirds = document.createElement("span");
  thirds.className = "crop-thirds";
  thirds.setAttribute("aria-hidden", "true");
  const badge = document.createElement("span");
  badge.className = "crop-live-badge";
  badge.setAttribute("aria-hidden", "true");
  box.append(thirds, badge);
  for (const handle of CROP_HANDLES) {
    const grip = document.createElement("span");
    grip.className = `crop-handle ${handle}`;
    grip.dataset.handle = handle;
    grip.setAttribute("aria-hidden", "true");
    box.append(grip);
  }
  bindZone(box, {
    path: () => editorPath,
    frame: () => $("ceFrame"),
    focus: () => {},
    paint,
    dragEl: editor(),
    blocked: (path) => hiddenZones.has(path),
    dragBox: dragEditorBox,
    nudge,
    done: () => renderCropReview(currentGroup()),
  });
  for (const side of SIDES) {
    fieldFor(side).addEventListener("input", (event) => {
      previewSide(side, Number.parseInt(event.target.value, 10));
    });
    fieldFor(side).addEventListener("change", (event) => {
      setSide(side, Number.parseInt(event.target.value, 10));
    });
  }
  editor().querySelectorAll(".ce-keep").forEach((button) => {
    button.addEventListener("click", () => setSide(button.dataset.side, 0));
  });
  editor().querySelectorAll("#ceAspect [data-aspect]").forEach((button) => {
    button.addEventListener("click", () => applyAspectPreset(button.dataset.aspect));
  });
  $("ceApply").addEventListener("click", () => decide(true));
  $("ceSkip").addEventListener("click", () => decide(false));
  $("ceReset").addEventListener("click", () => {
    aspectMode = "free";
    previewOnly.delete(editorPath);
    resetCropZone(editorPath);
  });
  $("ceToggleZone")?.addEventListener("click", () => {
    if (!editorPath) return;
    toggleCropZone(editorPath);
    draw();
  });
  $("cePrev").addEventListener("click", () => step(-1));
  $("ceNext").addEventListener("click", () => step(1));
  $("ceClose").addEventListener("click", closeCropEditor);
  window.addEventListener("resize", () => {
    if (cropEditorOpen()) draw();
  });
}

function openCropEditor(path) {
  const g = currentGroup();
  const member = g?.kind === "crop" ? (g.members || []).find((m) => m.path === path) : null;
  if (!editable(member)) return;
  build();
  const wasOpen = cropEditorOpen();
  if (!wasOpen) previousFocus = document.activeElement;
  aspectMode = "free";
  editorPath = path;
  if (!wasOpen) editor().showPopover?.();
  syncAppInert();
  promoteToastTopLayer();
  draw();
  $("ceBox").focus({ preventScroll: true });
}

function closeCropEditor() {
  if (!cropEditorOpen()) return;
  const path = editorPath;
  editor().hidePopover?.();
  syncAppInert();
  editorPath = null;
  $("ceImage").removeAttribute("src");
  // Back to the zone that was being edited, where the page still shows it.
  const zone = path
    ? document.querySelector(`.crop-card[data-path="${CSS.escape(path)}"] .crop-box.editable`)
    : null;
  const target = zone || (previousFocus?.isConnected ? previousFocus : null);
  previousFocus = null;
  target?.focus?.({ preventScroll: true });
}

// Called with every render of the Crop review: follow the file being
// edited, move on when it has been decided, close when nothing is left.
function syncCropEditor(g) {
  if (!cropEditorOpen()) return;
  if (!g || g.kind !== "crop") {
    closeCropEditor();
    return;
  }
  const members = g.members || [];
  const at = members.findIndex((member) => member.path === editorPath);
  if (at < 0 || !editable(members[at])) {
    const next = at < 0
      ? null
      : members.slice(at + 1).find(editable) || members.slice(0, at).reverse().find(editable);
    if (!next) {
      closeCropEditor();
      return;
    }
    editorPath = next.path;
  }
  draw();
}

// Keys while the editor is open; the page's own key map stands aside.
function handleCropEditorKey(event) {
  if (!cropEditorOpen()) return false;
  if (event.key === "Tab") {
    if (!trapTabKey(editor(), event)) event.preventDefault();
    return true;
  }
  if (event.key === "Escape") {
    closeCropEditor();
    event.preventDefault();
    return true;
  }
  const typing = event.target?.tagName === "INPUT";
  if (typing) {
    if (event.key === "Enter") {
      if (event.isComposing || event.keyCode === 229) return true;
      event.target.blur(); // commits the number through its change event
      $("ceBox").focus({ preventScroll: true });
      event.preventDefault();
    }
    return true; // digits and arrows belong to the field
  }
  if (event.metaKey || event.ctrlKey) return true;
  if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
    nudge(event);
    event.preventDefault();
    return true;
  }
  if (event.altKey) return true;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (key === "c") decide(true);
  else if (key === "x") decide(false);
  else if (key === "r") {
    aspectMode = "free";
    previewOnly.delete(editorPath);
    resetCropZone(editorPath);
  } else if (key === "v" && editorPath) {
    toggleCropZone(editorPath);
    draw();
  } else if (key === "]" || key === "PageDown") step(1);
  else if (key === "[" || key === "PageUp") step(-1);
  else if (["1", "2", "3"].includes(key)) {
    chooseCropLevel(currentGroup(), editorPath, CROP_LEVELS[Number(key) - 1]);
  } else return true; // swallow: nothing behind the editor should react
  event.preventDefault();
  return true;
}

export { closeCropEditor, cropEditorOpen, handleCropEditorKey, openCropEditor, syncCropEditor };
