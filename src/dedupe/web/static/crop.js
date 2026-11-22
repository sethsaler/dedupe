// Crop review: images and videos framed by bars (black, or on images any
// flat colour) or screenshot interface.
// Each card shows the picture (a video's poster frame) with the suggested crop
// drawn on it — the bars that would go are shaded — and one click applies it.
// The zone itself can be clicked to select it, dragged to move it, and
// resized from any edge or corner; the moved zone is saved with the review
// and is the crop a Crop (or Crop all) click cuts. Reset zone puts the
// detector's suggestion back. A dragged edge snaps to the edges the detector
// found at any level and to the picture's own edge (Alt places it freely),
// and Adjust (or a double-click on the zone) opens the same zone in a large
// editor with a field for each side.
// A card's zone can be toggled off to see the untouched picture (and every
// zone at once from the header), then back on to see what a crop removes.
// The file is rewritten in place (videos are re-encoded) and the untouched
// original goes to the Trash, so every crop has an Undo. Skip declines a
// suggestion; it is remembered so the same file is not suggested again.

import { undoAction } from "./actions.js";
import { api } from "./api.js";
import { applyResultControls, loadGroups, renderTabCounts, updateGroupListItem } from "./groups.js";
import { openLightbox } from "./lightbox.js";
import { currentGroup, patchGroup } from "./model.js";
import { scheduleRender } from "./render.js";
import { state } from "./state.js";
import { confirmModal } from "./modal.js";
import { openCropEditor, syncCropEditor } from "./cropeditor.js";
import {
  $, basename, escapeHtml, formatBytes, hideToast, isOverlayOpen, noteMediaVersion, sleep, toast,
  withMediaVersion,
} from "./util.js";

const busy = new Set();
const undoing = new Set();

// Zones the user has dragged, by path, shown until the server's copy of the
// group (which carries the same box as its effective crop) replaces them.
// The server is the store; this only keeps a drag ahead of its save.
const localAdjustments = new Map();
const adjustInFlight = new Map();
const adjustTimers = new Map();
const adjustEpoch = new Map();
let adjustmentScan = null;

const CROP_PAGE_SIZE = 50;
let activeCropFilter = "all";
let cropPage = 0;
let recheckBusy = false;
let skipAllBusy = false;

const TRASH_ELIGIBLE_KINDS = new Set(["all_files", "no_humans", "faces", "sets", "similar"]);
const STANDARD_ASPECT_RATIOS = [
  [1, 1], [4, 3], [3, 4], [3, 2], [2, 3], [16, 9], [9, 16],
  [16, 10], [10, 16], [21, 9], [5, 4], [4, 5],
];
const PROVENANCE_LABELS = {
  chrome: "Screenshot UI",
  black_bar: "Black bars",
  colour_bar: "Flat borders",
  blurred_bar: "Blurred bars",
};

function clearPendingAdjustments(path) {
  const timer = adjustTimers.get(path);
  if (timer) clearTimeout(timer);
  adjustTimers.delete(path);
  adjustEpoch.set(path, (adjustEpoch.get(path) || 0) + 1);
}

function bumpAdjustEpoch(path) {
  adjustEpoch.set(path, (adjustEpoch.get(path) || 0) + 1);
}

function syncAdjustmentScan() {
  if (adjustmentScan === state.scanId) return;
  adjustmentScan = state.scanId;
  for (const timer of adjustTimers.values()) clearTimeout(timer);
  adjustTimers.clear();
  localAdjustments.clear();
  adjustInFlight.clear();
  adjustEpoch.clear();
  busy.clear();
  undoing.clear();
  hiddenZones.clear();
  openWhy.clear();
  activeCropFilter = "all";
  cropPage = 0;
  document.querySelectorAll(".crop-card").forEach((card) => { delete card.dataset.signature; });
}

// A pointer press on a zone must not fall through to the thumbnail's click
// (which opens the lightbox): the press stamps its card's path here and the
// thumbnail handler swallows one click that follows it.
const suppressThumbClick = new Map();
const CROP_HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
// The smallest zone a drag can make, in picture pixels per side.
const MIN_CROP_PX = 8;
// A dragged edge within this many screen pixels of a detected edge lands on it.
const SNAP_SCREEN_PX = 7;

// Aggressiveness levels: every candidate was measured at all three in one
// pass. The level on show is the suggestion Crop (and Crop all) cuts.
const CROP_LEVELS = ["conservative", "balanced", "aggressive"];
const CROP_LEVEL_LABELS = {
  conservative: "Conservative",
  balanced: "Balanced",
  aggressive: "Aggressive",
};
const CROP_LEVEL_TITLES = {
  conservative: "Conservative — only flat bars of one colour with a hard edge",
  balanced: "Balanced — also black bars holding captions or encoder noise",
  aggressive: "Aggressive — also dark-gray and hairline bars, and colour bars holding a mark",
};

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

function deletedSet(g) {
  return new Set(g?.deleted_paths || []);
}

function pendingMembers(g) {
  const deleted = deletedSet(g);
  return (g?.members || []).filter(
    (member) => (member.crop_state || "pending") === "pending" && !member.protected && !deleted.has(member.path),
  );
}

// Pending files a Crop all click would actually cut: the level on show (or
// a dragged zone) gives them a box. The rest stay undecided.
function croppableMembers(g) {
  return pendingMembers(g).filter((member) => effectiveBox(member));
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
    provenance: frame.provenance || [],
  };
}

// The zone on screen for a member: an unsaved drag wins over the server's
// effective crop, which already folds in any saved adjustment.
function effectiveBox(member) {
  if (!member) return null;
  if (localAdjustments.has(member.path)) {
    const local = localAdjustments.get(member.path);
    return local && !coversFrame(local) ? local : null;
  }
  return member.crop || null;
}

function hasAnySuggestion(member) {
  return Boolean(
    member?.crop_suggested || Object.values(member?.crop_levels || {}).some(Boolean),
  );
}

// The zone a drag starts from. Where the level on show suggests nothing the
// whole picture is the zone, so an edge can still be pulled in by hand.
function zoneBox(member) {
  if (!member) return null;
  if (localAdjustments.has(member.path)) {
    const local = localAdjustments.get(member.path);
    if (local) return local;
  }
  const shown = effectiveBox(member);
  if (shown) return shown;
  const frame = Object.values(member?.crop_levels || {}).find(Boolean)
    || member?.crop_suggested
    || (member.width > 0 && member.height > 0 ? { width: member.width, height: member.height } : null);
  if (!frame) return null;
  return deriveBox({ left: 0, top: 0, right: frame.width, bottom: frame.height }, frame);
}

// A whole-picture zone is no crop: nothing to save, nothing to cut.
function coversFrame(box) {
  return Boolean(box) && box.left === 0 && box.top === 0 && box.right === box.width && box.bottom === box.height;
}

function isKeptWhole(member) {
  if (!member) return false;
  if (localAdjustments.has(member.path)) {
    const local = localAdjustments.get(member.path);
    return Boolean(local && coversFrame(local)) && hasAnySuggestion(member);
  }
  return Boolean(member.crop_keep_whole);
}

