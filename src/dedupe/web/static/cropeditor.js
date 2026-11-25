// The large crop editor: one file's zone on a picture (or scrubbed video) as
// big as the window. A card's thumbnail is a few hundred pixels across, so a
// pixel of pointer travel there is five or ten of the picture; here the same
// zone is drawn on the lightbox-sized preview, with a number field for each
// side, a button that keeps a side whole, preview mode cycling, and the level
// switch. It edits the very zone the card shows — the server stays the store,
// through crop.js — and Crop, Skip, or Trash moves straight on to the next
// file still to review.

import {
  CROP_HANDLES, CROP_LEVELS, applyCrop, bindZone, chooseCropLevel, coversFrame,
  cropWarnings, deriveBox, evidenceFor, formatAspectRatio, levelSwitchHtml, nudgedBox,
  placeInFrame, queueZone, renderCropReview, resetCropZone, sameEdges, skipCrop, snapEdges,
  syncCardZone, trashCropMember, zoneBox,
} from "./crop.js";
import { currentGroup } from "./model.js";
import {
  $, basename, isOverlayOpen, promoteToastTopLayer, showOverlay, hideOverlay, syncAppInert, trapTabKey,
  withMediaVersion,
} from "./util.js";

const SIDES = ["top", "right", "bottom", "left"];
// The smallest zone the fields can make, matching a drag.
const MIN_ZONE_PX = 8;
// Matches the 5 detector sample positions in borders.py (VIDEO_SAMPLE_FRACTIONS).
const VIDEO_SAMPLE_FRACTIONS = [0.08, 0.27, 0.5, 0.73, 0.92];
const PREVIEW_MODES = ["mask", "cropped", "original"];
const PREVIEW_LABELS = {
  mask: "Preview: Cut mask",
  cropped: "Preview: Cropped view",
  original: "Preview: Original",
};

let editorPath = null;
let built = false;
let working = false;
let previousFocus = null;
let previewMode = "mask";
let pendingSampleFrac = null;
let activeSampleFrac = VIDEO_SAMPLE_FRACTIONS[0];

const editor = () => $("cropEditor");
const fieldFor = (side) => $(`ce${side[0].toUpperCase()}${side.slice(1)}`);

function cropEditorOpen() {
  return isOverlayOpen(editor());
}

function editorMember(g = currentGroup()) {
  if (!editorPath || g?.kind !== "crop") return null;
  return (g.members || []).find((member) => member.path === editorPath) || null;
}

function editable(member, g = currentGroup()) {
  const deleted = new Set(g?.deleted_paths || []);
  return Boolean(member) && member.crop_state === "pending" && !member.protected
    && !deleted.has(member.path) && Boolean(zoneBox(member));
}

function reviewable(g) {
  return (g?.members || []).filter((member) => editable(member, g));
}

function previewUrl(path) {
  return withMediaVersion(`/api/thumbnail?path=${encodeURIComponent(path)}&variant=preview`, path);
}