function isAdjusted(member) {
  if (!member) return false;
  if (localAdjustments.has(member.path)) {
    const local = localAdjustments.get(member.path);
    if (!local || coversFrame(local)) return Boolean(member.crop_suggested);
    return member.crop_suggested ? !sameEdges(local, member.crop_suggested) : true;
  }
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

// Where a dragged edge may land exactly: the picture's own edges (no crop
// on that side) and every edge the detector found, at any level.
function snapTargets(member, frame) {
  const x = new Set([0, frame.width]);
  const y = new Set([0, frame.height]);
  for (const box of [...Object.values(member?.crop_levels || {}), member?.crop_suggested]) {
    if (!box) continue;
    x.add(box.left).add(box.right);
    y.add(box.top).add(box.bottom);
  }
  return { x: [...x], y: [...y] };
}

function snapped(value, targets, reach) {
  let best = value;
  let nearest = reach;
  let didSnap = false;
  for (const target of targets) {
    const distance = Math.abs(target - value);
    if (distance <= nearest) {
      best = target;
      nearest = distance;
      didSnap = true;
    }
  }
  return { value: best, snapped: didSnap };
}

function draggedBoxDetailed(member, start, handle, dx, dy, snap = null) {
  const frame = start;
  const minW = Math.max(1, Math.min(MIN_CROP_PX, frame.width));
  const minH = Math.max(1, Math.min(MIN_CROP_PX, frame.height));
  const snapX = (value) => (snap ? snapped(value, snap.x, snap.reach) : { value, snapped: false });
  const snapY = (value) => (snap ? snapped(value, snap.y, snap.reach) : { value, snapped: false });
  const clamp = (value, low, high) => Math.max(low, Math.min(high, Math.round(value)));
  let edges = edgesOf(start);
  const guidesX = [];
  const guidesY = [];
  if (handle === "move") {
    const shiftX = clamp(dx, -start.left, frame.width - start.right);
    const shiftY = clamp(dy, -start.top, frame.height - start.bottom);
    edges = {
      left: start.left + shiftX, right: start.right + shiftX,
      top: start.top + shiftY, bottom: start.bottom + shiftY,
    };
  } else {
    if (handle.includes("w")) {
      const res = snapX(start.left + dx);
      edges.left = clamp(res.value, 0, start.right - minW);
      if (res.snapped) guidesX.push(edges.left);
    }
    if (handle.includes("e")) {
      const res = snapX(start.right + dx);
      edges.right = clamp(res.value, start.left + minW, frame.width);
      if (res.snapped) guidesX.push(edges.right);
    }
    if (handle.includes("n")) {
      const res = snapY(start.top + dy);
      edges.top = clamp(res.value, 0, start.bottom - minH);
      if (res.snapped) guidesY.push(edges.top);
    }
    if (handle.includes("s")) {
      const res = snapY(start.bottom + dy);
      edges.bottom = clamp(res.value, start.top + minH, frame.height);
      if (res.snapped) guidesY.push(edges.bottom);
    }
  }
  return {
    box: deriveBox(snapEdges(member, edges, frame), frame),
    guides: { x: guidesX, y: guidesY },
  };
}

// The zone after dragging ``start`` by (dx, dy) picture pixels: "move"
// slides it inside the frame; a handle name drags its named edge(s).
// ``snap`` ({x, y, reach}) pulls a dragged edge onto a nearby target.
function draggedBox(member, start, handle, dx, dy, snap = null) {
  return draggedBoxDetailed(member, start, handle, dx, dy, snap).box;
}

function hasBlackBars(crop) {
  const chrome = crop.chrome || [];
  return (crop.left > 0) || (crop.right < crop.width)
    || (crop.top > 0 && !chrome.includes("top"))
    || (crop.bottom < crop.height && !chrome.includes("bottom"));
}

// Each removed edge with its rows; when interface and bars mix, each
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
      return chrome.has(side) ? `${px}px UI (${side})` : `${px}px bar (${side})`;
    })
    .join(" · ");
}

function provenanceTags(member, crop) {
  const raw = crop?.provenance?.length ? crop.provenance : (member?.crop_provenance || []);
  const mapped = [];
  for (const tag of raw) {
    const label = PROVENANCE_LABELS[tag];
    if (label && !mapped.includes(label)) mapped.push(label);
  }
  if (mapped.length) return mapped.join(" & ");
  const chrome = crop?.chrome || [];
  if (!chrome.length) return "Black bars";
  return hasBlackBars(crop) ? "Screenshot UI & Black bars" : "Screenshot UI";
}

function duplicateMembershipLabel(path) {
  if (!path) return "";
  let inExact = false;
  let inSimilar = false;
  for (const g of state.allGroups || []) {
    if (g.kind !== "exact" && g.kind !== "similar") continue;
    if (!(g.members || []).some((m) => m.path === path)) continue;
    if (g.kind === "exact") inExact = true;
    else inSimilar = true;
    if (inExact && inSimilar) break;
  }
  if (inExact && inSimilar) return "Also in Exact/Similar";
  if (inExact) return "Also in Exact";
  if (inSimilar) return "Also in Similar";
  return "";
}

function cropWarnings(crop) {
  if (!crop || coversFrame(crop)) return [];
  const warnings = [];
  if ((crop.removed_fraction || 0) > 0.5) warnings.push("Removes >50%");
  const shortest = Math.min(crop.crop_width || 0, crop.crop_height || 0);
  if (shortest > 0 && shortest < 480) warnings.push("Output <480px");
  return warnings;
}

function formatAspectRatio(width, height) {
  if (!(width > 0 && height > 0)) return "";
  const ratio = width / height;
  for (const [rw, rh] of STANDARD_ASPECT_RATIOS) {
    if (Math.abs(ratio - rw / rh) / ratio <= 0.015) return `${rw}:${rh}`;
  }
  const gcd = (a, b) => (b === 0 ? a : gcd(b, a % b));
  const d = gcd(Math.round(width), Math.round(height));
  const sw = Math.round(width / d);
  const sh = Math.round(height / d);
  if (sw <= 24 && sh <= 24) return `${sw}:${sh}`;
  return `${ratio.toFixed(2)}:1`;
}

function applyWarningBadgeStyle(el) {
  el.style.display = "inline-block";
  el.style.marginLeft = "6px";
  el.style.padding = "1px 6px";
  el.style.borderRadius = "999px";
  el.style.fontSize = "11px";
  el.style.fontWeight = "500";
  el.style.whiteSpace = "nowrap";
  el.style.background = "rgba(217, 119, 6, 0.16)";
  el.style.color = "#b45309";
  el.style.border = "1px solid rgba(217, 119, 6, 0.4)";
}

function renderEvidenceContent(el, text, warnings) {
  if (!el) return;
  el.textContent = text;
  el.title = warnings.length ? `${text} · ⚠ ${warnings.join(" · ")}` : text;
  if (warnings.length) {
    const badge = document.createElement("span");
    badge.className = "crop-warn-badge";
    badge.textContent = `⚠ ${warnings.join(" · ")}`;
    applyWarningBadgeStyle(badge);
    el.append(" ", badge);
  }
}