function mediaUrl(path) {
  return withMediaVersion(`/api/media?path=${encodeURIComponent(path)}`, path);
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

function formatClock(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  const total = Math.round(seconds);
  const mins = Math.floor(total / 60);
  const secs = String(total % 60).padStart(2, "0");
  return `${mins}:${secs}`;
}

function applyPreviewMode() {
  const box = $("ceBox");
  const toggle = $("cePreviewToggle");
  if (!box) return;
  if (previewMode === "original") {
    box.hidden = true;
    box.style.boxShadow = "";
    box.style.borderColor = "";
  } else if (previewMode === "cropped") {
    box.hidden = false;
    box.style.boxShadow = "0 0 0 100vmax var(--lt-table, #111)";
    box.style.borderColor = "rgba(255, 255, 255, 0.25)";
  } else {
    box.hidden = false;
    box.style.boxShadow = "";
    box.style.borderColor = "";
  }
  box.dataset.previewMode = previewMode;
  if (toggle) {
    toggle.textContent = PREVIEW_LABELS[previewMode] || PREVIEW_LABELS.mask;
    toggle.setAttribute("aria-pressed", String(previewMode === "cropped"));
  }
}

function cyclePreviewMode() {
  const idx = PREVIEW_MODES.indexOf(previewMode);
  previewMode = PREVIEW_MODES[(idx + 1) % PREVIEW_MODES.length];
  applyPreviewMode();
}

function drawGuides(axes, box) {
  const guidesEl = $("ceGuides");
  if (!guidesEl) return;
  if (!axes || !box || (!axes.x?.length && !axes.y?.length)) {
    guidesEl.hidden = true;
    guidesEl.replaceChildren();
    return;
  }
  const lines = [];
  for (const xVal of new Set(axes.x || [])) {
    const line = document.createElement("span");
    line.style.position = "absolute";
    line.style.top = "0";
    line.style.bottom = "0";
    line.style.left = `${(xVal / box.width) * 100}%`;
    line.style.width = "0";
    line.style.borderLeft = "1.5px dashed var(--lt-focus, #3b82f6)";
    lines.push(line);
  }
  for (const yVal of new Set(axes.y || [])) {
    const line = document.createElement("span");
    line.style.position = "absolute";
    line.style.left = "0";
    line.style.right = "0";
    line.style.top = `${(yVal / box.height) * 100}%`;
    line.style.height = "0";
    line.style.borderTop = "1.5px dashed var(--lt-focus, #3b82f6)";
    lines.push(line);
  }
  guidesEl.replaceChildren(...lines);
  guidesEl.hidden = lines.length === 0;
}

function syncVideoStrip(video) {
  const strip = $("ceVideoStrip");
  if (!strip) return;
  const playBtn = strip.querySelector(".ce-video-play");
  if (playBtn && video) {
    const playing = !video.paused && !video.ended;
    playBtn.textContent = playing ? "⏸ Pause" : "▶ Play";
    playBtn.setAttribute("aria-pressed", String(playing));
  }
  const duration = video && Number.isFinite(video.duration) && video.duration > 0
    ? video.duration
    : null;
  strip.querySelectorAll(".ce-sample-chip").forEach((chip) => {
    const frac = Number(chip.dataset.frac);
    const active = frac === activeSampleFrac;
    chip.classList.toggle("chosen", active);
    chip.setAttribute("aria-pressed", String(active));
    const pct = `${Math.round(frac * 100)}%`;
    chip.textContent = duration ? `${pct} (${formatClock(frac * duration)})` : pct;
    chip.title = duration
      ? `Jump to ${pct} (${formatClock(frac * duration)})`
      : `Jump to ${pct} of video`;
  });
}

function stopEditorVideo() {
  const video = $("ceVideo");
  if (!video) return;
  video.pause();
  video.removeAttribute("src");
  video.removeAttribute("poster");
  delete video.dataset.path;
  video.load();
  video.hidden = true;
  pendingSampleFrac = null;
  const strip = $("ceVideoStrip");
  if (strip) {
    strip.hidden = true;
    strip.style.display = "";
  }
}

// The picture as large as the stage allows, at its own proportions.
function layoutFrame(box) {
  const stage = $("ceStage");
  const frame = $("ceFrame");
  const strip = $("ceVideoStrip");
  const room = stage.getBoundingClientRect();
  if (!box || room.width <= 0 || room.height <= 0) return;
  const stripHeight = strip && !strip.hidden ? strip.getBoundingClientRect().height + 12 : 0;
  const availHeight = Math.max(80, room.height - stripHeight);
  const scale = Math.min(room.width / box.width, availHeight / box.height);
  frame.style.width = `${Math.max(1, Math.floor(box.width * scale))}px`;
  frame.style.height = `${Math.max(1, Math.floor(box.height * scale))}px`;
}

function renderSummary(member, box, whole) {
  const summary = $("ceSummary");
  if (!summary) return;
  if (whole) {
    summary.textContent =
      "Nothing is cut yet — drag an edge of the zone inward, or type the pixels to remove from a side.";
    return;
  }
  const text = evidenceFor(member, box);
  const warnings = cropWarnings(box);
  summary.textContent = text;
  if (warnings.length) {
    const badge = document.createElement("span");
    badge.className = "crop-warn-badge";
    badge.textContent = `⚠ ${warnings.join(" · ")}`;
    badge.style.display = "inline-block";
    badge.style.marginLeft = "6px";
    badge.style.padding = "1px 6px";
    badge.style.borderRadius = "999px";
    badge.style.fontSize = "11px";
    badge.style.fontWeight = "500";
    badge.style.whiteSpace = "nowrap";
    badge.style.background = "rgba(217, 119, 6, 0.16)";
    badge.style.color = "#b45309";
    badge.style.border = "1px solid rgba(217, 119, 6, 0.4)";
    summary.append(" ", badge);
  }
}

function paint(member, box) {
  placeInFrame($("ceBox"), box);
  const removed = removedFrom(box);
  for (const side of SIDES) {
    const field = fieldFor(side);
    if (document.activeElement !== field) field.value = String(removed[side]);
    field.max = String(side === "top" || side === "bottom" ? box.height : box.width);
    field.disabled = working;
    $("cropEditor").querySelector(`.ce-keep[data-side="${side}"]`).disabled =
      working || removed[side] === 0;
  }
  const whole = coversFrame(box);
  renderSummary(member, box, whole);
  const aspectEl = $("ceAspect");
  if (aspectEl) {
    if (box.crop_width > 0 && box.crop_height > 0) {
      const ratio = formatAspectRatio(box.crop_width, box.crop_height);
      aspectEl.textContent = `Output: ${box.crop_width}×${box.crop_height}${ratio ? ` (${ratio})` : ""}`;
      aspectEl.hidden = false;
    } else {
      aspectEl.hidden = true;
      aspectEl.textContent = "";
    }
  }
  $("ceApply").disabled = whole || working;
  // Reset has something to restore when the zone is not the shown level's.
  const suggested = member.crop_suggested;
  $("ceReset").disabled = working || (suggested ? sameEdges(box, suggested) : whole);
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
  $("cePrev").disabled = working || at <= 0;
  $("ceNext").disabled = working || at < 0 || at >= order.length - 1;

  const isVideo = member.media_type === "video";
  const stage = $("ceStage");
  if (stage) stage.style.gridTemplateRows = isVideo ? "minmax(0, 1fr) auto" : "";
  const image = $("ceImage");
  const video = $("ceVideo");
  const strip = $("ceVideoStrip");
  if (isVideo && video) {
    image.hidden = true;
    image.removeAttribute("src");
    video.hidden = false;
    if (strip) {
      strip.hidden = false;
      strip.style.display = "flex";
    }
    const url = mediaUrl(member.path);
    if (video.dataset.path !== member.path || video.getAttribute("src") !== url) {
      video.dataset.path = member.path;
      video.pause();
      video.poster = previewUrl(member.path);
      activeSampleFrac = VIDEO_SAMPLE_FRACTIONS[0];
      pendingSampleFrac = activeSampleFrac;
      video.setAttribute("src", url);
      video.load();
    }
    syncVideoStrip(video);
  } else {
    stopEditorVideo();
    image.hidden = false;
    const url = previewUrl(member.path);
    if (image.getAttribute("src") !== url) {
      image.setAttribute("src", url);
      image.alt = `Preview of ${name}`;
    }
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
        if (working) return;
        chooseCropLevel(currentGroup(), editorPath, button.dataset.level);
      });
    });
  }
  levels.querySelectorAll(".crop-level-btn").forEach((button) => {
    button.disabled = working;
    const unavailable = button.classList.contains("unavailable")
      && !button.classList.contains("chosen");
    button.style.opacity = unavailable ? "0.48" : "";
    button.style.borderStyle = unavailable ? "dashed" : "";
  });

  $("ceApply").textContent = working ? "Cropping…" : "Crop";
  $("ceSkip").disabled = working;
  const trashBtn = $("ceTrash");
  if (trashBtn) trashBtn.disabled = working || Boolean(member.protected);
  applyPreviewMode();
  layoutFrame(box);
  // A zone mid-drag is ahead of any reply or poll that lands meanwhile.
  if (!editor().classList.contains("crop-dragging")) paint(member, box);
}

// Set how much one side removes, in picture pixels (typed, or 0 from Keep).
function setSide(side, pixels, { live = false } = {}) {
  const member = editorMember();
  const start = member && zoneBox(member);
  if (!start || working) return;
  const clamp = (value, low, high) => Math.max(low, Math.min(high, Math.round(value)));
  const amount = Number.isFinite(pixels) ? pixels : 0;
  const edges = { left: start.left, top: start.top, right: start.right, bottom: start.bottom };
  const minW = Math.min(MIN_ZONE_PX, start.width);
  const minH = Math.min(MIN_ZONE_PX, start.height);
  if (side === "top") edges.top = clamp(amount, 0, start.bottom - minH);
  if (side === "bottom") edges.bottom = clamp(start.height - amount, start.top + minH, start.height);
  if (side === "left") edges.left = clamp(amount, 0, start.right - minW);
  if (side === "right") edges.right = clamp(start.width - amount, start.left + minW, start.width);
  const next = deriveBox(snapEdges(member, edges, start), start);
  if (!sameEdges(next, start)) queueZone(member.path, next);
  paint(member, next);
  syncCardZone(member, next);
  if (!live) {
    const field = fieldFor(side);
    if (document.activeElement === field) field.value = String(removedFrom(next)[side]);
    renderCropReview(currentGroup());
  }
}