function evidenceFor(member, crop = effectiveBox(member)) {
  if (!crop) {
    if (isKeptWhole(member)) return "Kept whole · no pixels cut";
    const label = CROP_LEVEL_LABELS[member.crop_level] || "Balanced";
    return `No crop suggested at the ${label} level`;
  }
  const removed = Math.round((crop.removed_fraction || 0) * 100);
  const size = `${crop.width}×${crop.height} → ${crop.crop_width}×${crop.crop_height}`;
  if (member.crop_state === "cropped") {
    return `Cropped to ${crop.crop_width}×${crop.crop_height} · original is in the Trash`;
  }
  if (member.crop_state === "skipped") {
    return `Skipped · ${size} would remove ${removed}%`;
  }
  // A video crop re-encodes the whole clip; say so before the click, not after.
  const reencode = member.media_type === "video" ? " · re-encodes video" : "";
  const adjusted = member.crop_suggested && !sameEdges(crop, member.crop_suggested)
    ? "Adjusted zone · "
    : "";
  const dup = duplicateMembershipLabel(member.path);
  return `${adjusted}${provenanceTags(member, crop)}: ${barsLabel(crop)} · ${size} (${removed}%)${dup ? ` · ${dup}` : ""}${reencode}`;
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
  + "resize it. With the zone focused, arrow keys move it, Shift+arrow keys move its bottom and "
  + "right edges, and Alt+arrow keys move its top and left edges.";

function cropBoxHtml(member, name, editable) {
  if (!editable) return '<span class="crop-box" aria-hidden="true"></span>';
  const handles = CROP_HANDLES
    .map((handle) => `<span class="crop-handle ${handle}" data-handle="${handle}" aria-hidden="true"></span>`)
    .join("");
  return `<span class="crop-box editable" tabindex="0" role="group" aria-label="${escapeHtml(cropBoxLabel(name))}" title="Drag to move · drag an edge or corner to resize · double-click to adjust it large">${handles}</span>`;
}

// The level switch under a card's evidence line. Every level is offered —
// switching to one that suggested nothing shows the untouched picture (or
// starts from the whole frame in the editor) — and unavailable levels dim
// visually while staying clickable.
function levelSwitchHtml(member, name) {
  if ((member.crop_state || "pending") === "cropped") return "";
  const levels = member.crop_levels || {};
  if (!CROP_LEVELS.some((level) => levels[level])) return "";
  const current = member.crop_level || "balanced";
  const buttons = CROP_LEVELS.map((level, index) => {
    const available = Boolean(levels[level]);
    const title = `${CROP_LEVEL_TITLES[level]}${available ? "" : " (no suggestion at this level — starts from whole frame in editor)"} (${index + 1})`;
    return `<button class="crop-level-btn ${level === current ? "chosen" : ""} ${available ? "" : "unavailable"}" type="button"
      data-level="${level}" data-path="${escapeHtml(member.path)}" data-available="${available}"
      aria-pressed="${level === current}" title="${escapeHtml(title)}">${CROP_LEVEL_LABELS[level]}</button>`;
  }).join("");
  return `<div class="crop-levels" role="group" aria-label="Crop aggressiveness for ${escapeHtml(name)}">${buttons}</div>`;
}

function styleUnavailableLevels(root) {
  root.querySelectorAll(".crop-level-btn").forEach((btn) => {
    const unavailable = btn.classList.contains("unavailable") && !btn.classList.contains("chosen");
    btn.style.opacity = unavailable ? "0.48" : "";
    btn.style.borderStyle = unavailable ? "dashed" : "";
  });
}

function cardHtml(g, member, index, lightboxIndex = index) {
  const name = basename(member.path);
  const deleted = deletedSet(g).has(member.path);
  const state_ = deleted ? "deleted" : (member.crop_state || "pending");
  const crop = deleted ? null : effectiveBox(member);
  // The frame always has the original's proportions, so applying or undoing
  // a crop never resizes the card: a cropped file is drawn inside the box it
  // was cut to and the bars it lost are left empty; otherwise the suggestion
  // is drawn over the original.
  const showsCrop = !deleted && Boolean(crop) && state_ !== "cropped";
  const editable = showsCrop && !member.protected;
  const adjusted = !deleted && editable && isAdjusted(member);
  const zoneHidden = showsCrop && hiddenZones.has(member.path);
  const width = crop?.width || member.width;
  const height = crop?.height || member.height;
  const badge = deleted
    ? '<span class="crop-badge skipped">Moved to Trash</span>'
    : state_ === "cropped"
      ? '<span class="crop-badge done">Cropped</span>'
      : state_ === "skipped"
        ? '<span class="crop-badge skipped">Skipped</span>'
        : "";
  const trashBtn = !deleted && !member.protected && state_ !== "cropped"
    ? `<button class="btn ghost danger-soft crop-trash" data-path="${escapeHtml(member.path)}" type="button" title="Move original file to Trash (undoable)">Trash</button>`
    : "";
  const primary = deleted
    ? `<button class="btn ghost crop-trash-undo" data-path="${escapeHtml(member.path)}" type="button" title="Restore from Trash">Undo trash</button>`
    : member.protected
      ? '<span class="protected-label">Protected library</span>'
      : state_ === "cropped"
        ? `<button class="btn ghost crop-undo" data-path="${escapeHtml(member.path)}" type="button" ${member.crop_undo_available ? "" : "disabled title=\"The original's place in the Trash is unknown\""}>Undo crop</button>`
        : state_ === "skipped"
          ? `<button class="btn ghost crop-unskip" data-path="${escapeHtml(member.path)}" type="button" title="Suggest this crop again">Reconsider</button>${trashBtn}`
          : `<button class="btn primary crop-apply" data-path="${escapeHtml(member.path)}" type="button" ${crop ? 'title="Crop now — the original goes to the Trash (c)"' : "disabled title=\"The level on show suggests no crop for this file\""}>Crop</button>
             <button class="btn ghost crop-skip" data-path="${escapeHtml(member.path)}" type="button" title="Leave this file as it is and stop suggesting it (x)">Skip</button>${trashBtn}`;
  const evText = deleted ? "Moved to Trash · undoable" : evidenceFor(member, crop);
  return `
    <article class="card crop-card ${state_} ${zoneHidden ? "crop-zone-hidden" : ""} ${index === state.memberFocus ? "focused" : ""}" data-path="${escapeHtml(member.path)}" data-index="${index}" data-lightbox-index="${lightboxIndex}">
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
        <div class="evidence" title="${escapeHtml(evText)}">${escapeHtml(evText)}</div>
        ${deleted ? "" : levelSwitchHtml(member, name)}
        <div class="card-actions crop-actions">
          ${primary}
        </div>
        <div class="crop-tools">
          ${!deleted && !member.protected && state_ === "pending" ? `<button class="linkish crop-adjust" data-path="${escapeHtml(member.path)}" type="button" title="Adjust this zone on a large view, with a field for each side (e)">Adjust</button>` : ""}
          ${adjusted ? `<button class="linkish crop-reset" data-path="${escapeHtml(member.path)}" type="button" title="Put the suggested crop zone back">Reset zone</button>` : ""}
          ${showsCrop ? `<button class="linkish crop-zone-toggle" data-path="${escapeHtml(member.path)}" type="button" aria-pressed="${!zoneHidden}" title="${zoneToggleTitle(zoneHidden)}">${zoneHidden ? "Show zone" : "Hide zone"}</button>` : ""}
          <button class="linkish reveal" data-path="${escapeHtml(member.path)}" type="button">Reveal</button>
        </div>
        ${deleted ? "" : skipWhyHtml(g, member)}
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
  const deleted = card.classList.contains("deleted");
  const image = card.querySelector(".crop-image");
  if (image) {
    image.style.opacity = deleted ? "0.42" : "";
    if (!deleted && crop && member.crop_state === "cropped") {
      placeInFrame(image, member.crop || crop);
    }
  }
  if (!crop || deleted) return;
  const box = card.querySelector(".crop-box");
  if (box) placeInFrame(box, crop);
}

// Position an element over the crop rectangle, in the original's coordinates.
function placeInFrame(element, crop) {
  element.style.left = `${(crop.left / crop.width) * 100}%`;
  element.style.top = `${(crop.top / crop.height) * 100}%`;
  element.style.width = `${(crop.crop_width / crop.width) * 100}%`;
  element.style.height = `${(crop.crop_height / crop.height) * 100}%`;
}

// What a card shows; a card is rebuilt only when this changes.
function cardSignature(member, g = currentGroup()) {
  const crop = effectiveBox(member);
  const suggested = member.crop_suggested;
  const deleted = deletedSet(g).has(member.path) ? 1 : 0;
  const levels = CROP_LEVELS
    .map((level) => {
      const box = (member.crop_levels || {})[level];
      return box ? [box.left, box.top, box.right, box.bottom].join(",") : "";
    })
    .join(";");
  return [
    deleted,
    member.crop_state || "pending",
    member.crop_level || "balanced",
    levels,
    member.protected ? 1 : 0,
    member.crop_undo_available ? 1 : 0,
    member.size,
    member.crop_skip_reason || "",
    member.crop_skip_note || "",
    hiddenZones.has(member.path) ? 1 : 0,
    isAdjusted(member) ? 1 : 0,
    member.crop_keep_whole ? 1 : 0,
    (member.crop_provenance || []).join(","),
    duplicateMembershipLabel(member.path),
    crop ? [crop.left, crop.top, crop.right, crop.bottom, crop.width, crop.height].join(",") : "",
    suggested ? [suggested.left, suggested.top, suggested.right, suggested.bottom].join(",") : "",
  ].join("|");
}

function buildCard(g, member, index, lightboxIndex = index) {
  const template = document.createElement("template");
  template.innerHTML = cardHtml(g, member, index, lightboxIndex).trim();
  const card = template.content.firstElementChild;
  card.dataset.signature = cardSignature(member, g);
  layoutCard(card, member);
  styleUnavailableLevels(card);
  const deleted = deletedSet(g).has(member.path);
  const crop = deleted ? null : effectiveBox(member);
  const evidenceEl = card.querySelector(".evidence");
  if (!deleted && evidenceEl) {
    const warnings = member.crop_state === "pending" ? cropWarnings(crop) : [];
    renderEvidenceContent(evidenceEl, evidenceFor(member, crop), warnings);
  }
  bindCard(card);
  return card;
}

// Swap a changed card in place. The previous picture stays on screen until
// the new one has loaded, so the card never flashes empty.
function replaceCard(previous, card) {
  const active = document.activeElement;
  const keepFocus = previous.contains(active);
  const keepZoneFocus = keepFocus && active?.classList?.contains("crop-box");
  const keepThumbFocus = keepFocus && active?.classList?.contains("crop-thumb");
  const keepLevel = keepFocus && active?.classList?.contains("crop-level-btn")
    ? active.dataset.level
    : null;
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
  if (keepZoneFocus) {
    card.querySelector(".crop-box.editable")?.focus({ preventScroll: true });
  } else if (keepLevel) {
    card.querySelector(`.crop-level-btn[data-level="${CSS.escape(keepLevel)}"]`)
      ?.focus({ preventScroll: true });
  } else if (keepThumbFocus) {
    card.querySelector(".crop-thumb")?.focus({ preventScroll: true });
  }
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
    const lbIndex = Number(card.dataset.lightboxIndex);
    openLightbox(Number.isFinite(lbIndex) ? lbIndex : state.memberFocus);
  });
  card.querySelectorAll(".crop-level-btn").forEach((button) => {
    button.addEventListener("click", () => {
      chooseCropLevel(currentGroup(), button.dataset.path, button.dataset.level);
    });
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
  card.querySelector(".crop-trash")?.addEventListener("click", (event) => {
    trashCropMember(currentGroup(), event.currentTarget.dataset.path);
  });
  card.querySelector(".crop-trash-undo")?.addEventListener("click", (event) => {
    undoTrashCropMember(currentGroup(), null, event.currentTarget.dataset.path);
  });
  card.querySelector(".crop-zone-toggle")?.addEventListener("click", (event) => {
    toggleCropZone(event.currentTarget.dataset.path);
  });
  card.querySelector(".crop-reset")?.addEventListener("click", (event) => {
    resetCropZone(event.currentTarget.dataset.path);
  });
  card.querySelector(".crop-adjust")?.addEventListener("click", (event) => {
    openCropEditor(event.currentTarget.dataset.path);
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
  const shownBox = box && coversFrame(box) ? null : box;
  const text = evidenceFor(member, shownBox);
  const warnings = member.crop_state === "pending" ? cropWarnings(shownBox) : [];
  renderEvidenceContent(evidence, text, warnings);
}

// Lightweight card box update during live input in the large editor.
function syncCardZone(member, box) {
  if (!member?.path) return;
  const card = document.querySelector(`.crop-card[data-path="${CSS.escape(member.path)}"]`);
  if (!card) return;
  const boxEl = card.querySelector(".crop-box");
  if (boxEl && box && !coversFrame(box)) placeInFrame(boxEl, box);
  syncEvidence(card, member, box);
}

// Click, drag, and resize for a zone, wherever it is drawn. ``host`` names
// the file (``path()``), the element whose box is the whole picture
// (``frame()``), how to mark the zone's owner focused (``focus()``), how to
// draw a box while it moves (``paint(member, box)``), the element that
// carries the dragging class (``dragEl``), and optionally when a press must
// be ignored (``blocked(path)``), how an arrow key moves the zone
// (``nudge(event)``), snap guide callback (``guides(axes, box)``), and
// what follows a finished drag (``done()``).
function bindZone(boxEl, host) {
  boxEl.addEventListener("focus", () => host.focus());
  boxEl.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    const path = host.path();
    const member = (currentGroup()?.members || []).find((m) => m.path === path);
    const start = member && zoneBox(member);
    const frame = host.frame();
    if (!member || !start || !frame || busy.has(path) || host.blocked?.(path)) return;
    event.preventDefault();
    event.stopPropagation();
    suppressThumbClick.set(path, Date.now());
    host.focus();
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
    const targets = snapTargets(member, start);
    let current = start;
    host.dragEl.classList.add("crop-dragging");
    boxEl.dataset.dragHandle = handle;

    const onMove = (moveEvent) => {
      if (rect.width <= 0 || rect.height <= 0) return;
      const dx = ((moveEvent.clientX - startX) / rect.width) * start.width;
      const dy = ((moveEvent.clientY - startY) / rect.height) * start.height;
      // Scale snap reach down proportionally on small card thumbnails so dragging
      // a 4K frame on a compact card does not jump ~112px at once.
      const snapFactor = Math.min(1, rect.width / 500);
      const snapPx = Math.max(1.5, SNAP_SCREEN_PX * snapFactor);
      const rawReach = (snapPx / rect.width) * start.width;
      const reach = rect.width < 420
        ? Math.min(rawReach, Math.max(4, start.width * 0.02))
        : rawReach;
      // Alt places the edge freely, between the edges it would snap to.
      const snap = moveEvent.altKey ? null : { ...targets, reach };
      const { box: next, guides } = draggedBoxDetailed(member, start, handle, dx, dy, snap);
      host.guides?.(guides, next);
      if (sameEdges(next, current)) return;
      current = next;
      localAdjustments.set(path, current);
      host.paint(member, current);
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      try {
        if (boxEl.hasPointerCapture?.(event.pointerId)) boxEl.releasePointerCapture(event.pointerId);
      } catch {
        /* ignore */
      }
      host.dragEl.classList.remove("crop-dragging");
      delete boxEl.dataset.dragHandle;
      host.guides?.(null, current);
      suppressThumbClick.set(path, Date.now());
      if (sameEdges(current, start)) {
        // A plain click: the zone is selected; its owner already took focus.
        if (sameEdges(current, member.crop)) localAdjustments.delete(path);
        return;
      }
      void commitZone(path, current);
      host.done?.();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
  });
  // The page's key map leaves Alt chords to the browser, so the zone takes
  // its own: Alt+arrows move the top and left edges.
  boxEl.addEventListener("keydown", (event) => {
    if (!event.altKey || event.metaKey || event.ctrlKey) return;
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    if (host.nudge ? host.nudge(event) : nudgeCropZone(boxEl, event)) {
      event.preventDefault();
      event.stopPropagation();
    }
  });
}

// Save a zone the user just placed. Pulling every edge out to the picture's
// boundary keeps the whole frame explicitly (keep_whole: true) when a
// suggestion exists so it never snaps back on reload or poll.
function commitZone(path, box) {
  const member = (currentGroup()?.members || []).find((m) => m.path === path);
  if (coversFrame(box)) {
    if (hasAnySuggestion(member)) {
      localAdjustments.set(path, box);
      return saveAdjustment(path, box, currentGroup(), state.scanId, { keepWhole: true });
    }
    localAdjustments.delete(path);
    return saveAdjustment(path, null);
  }
  return saveAdjustment(path, box);
}

// A card's zone lives inside the thumbnail button, so a press on it claims
// the gesture (and stamps the suppression the thumbnail's click handler
// checks) instead of enlarging.
function bindCropBox(card, boxEl) {
  bindZone(boxEl, {
    path: () => card.dataset.path,
    frame: () => card.querySelector(".crop-frame"),
    focus: () => {
      state.memberFocus = Number(card.dataset.index);
      markFocused();
    },
    paint: (member, box) => {
      placeInFrame(boxEl, box);
      syncEvidence(card, member, box);
    },
    dragEl: card,
    blocked: (path) => hiddenZones.has(path),
  });
  // A zone can be Tabbed to; Enter/Space there must not also press the
  // enclosing thumbnail button.
  boxEl.addEventListener("click", (event) => {
    event.stopPropagation();
    suppressThumbClick.set(card.dataset.path, Date.now());
  });
  boxEl.addEventListener("dblclick", (event) => {
    event.stopPropagation();
    openCropEditor(card.dataset.path);
  });
}

// Store a dragged zone (or, with null, the suggestion again). The reply is
// the whole updated group, applied like any other crop decision.
async function saveAdjustment(
  path,
  box,
  g = currentGroup(),
  scanId = state.scanId,
  { keepWhole = false } = {},
) {
  syncAdjustmentScan();
  if (!g || !path || state.scanId !== scanId) return false;
  const timer = adjustTimers.get(path);
  if (timer) clearTimeout(timer);
  adjustTimers.delete(path);
  const myEpoch = adjustEpoch.get(path) || 0;
  // Serialize writes for this file: ignoring an older reply alone would still
  // let an older request overwrite the newer box on the server.
  const previous = adjustInFlight.get(path);
  const run = (async () => {
    if (previous) await previous;
    if (state.scanId !== scanId || (adjustEpoch.get(path) || 0) !== myEpoch) return false;
    try {
      const payload = {
        group_id: g.id,
        path,
        scan_id: scanId,
        box: box ? edgesOf(box) : null,
        ...(keepWhole ? { keep_whole: true } : {}),
      };
      const updated = await post("/api/crop/adjust", payload);
      if (state.scanId !== scanId || (adjustEpoch.get(path) || 0) !== myEpoch) return false;
      if (localAdjustments.get(path) === box) localAdjustments.delete(path);
      // Replies contain an entire group snapshot. Another card may have saved
      // since that snapshot was taken; only acknowledge this request's member.
      const group = state.allGroups.find((item) => item.id === g.id)
        || state.groups.find((item) => item.id === g.id);
      if (group) {
        const focused = document.activeElement?.closest(".crop-card")?.dataset.path;
        const hadBoxFocus = document.activeElement?.classList?.contains("crop-box");
        applyUpdate({
          ...group,
          members: group.members.map((member) => {
            const saved = updated.members.find((item) => item.path === path);
            if (member.path !== path || !saved || member.crop_state === "cropped") return member;
            return {
              ...member,
              crop: saved.crop,
              crop_adjusted: saved.crop_adjusted,
              crop_suggested: saved.crop_suggested,
              crop_keep_whole: saved.crop_keep_whole,
              crop_provenance: saved.crop_provenance,
            };
          }),
        });
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
  const group = currentGroup();
  const scanId = state.scanId;
  const member = group?.members.find((item) => item.path === path);
  if (group?.kind !== "crop" || !member) return;
  clearPendingAdjustments(path);
  const hadLocal = localAdjustments.has(path);
  const prevLocal = localAdjustments.get(path);
  // Keep reset visible even if an earlier save arrives while reset is queued.
  // (Where the level on show suggests nothing, reset means no zone at all.)
  const suggestion = member.crop_suggested || null;
  localAdjustments.set(path, suggestion);
  void saveAdjustment(path, null, group, scanId).then((saved) => {
    if (state.scanId !== scanId) return;
    if (saved) {
      if (localAdjustments.get(path) === suggestion) localAdjustments.delete(path);
      const selected = currentGroup();
      if (selected?.id === group.id && selected.kind === "crop") renderCropReview(selected);
    } else if (localAdjustments.get(path) === suggestion) {
      if (hadLocal) localAdjustments.set(path, prevLocal);
      else localAdjustments.delete(path);
      const selected = currentGroup();
      if (selected?.id === group.id && selected.kind === "crop") renderCropReview(selected);
    }
  });
  renderCropReview(group);
}

// The zone after one arrow key: plain arrows move it, Shift+arrows move
// its bottom and right edges, Alt+arrows its top and left edges.
function nudgedBox(member, start, event, stepX, stepY) {
  const dx = event.key === "ArrowRight" ? stepX : event.key === "ArrowLeft" ? -stepX : 0;
  const dy = event.key === "ArrowDown" ? stepY : event.key === "ArrowUp" ? -stepY : 0;
  const handle = event.altKey
    ? (dx !== 0 ? "w" : "n")
    : event.shiftKey ? (dx !== 0 ? "e" : "s") : "move";
  return draggedBox(member, start, handle, dx, dy);
}

// Show a keyed zone at once and save it after a pause, so held keys send
// one zone, not fifty.
function queueZone(path, box) {
  clearPendingAdjustments(path);
  localAdjustments.set(path, box);
  const group = currentGroup();
  const scanId = state.scanId;
  const member = group?.members.find((m) => m.path === path);
  adjustTimers.set(path, setTimeout(() => {
    adjustTimers.delete(path);
    if (state.scanId !== scanId) return;
    if (coversFrame(box)) {
      if (hasAnySuggestion(member)) {
        void saveAdjustment(path, box, group, scanId, { keepWhole: true });
      } else {
        if (localAdjustments.get(path) === box) localAdjustments.delete(path);
        void saveAdjustment(path, null, group, scanId);
      }
    } else {
      void saveAdjustment(path, box, group, scanId);
    }
  }, 350));
}

// Arrow-key editing for a focused zone on a card, by 1% of the picture (at
// least a pixel).
function nudgeCropZone(boxEl, event) {
  const card = boxEl.closest(".crop-card");
  const path = card?.dataset.path;
  const member = (currentGroup()?.members || []).find((m) => m.path === path);
  const start = member && effectiveBox(member);
  if (!card || !member || !start || busy.has(path)) return false;
  state.memberFocus = Number(card.dataset.index);
  markFocused();
  const next = nudgedBox(
    member, start, event,
    Math.max(1, Math.round(start.width * 0.01)),
    Math.max(1, Math.round(start.height * 0.01)),
  );
  if (!sameEdges(next, start)) {
    placeInFrame(boxEl, next);
    syncEvidence(card, member, next);
    queueZone(path, next);
  }
  return true;
}

// Members still showing a suggested zone (cropped/trashed files have none to hide).
function zonedMembers(g) {
  const deleted = deletedSet(g);
  return (g?.members || []).filter(
    (member) => !deleted.has(member.path)
      && Boolean(effectiveBox(member))
      && (member.crop_state || "pending") !== "cropped",
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

function hasScreenshotProvenance(member) {
  if ((member?.crop_provenance || []).includes("chrome")) return true;
  const box = effectiveBox(member) || member?.crop_suggested;
  return Boolean(box?.chrome?.length);
}

function filterCropMembers(g, members) {
  const deleted = deletedSet(g);
  if (activeCropFilter === "pending") {
    return members.filter(
      (m) => (m.crop_state || "pending") === "pending" && !deleted.has(m.path),
    );
  }
  if (activeCropFilter === "images") {
    return members.filter((m) => m.media_type === "image" || m.media_type === "gif");
  }
  if (activeCropFilter === "videos") {
    return members.filter((m) => m.media_type === "video");
  }
  if (activeCropFilter === "screenshots") {
    return members.filter((m) => hasScreenshotProvenance(m));
  }
  if (activeCropFilter === "clear_bars") {
    return members.filter(
      (m) => (m.crop_state || "pending") === "pending"
        && !deleted.has(m.path)
        && !hasScreenshotProvenance(m)
        && Boolean(effectiveBox(m)),
    );
  }
  return members;
}

function filteredPendingMembers(g) {
  const deleted = deletedSet(g);
  return filterCropMembers(g, g?.members || []).filter(
    (m) => (m.crop_state || "pending") === "pending" && !m.protected && !deleted.has(m.path),
  );
}

function styleCropFilters(wrap) {
  wrap.style.display = "flex";
  wrap.style.flexWrap = "wrap";
  wrap.style.alignItems = "center";
  wrap.style.gap = "6px";
  wrap.style.margin = "0 0 6px 0";
  wrap.querySelectorAll(".crop-filter-chip, #btnSkipAllCrops").forEach((btn) => {
    btn.style.minBlockSize = "26px";
    btn.style.minHeight = "26px";
    btn.style.padding = "2px 10px";
    btn.style.fontSize = "12px";
    btn.style.borderRadius = "999px";
  });
}

function syncCropFilters(g) {
  const wrap = $("cropFilters");
  if (!wrap) return;
  const show = Boolean(g && g.kind === "crop");
  wrap.hidden = !show;
  if (!show) {
    wrap.style.display = "";
    return;
  }
  styleCropFilters(wrap);
  wrap.querySelectorAll(".crop-filter-chip").forEach((chip) => {
    const active = chip.dataset.cropFilter === activeCropFilter;
    chip.setAttribute("aria-pressed", String(active));
    chip.classList.toggle("active", active);
    chip.classList.toggle("chosen", active);
    chip.style.background = active ? "var(--lt-ink)" : "";
    chip.style.borderColor = active ? "var(--lt-ink)" : "";
    chip.style.color = active ? "var(--lt-table)" : "";
  });
  const skipBtn = $("btnSkipAllCrops");
  if (skipBtn) {
    const remaining = filteredPendingMembers(g).length;
    skipBtn.hidden = remaining === 0;
    skipBtn.style.marginLeft = "auto";
    if (!skipAllBusy) {
      skipBtn.disabled = remaining === 0;
      skipBtn.textContent = remaining ? `Skip ${remaining} remaining` : "Skip remaining";
    }
  }
}

function syncRecheckButton(g) {
  const btn = $("btnRecheckCropDups");
  if (!btn) return;
  const croppedCount = (g?.members || []).filter((m) => m.crop_state === "cropped").length;
  btn.hidden = !g || g.kind !== "crop" || croppedCount === 0;
  if (!recheckBusy && !btn.hidden) {
    btn.disabled = false;
    btn.textContent = "Check cropped for duplicates";
  }
}

function hideCropHeaderControls() {
  const filters = $("cropFilters");
  if (filters) {
    filters.hidden = true;
    filters.style.display = "";
  }
  const skipBtn = $("btnSkipAllCrops");
  if (skipBtn) skipBtn.hidden = true;
  const recheckBtn = $("btnRecheckCropDups");
  if (recheckBtn) recheckBtn.hidden = true;
}

function syncCropPagination(totalFiltered, pageStart, shownCount) {
  const show = totalFiltered > CROP_PAGE_SIZE;
  const pageCount = Math.max(1, Math.ceil(totalFiltered / CROP_PAGE_SIZE));
  for (const id of ["memberPagination", "memberPaginationBottom"]) {
    const bar = $(id);
    if (!bar) continue;
    bar.hidden = !show;
    if (!show) continue;
    const prev = bar.querySelector(".member-prev");
    const next = bar.querySelector(".member-next");
    const summary = bar.querySelector(".member-page-summary");
    if (prev) {
      prev.hidden = false;
      prev.disabled = cropPage <= 0;
    }
    if (next) {
      next.hidden = false;
      next.disabled = cropPage >= pageCount - 1;
    }
    if (summary) {
      summary.textContent = shownCount
        ? `${pageStart + 1}–${pageStart + shownCount} of ${totalFiltered}`
        : `0 of ${totalFiltered}`;
    }
  }
}

function updateCropMeta(g) {
  const members = g.members || [];
  const cropped = members.filter((m) => m.crop_state === "cropped").length;
  const skipped = members.filter((m) => m.crop_state === "skipped").length;
  const pending = pendingMembers(g).length;
  const croppable = croppableMembers(g).length;
  $("detailMeta").textContent =
    `${pending} to review · ${cropped} cropped · ${skipped} skipped · originals go to the Trash, so every crop can be undone`;
  const all = $("btnCropAll");
  all.hidden = croppable === 0;
  if (!all.disabled) all.textContent = `Crop all ${croppable} remaining`;
  const levelAll = $("cropLevelAll");
  if (levelAll && document.activeElement !== levelAll) {
    levelAll.value = g.crop_level || "balanced";
  }
  syncCropZonesButton(g);
  syncCropFilters(g);
  syncRecheckButton(g);
}

function renderCropReview(g) {
  syncAdjustmentScan();
  const box = $("members");
  // Hidden zones and filter/page belong to the review on screen; a different
  // pile starts with every zone showing on page 1 under "all".
  if (box.dataset.cropGroup && box.dataset.cropGroup !== g.id) {
    hiddenZones.clear();
    activeCropFilter = "all";
    cropPage = 0;
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
  ["memberSort", "memberModified", "memberPeople"].forEach((id) => {
    const el = $(id);
    if (el) el.hidden = true;
  });
  const allMembers = g.members || [];
  const filtered = filterCropMembers(g, allMembers);
  const totalPages = Math.max(1, Math.ceil(filtered.length / CROP_PAGE_SIZE));
  cropPage = Math.max(0, Math.min(totalPages - 1, cropPage));
  const pageStart = cropPage * CROP_PAGE_SIZE;
  const members = filtered.slice(pageStart, pageStart + CROP_PAGE_SIZE);

  syncCropPagination(filtered.length, pageStart, members.length);
  state.memberFocus = members.length
    ? Math.max(0, Math.min(members.length - 1, state.memberFocus))
    : 0;
  state.lightboxItems = filtered.map((member) => ({
    path: member.path,
    mediaType: member.media_type,
    kind: g.kind,
    size: member.size,
    width: member.width,
    height: member.height,
    mtime: member.mtime,
  }));
  updateCropMeta(g);
  syncCropEditor(g);
  if (!members.length) {
    box.innerHTML = allMembers.length
      ? '<div class="triage-empty">No matching crop candidates for this filter.</div>'
      : '<div class="triage-empty">No files to crop.</div>';
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
      card.dataset.index = String(index);
      card.dataset.lightboxIndex = String(pageStart + index);
      // Never rebuild a card mid-drag: its zone holds the pointer capture.
      if (card.classList.contains("crop-dragging")) return;
      if (card.dataset.signature !== cardSignature(member, g)) {
        replaceCard(card, buildCard(g, member, index, pageStart + index));
      }
    });
    markFocused();
    return;
  }
  box.replaceChildren(...members.map((member, index) => buildCard(g, member, index, pageStart + index)));
  box.dataset.cropGroup = g.id;
}

function markFocused() {
  $("members").querySelectorAll(":scope > .crop-card").forEach((card) => {
    card.classList.toggle("focused", Number(card.dataset.index) === state.memberFocus);
  });
}

// Only keyboard moves scroll, and only when the card is out of view: a click
// must never move the grid out from under the pointer. When moving via
// keyboard inside an active grid, real DOM focus moves onto the card too.
function focusCard(index, { reveal = false, focus = reveal } = {}) {
  const cards = [...$("members").querySelectorAll(":scope > .crop-card")];
  if (!cards.length) return;
  state.memberFocus = Math.max(0, Math.min(cards.length - 1, index));
  markFocused();
  const targetCard = cards[state.memberFocus];
  if (reveal) revealCard(targetCard);
  if (
    focus
    && !$("results")?.hidden
    && !$("detailBody")?.hidden
    && !isOverlayOpen($("lightbox"))
    && !isOverlayOpen($("cropEditor"))
  ) {
    targetCard.querySelector(".crop-thumb")?.focus({ preventScroll: true });
  }
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
  const visible = filterCropMembers(updated, updated.members || [])
    .slice(cropPage * CROP_PAGE_SIZE, (cropPage + 1) * CROP_PAGE_SIZE);
  const from = visible.findIndex((member) => member.path === path);
  const next = visible.findIndex((member, index) => index > from && member.crop_state === "pending");
  const fallback = visible.findIndex((member) => member.crop_state === "pending");
  const target = next >= 0 ? next : fallback >= 0 ? fallback : from;
  if (target >= 0) focusCard(target, { reveal });
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
  if (isOverlayOpen($("toast")) && $("toastMessage")?.textContent === cropUndoMessage(shown)) hideToast();
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
    if (local) {
      const keepWhole = coversFrame(local);
      if (!await saveAdjustment(path, local, g, scanId, { keepWhole })) return;
    }
    if (state.scanId !== scanId || currentGroup()?.id !== g.id) return;
    const member = (currentGroup()?.members || []).find((m) => m.path === path);
    if (!effectiveBox(member)) {
      toast("Nothing to cut yet — adjust the crop zone or pick another level");
      return false;
    }
    const zone = localAdjustments.get(path) || (isAdjusted(member) ? effectiveBox(member) : null);
    const updated = await post("/api/crop/apply", {
      group_id: g.id, path, scan_id: scanId, ...(zone && !coversFrame(zone) ? { box: edgesOf(zone) } : {}),
    });
    if (state.scanId !== scanId) return false;
    applyUpdate(updated);
    advanceFocus(updated, path, { reveal: fromKeyboard });
    dismissCropUndoToast();
    toast(cropUndoMessage(path), "ok", {
      actionLabel: "Undo",
      onAction: () => undoCrop(currentGroup() || updated, path),
    });
    cropUndoToastPath = path;
    return true;
  } catch (error) {
    if (state.scanId === scanId) toast(error.message || "Could not crop that file", "error");
  } finally {
    busy.delete(path);
    markCropping(path, false);
  }
}

// Show one file at another level — or, without a path, the whole review.
// Explicit level switches clear manual adjustments on the affected file(s)
// so the newly chosen level's zone immediately shows on screen.
async function chooseCropLevel(g, path, level) {
  if (!g || !CROP_LEVELS.includes(level)) return;
  if (path && busy.has(path)) return;
  const scanId = state.scanId;
  const member = path
    ? (g.members || []).find((m) => m.path === path)
    : null;
  if (member && (member.crop_level || "balanced") === level && !isAdjusted(member)) return;
  const affected = path
    ? [path]
    : (g.members || []).filter((m) => m.crop_state !== "cropped").map((m) => m.path);
  for (const targetPath of affected) {
    clearPendingAdjustments(targetPath);
    localAdjustments.delete(targetPath);
  }
  if (path) busy.add(path);
  try {
    const updated = await post("/api/crop/level", {
      group_id: g.id, scan_id: scanId, level, ...(path ? { path } : {}),
    });
    if (state.scanId !== scanId) return;
    for (const targetPath of affected) {
      localAdjustments.delete(targetPath);
    }
    applyUpdate(updated);
  } catch (error) {
    if (state.scanId === scanId) {
      toast(error.message || "Could not change the crop level", "error");
      if (!path) updateCropMeta(g); // put the header select back
    }
  } finally {
    if (path) busy.delete(path);
  }
}

$("cropLevelAll")?.addEventListener("change", (event) => {
  chooseCropLevel(currentGroup(), null, event.currentTarget.value);
});

async function skipCrop(g, path, skip, { fromKeyboard = false } = {}) {
  if (!g || !path || busy.has(path)) return;
  const scanId = state.scanId;
  clearPendingAdjustments(path);
  localAdjustments.delete(path);
  busy.add(path);
  try {
    const updated = await post("/api/crop/skip", { group_id: g.id, path, scan_id: scanId, skip });
    if (state.scanId !== scanId) return false;
    applyUpdate(updated);
    if (skip) advanceFocus(updated, path, { reveal: fromKeyboard });
    return true;
  } catch (error) {
    if (state.scanId === scanId) toast(error.message, "error");
  } finally {
    busy.delete(path);
  }
}

async function annotateSkip(g, path, reason, note) {
  if (!g || !path || busy.has(path)) return;
  const scanId = state.scanId;
  busy.add(path);
  try {
    const updated = await post("/api/crop/annotate-skip", {
      group_id: g.id, path, scan_id: scanId, reason, note,
    });
    if (state.scanId !== scanId) return;
    applyUpdate(updated);
  } catch (error) {
    if (state.scanId === scanId) toast(error.message || "Could not save that reason", "error");
  } finally {
    busy.delete(path);
  }
}

async function undoCrop(g, path) {
  if (!g || !path || undoing.has(path)) return;
  const scanId = state.scanId;
  undoing.add(path);
  const card = document.querySelector(`.crop-card[data-path="${CSS.escape(path)}"]`);
  const undoBtn = card?.querySelector(".crop-undo");
  if (undoBtn) undoBtn.disabled = true;
  try {
    let waited = 0;
    while (busy.has(path) && waited < 5000) {
      await sleep(40);
      waited += 40;
      if (state.scanId !== scanId) return;
    }
    if (busy.has(path)) return;
    const liveGroup = currentGroup()?.id === g.id ? currentGroup() : g;
    const liveMember = (liveGroup?.members || []).find((m) => m.path === path);
    if (liveMember && liveMember.crop_state !== "cropped") return;
    busy.add(path);
    try {
      const updated = await post("/api/crop/undo", { group_id: g.id, path, scan_id: scanId });
      if (state.scanId !== scanId) return;
      bumpAdjustEpoch(path);
      localAdjustments.delete(path);
      applyUpdate(updated);
      dismissCropUndoToast(path);
      toast(`Restored the original ${basename(path)}`, "ok");
    } finally {
      busy.delete(path);
    }
  } catch (error) {
    if (state.scanId === scanId) toast(error.message, "error");
  } finally {
    undoing.delete(path);
    if (undoBtn?.isConnected) undoBtn.disabled = false;
  }
}

async function resolveTrashTargetGroup(path) {
  const findGroup = () => {
    const candidates = (state.allGroups || []).filter(
      (group) => TRASH_ELIGIBLE_KINDS.has(group.kind)
        && (group.members || []).some((m) => m.path === path),
    );
    return candidates.find((group) => group.kind === "all_files") || candidates[0] || null;
  };
  let target = findGroup();
  if (target) return target;
  try {
    const res = await api("/api/groups?kind=all_files");
    for (const group of res.groups || []) {
      if ((group.members || []).some((m) => m.path === path)) return group;
    }
  } catch {
    /* ignore */
  }
  return findGroup();
}

async function trashCropMember(g, path) {
  if (!g || !path || busy.has(path)) return false;
  const scanId = state.scanId;
  const targetGroup = await resolveTrashTargetGroup(path);
  if (state.scanId !== scanId) return false;
  if (!targetGroup) {
    toast("Cannot trash this file here — open it in Files to move it to Trash", "error");
    return false;
  }
  clearPendingAdjustments(path);
  localAdjustments.delete(path);
  busy.add(path);
  try {
    const updatedTarget = await post("/api/review-candidate/delete", {
      group_id: targetGroup.id,
      path,
      scan_id: scanId,
      dry_run: false,
    });
    if (state.scanId !== scanId) return false;
    patchGroup(updatedTarget);
    const liveCrop = state.allGroups.find((item) => item.id === g.id) || g;
    const nextCrop = {
      ...liveCrop,
      deleted_paths: [...new Set([...(liveCrop.deleted_paths || []), path])],
    };
    applyUpdate(nextCrop);
    toast(`Moved ${basename(path)} to Trash`, "ok", {
      actionLabel: "Undo",
      onAction: () => undoTrashCropMember(currentGroup() || nextCrop, targetGroup.id, path),
    });
    return true;
  } catch (error) {
    if (state.scanId === scanId) toast(error.message || "Could not move that file to Trash", "error");
    return false;
  } finally {
    busy.delete(path);
  }
}

async function undoTrashCropMember(g, targetGroupId, path) {
  if (!path || undoing.has(path)) return false;
  const scanId = state.scanId;
  const resolvedId = targetGroupId || (await resolveTrashTargetGroup(path))?.id;
  if (!resolvedId || state.scanId !== scanId) return false;
  undoing.add(path);
  try {
    let waited = 0;
    while (busy.has(path) && waited < 5000) {
      await sleep(40);
      waited += 40;
      if (state.scanId !== scanId) return false;
    }
    if (busy.has(path)) return false;
    busy.add(path);
    try {
      const updatedTarget = await post("/api/review-candidate/undo", {
        group_id: resolvedId,
        path,
        scan_id: scanId,
      });
      if (state.scanId !== scanId) return false;
      patchGroup(updatedTarget);
      const liveCrop = (g && state.allGroups.find((item) => item.id === g.id)) || g;
      if (liveCrop) {
        const nextCrop = {
          ...liveCrop,
          deleted_paths: (liveCrop.deleted_paths || []).filter((p) => p !== path),
        };
        applyUpdate(nextCrop);
      }
      toast(`Restored ${basename(path)} from Trash`, "ok");
      return true;
    } finally {
      busy.delete(path);
    }
  } catch (error) {
    if (state.scanId === scanId) toast(error.message || "Could not restore from Trash", "error");
    return false;
  } finally {
    undoing.delete(path);
  }
}

async function cropAllRemaining() {
  const g = currentGroup();
  if (!g || g.kind !== "crop") return;
  const initialPending = croppableMembers(g);
  if (!initialPending.length) return;
  const scanId = state.scanId;
  const videos = initialPending.filter((member) => member.media_type === "video").length;
  const ok = await confirmModal({
    title: `Crop ${initialPending.length} file${initialPending.length === 1 ? "" : "s"}?`,
    body: `<p>Each file is cropped to the zone shown on its card (dragged zones included) and rewritten in place. The originals go to the Trash, and each crop can be undone from its card.</p>${videos ? `<p>${videos} video${videos === 1 ? "" : "s"} will be re-encoded, which can take a while.</p>` : ""}<p class="muted">Skipped and already cropped files are left alone.</p>`,
    confirmLabel: `Crop ${initialPending.length}`,
    danger: false,
  });
  if (ok !== true || state.scanId !== scanId) return;
  const live = currentGroup();
  if (!live || live.id !== g.id || live.kind !== "crop") return;
  const pending = croppableMembers(live);
  if (!pending.length) return;
  if (pending.some((member) => busy.has(member.path))) {
    toast("Another crop operation is still running — please wait", "error");
    return;
  }
  for (const member of pending) busy.add(member.path);
  const btn = $("btnCropAll");
  try {
    btn.disabled = true;
    btn.textContent = `Cropping ${pending.length} file${pending.length === 1 ? "" : "s"}…`;
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
      if (local && !await saveAdjustment(path, local, live, scanId, { keepWhole: coversFrame(local) })) {
        return;
      }
    }
    if (state.scanId !== scanId || currentGroup()?.id !== g.id) return;
    const updated = await post("/api/crop/apply-all", { group_id: g.id, scan_id: scanId });
    if (state.scanId !== scanId) return;
    applyUpdate(updated);
    dismissCropUndoToast();
    const failed = (updated.failures || []).length;
    const receiptRef = updated.receipt_id || updated.log_path;
    const toastOpts = receiptRef && updated.cropped_count > 0
      ? {
          actionLabel: "Undo",
          onAction: async () => {
            await undoAction([receiptRef]);
            if (state.scanId === scanId) {
              await loadGroups({ preserveSelection: true });
              const refreshed = state.allGroups.find((item) => item.id === g.id);
              if (refreshed && currentGroup()?.id === g.id) renderCropReview(refreshed);
            }
          },
        }
      : {};
    toast(
      `Cropped ${updated.cropped_count} file${updated.cropped_count === 1 ? "" : "s"}`
        + (failed ? ` · ${failed} could not be cropped (${updated.failures[0].error})` : ""),
      failed ? "error" : "ok",
      toastOpts,
    );
  } catch (error) {
    if (state.scanId === scanId) toast(error.message, "error");
  } finally {
    for (const member of pending) busy.delete(member.path);
    btn.disabled = false;
    const current = currentGroup();
    if (current?.id === g.id && current.kind === "crop") updateCropMeta(current);
  }
}

$("btnCropAll")?.addEventListener("click", cropAllRemaining);

async function skipAllRemaining() {
  const g = currentGroup();
  if (!g || g.kind !== "crop" || skipAllBusy) return;
  const initialTargets = filteredPendingMembers(g);
  if (!initialTargets.length) return;
  const scanId = state.scanId;
  const ok = await confirmModal({
    title: `Skip ${initialTargets.length} remaining suggestion${initialTargets.length === 1 ? "" : "s"}?`,
    body: "<p>Every remaining pending file in this view will be marked skipped. You can reconsider any file from its card later.</p>",
    confirmLabel: `Skip ${initialTargets.length}`,
    danger: false,
  });
  if (ok !== true || state.scanId !== scanId) return;
  const live = currentGroup();
  if (!live || live.id !== g.id || live.kind !== "crop") return;
  const targets = filteredPendingMembers(live);
  if (!targets.length) return;
  if (targets.some((m) => busy.has(m.path))) {
    toast("Another crop operation is still running — please wait", "error");
    return;
  }
  skipAllBusy = true;
  for (const m of targets) {
    busy.add(m.path);
    clearPendingAdjustments(m.path);
    localAdjustments.delete(m.path);
  }
  const btn = $("btnSkipAllCrops");
  let skipped = 0;
  let failed = 0;
  let latest = live;
  try {
    if (btn) btn.disabled = true;
    for (let i = 0; i < targets.length; i += 1) {
      if (state.scanId !== scanId || currentGroup()?.id !== g.id) break;
      if (btn) btn.textContent = `Skipping… (${i + 1}/${targets.length})`;
      try {
        latest = await post("/api/crop/skip", {
          group_id: g.id, path: targets[i].path, scan_id: scanId, skip: true,
        });
        skipped += 1;
      } catch {
        failed += 1;
      }
    }
    if (state.scanId !== scanId) return;
    if (latest) applyUpdate(latest);
    dismissCropUndoToast();
    toast(
      `Skipped ${skipped} crop suggestion${skipped === 1 ? "" : "s"}`
        + (failed ? ` · ${failed} failed` : ""),
      failed ? "error" : "ok",
    );
  } finally {
    skipAllBusy = false;
    for (const m of targets) busy.delete(m.path);
    const current = currentGroup();
    if (current?.id === g.id && current.kind === "crop") syncCropFilters(current);
  }
}

$("btnSkipAllCrops")?.addEventListener("click", skipAllRemaining);

async function recheckCropDuplicates() {
  const g = currentGroup();
  if (!g || g.kind !== "crop" || recheckBusy) return;
  const scanId = state.scanId;
  const btn = $("btnRecheckCropDups");
  recheckBusy = true;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Checking…";
  }
  try {
    const res = await post("/api/crop/recheck-duplicates", { group_id: g.id, scan_id: scanId });
    if (state.scanId !== scanId) return;
    const newCount = (res.new_groups || []).length;
    const updCount = (res.updated_groups || []).length;
    if (newCount || updCount) {
      await loadGroups({ preserveSelection: true });
      const refreshed = state.allGroups.find((item) => item.id === g.id);
      if (refreshed && currentGroup()?.id === g.id) renderCropReview(refreshed);
    } else {
      for (const upd of res.updated_groups || []) patchGroup(upd);
      renderTabCounts();
      applyResultControls();
    }
    const checked = res.checked || 0;
    dismissCropUndoToast();
    if (!checked) {
      toast("No cropped files to check");
    } else if (!res.exact_matches && !res.similar_matches) {
      toast(`Checked ${checked} cropped file${checked === 1 ? "" : "s"} — no new duplicates found`);
    } else {
      toast(
        `Found ${res.exact_matches || 0} exact and ${res.similar_matches || 0} similar match${(res.exact_matches || 0) + (res.similar_matches || 0) === 1 ? "" : "es"} across ${checked} cropped file${checked === 1 ? "" : "s"}`,
        "ok",
      );
    }
  } catch (error) {
    if (state.scanId === scanId) toast(error.message || "Could not recheck cropped files", "error");
  } finally {
    recheckBusy = false;
    const current = currentGroup();
    if (current?.kind === "crop") syncRecheckButton(current);
  }
}

$("btnRecheckCropDups")?.addEventListener("click", recheckCropDuplicates);

$("cropFilters")?.addEventListener("click", (event) => {
  const chip = event.target.closest(".crop-filter-chip");
  if (!chip) return;
  const nextFilter = chip.dataset.cropFilter;
  if (!nextFilter || nextFilter === activeCropFilter) return;
  activeCropFilter = nextFilter;
  cropPage = 0;
  state.memberFocus = 0;
  const g = currentGroup();
  if (g?.kind === "crop") renderCropReview(g);
});

for (const id of ["memberPagination", "memberPaginationBottom"]) {
  const bar = $(id);
  bar?.querySelector(".member-prev")?.addEventListener("click", () => {
    const g = currentGroup();
    if (g?.kind !== "crop" || cropPage <= 0) return;
    cropPage -= 1;
    state.memberFocus = 0;
    renderCropReview(g);
    $("main")?.scrollTo?.({ top: 0, behavior: "auto" });
  });
  bar?.querySelector(".member-next")?.addEventListener("click", () => {
    const g = currentGroup();
    if (g?.kind !== "crop") return;
    const total = filterCropMembers(g, g.members || []).length;
    const pageCount = Math.max(1, Math.ceil(total / CROP_PAGE_SIZE));
    if (cropPage >= pageCount - 1) return;
    cropPage += 1;
    state.memberFocus = 0;
    renderCropReview(g);
    $("main")?.scrollTo?.({ top: 0, behavior: "auto" });
  });
}

// Hide crop-specific header/filter controls when switching away to another group kind.
const membersEl = $("members");
if (membersEl && typeof MutationObserver === "function") {
  new MutationObserver(() => {
    if (!membersEl.classList.contains("crop-grid")) hideCropHeaderControls();
  }).observe(membersEl, { attributes: true, attributeFilter: ["class"] });
}

function gridColumnCount(cards) {
  if (cards.length <= 1) return 1;
  const firstTop = cards[0].getBoundingClientRect().top;
  let cols = 0;
  for (const card of cards) {
    if (Math.abs(card.getBoundingClientRect().top - firstTop) <= 6) cols += 1;
    else break;
  }
  return Math.max(1, cols);
}

// Keyboard: c crops the focused file, x skips it, e opens its zone in the
// large editor, v toggles its crop zone, 1/2/3 switch its aggressiveness
// level, ←/→/↑/↓ move between cards. On a focused zone itself, arrows move the
// zone, Shift+arrows move its bottom and right edges, Alt+arrows (taken
// on the zone, since the page's key map leaves Alt chords alone) its top
// and left edges, and Esc steps back out to the card thumbnail.
function handleCropKey(event) {
  const g = currentGroup();
  if (!g || g.kind !== "crop") return false;
  const boxEl = event.target?.closest?.(".crop-box.editable");
  if (boxEl && event.key === "Escape") {
    const card = boxEl.closest(".crop-card");
    card?.querySelector(".crop-thumb")?.focus({ preventScroll: true });
    if (!card) boxEl.blur();
    return true;
  }
  if (boxEl && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
    return nudgeCropZone(boxEl, event);
  }
  const visible = filterCropMembers(g, g.members || [])
    .slice(cropPage * CROP_PAGE_SIZE, (cropPage + 1) * CROP_PAGE_SIZE);
  const member = visible[state.memberFocus];
  const deleted = deletedSet(g);
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (key === "c" && member?.crop_state === "pending" && !deleted.has(member.path)) {
    if (member.protected) {
      toast("This file is in a protected library");
    } else if (!effectiveBox(member)) {
      toast("Nothing to cut yet — adjust the crop zone or pick another level");
    } else {
      applyCrop(g, member.path, { fromKeyboard: true });
    }
    return true;
  }
  if (key === "x" && member?.crop_state === "pending" && !deleted.has(member.path)) {
    skipCrop(g, member.path, true, { fromKeyboard: true });
    return true;
  }
  if (key === "e" && member?.crop_state === "pending" && !member.protected && !deleted.has(member.path)) {
    openCropEditor(member.path);
    return true;
  }
  if (key === "v" && member && effectiveBox(member) && member.crop_state !== "cropped" && !deleted.has(member.path)) {
    toggleCropZone(member.path);
    return true;
  }
  if (["1", "2", "3"].includes(key) && member && member.crop_state !== "cropped" && !deleted.has(member.path)) {
    chooseCropLevel(g, member.path, CROP_LEVELS[Number(key) - 1]);
    return true;
  }
  if (key === "ArrowRight" || key === "ArrowLeft") {
    focusCard(state.memberFocus + (key === "ArrowRight" ? 1 : -1), { reveal: true });
    return true;
  }
  if (key === "ArrowDown" || key === "ArrowUp") {
    const cards = [...$("members").querySelectorAll(":scope > .crop-card")];
    if (!cards.length) return true;
    const cols = gridColumnCount(cards);
    const next = state.memberFocus + (key === "ArrowDown" ? cols : -cols);
    if (next >= 0 && next < cards.length) focusCard(next, { reveal: true });
    return true;
  }
  return false;
}

export {
  CROP_HANDLES, CROP_LEVELS, applyCrop, bindZone, chooseCropLevel, clearPendingAdjustments,
  commitZone, coversFrame, cropWarnings, deriveBox, effectiveBox, evidenceFor, formatAspectRatio,
  handleCropKey, levelSwitchHtml, nudgedBox, placeInFrame, queueZone, renderCropReview,
  resetCropZone, sameEdges, skipCrop, snapEdges, syncCardZone, trashCropMember, updateCropMeta,
  zoneBox,
};