// One arrow key in the editor: a screen pixel's worth of picture, so the
// zone can be placed more finely here than on a card.
function nudge(event) {
  const member = editorMember();
  const start = member && zoneBox(member);
  if (!start || working) return false;
  const shown = $("ceFrame").getBoundingClientRect().width || start.width;
  const even = member.media_type === "video" ? 2 : 1;
  const stepSize = Math.max(even, Math.round(start.width / shown));
  const next = nudgedBox(member, start, event, stepSize, stepSize);
  if (!sameEdges(next, start)) {
    queueZone(member.path, next);
    paint(member, next);
    renderCropReview(currentGroup());
  }
  return true;
}

function step(delta) {
  if (working) return;
  const g = currentGroup();
  const order = reviewable(g);
  const at = order.indexOf(editorMember(g));
  const next = order[at + delta];
  if (!next) return;
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

async function trashFromEditor() {
  const g = currentGroup();
  const path = editorPath;
  if (!g || !path || working) return;
  working = true;
  draw();
  try {
    await trashCropMember(g, path);
  } finally {
    working = false;
    if (cropEditorOpen()) draw();
  }
}

function buildVideoControls() {
  const video = $("ceVideo");
  const strip = $("ceVideoStrip");
  if (!video || !strip) return;
  video.style.position = "absolute";
  video.style.inset = "0";
  video.style.width = "100%";
  video.style.height = "100%";
  video.style.objectFit = "fill";
  video.style.pointerEvents = "none";
  video.style.zIndex = "1";

  strip.style.flexWrap = "wrap";
  strip.style.gap = "6px";
  strip.style.justifyContent = "center";
  strip.style.alignItems = "center";
  strip.style.marginTop = "10px";

  const playBtn = document.createElement("button");
  playBtn.type = "button";
  playBtn.className = "btn ghost ce-video-play";
  playBtn.textContent = "▶ Play";
  playBtn.setAttribute("aria-pressed", "false");
  playBtn.addEventListener("click", () => {
    if (video.paused || video.ended) {
      video.play().catch(() => { /* codec unavailable */ });
    } else {
      video.pause();
    }
  });

  const chips = VIDEO_SAMPLE_FRACTIONS.map((frac) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip ce-sample-chip";
    chip.dataset.frac = String(frac);
    chip.textContent = `${Math.round(frac * 100)}%`;
    chip.addEventListener("click", () => {
      activeSampleFrac = frac;
      pendingSampleFrac = frac;
      if (Number.isFinite(video.duration) && video.duration > 0) {
        video.currentTime = frac * video.duration;
      }
      syncVideoStrip(video);
    });
    return chip;
  });

  strip.replaceChildren(playBtn, ...chips);
  video.addEventListener("loadedmetadata", () => {
    if (pendingSampleFrac != null && Number.isFinite(video.duration) && video.duration > 0) {
      video.currentTime = pendingSampleFrac * video.duration;
      pendingSampleFrac = null;
    }
    syncVideoStrip(video);
  });
  for (const evt of ["play", "pause", "ended"]) {
    video.addEventListener(evt, () => syncVideoStrip(video));
  }
}

function build() {
  if (built) return;
  built = true;
  const guides = $("ceGuides");
  if (guides) {
    guides.style.position = "absolute";
    guides.style.inset = "0";
    guides.style.pointerEvents = "none";
    guides.style.zIndex = "2";
  }
  const box = $("ceBox");
  box.style.zIndex = "3";
  for (const handle of CROP_HANDLES) {
    const grip = document.createElement("span");
    grip.className = `crop-handle ${handle}`;
    grip.dataset.handle = handle;
    grip.setAttribute("aria-hidden", "true");
    box.append(grip);
  }
  buildVideoControls();
  bindZone(box, {
    path: () => editorPath,
    frame: () => $("ceFrame"),
    focus: () => {},
    paint,
    dragEl: editor(),
    nudge,
    guides: drawGuides,
    done: () => renderCropReview(currentGroup()),
  });
  for (const side of SIDES) {
    const field = fieldFor(side);
    field.addEventListener("input", (event) => {
      if (event.target.value === "") return;
      const val = Number.parseInt(event.target.value, 10);
      if (Number.isFinite(val) && val >= 0) setSide(side, val, { live: true });
    });
    field.addEventListener("change", (event) => {
      const val = Number.parseInt(event.target.value, 10);
      setSide(side, Number.isFinite(val) ? val : 0, { live: false });
    });
  }
  editor().querySelectorAll(".ce-keep").forEach((button) => {
    button.addEventListener("click", () => setSide(button.dataset.side, 0));
  });
  $("cePreviewToggle")?.addEventListener("click", cyclePreviewMode);
  $("ceApply").addEventListener("click", () => decide(true));
  $("ceSkip").addEventListener("click", () => decide(false));
  $("ceReset").addEventListener("click", () => {
    if (!working) resetCropZone(editorPath);
  });
  $("ceTrash")?.addEventListener("click", trashFromEditor);
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
  if (!editable(member, g)) return;
  build();
  const wasOpen = cropEditorOpen();
  if (!wasOpen) {
    previousFocus = document.activeElement;
    previewMode = "mask";
  }
  editorPath = path;
  if (!wasOpen) showOverlay(editor());
  syncAppInert();
  promoteToastTopLayer();
  draw();
  $("ceBox").focus({ preventScroll: true });
}

function closeCropEditor() {
  if (!cropEditorOpen()) return;
  const path = editorPath;
  hideOverlay(editor());
  syncAppInert();
  editorPath = null;
  previewMode = "mask";
  applyPreviewMode();
  drawGuides(null, null);
  $("ceImage").removeAttribute("src");
  stopEditorVideo();
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
  if (at < 0 || !editable(members[at], g)) {
    const next = at < 0
      ? null
      : members.slice(at + 1).find((m) => editable(m, g))
        || members.slice(0, at).reverse().find((m) => editable(m, g));
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
    const handled = trapTabKey(editor(), event);
    if (!handled) {
      const hasFocusable = editor()?.querySelector(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!hasFocusable) event.preventDefault();
    }
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
    if (!working) resetCropZone(editorPath);
  } else if (key === "v") cyclePreviewMode();
  else if (key === "]" || key === "PageDown") step(1);
  else if (key === "[" || key === "PageUp") step(-1);
  else if (["1", "2", "3"].includes(key)) {
    if (!working) chooseCropLevel(currentGroup(), editorPath, CROP_LEVELS[Number(key) - 1]);
  } else return true; // swallow: nothing behind the editor should react
  event.preventDefault();
  return true;
}

export { closeCropEditor, cropEditorOpen, handleCropEditorKey, openCropEditor, syncCropEditor };
