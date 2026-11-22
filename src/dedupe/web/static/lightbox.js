// The full-screen comparison overlay.

import { api } from "./api.js";
import { boardActive, refreshBoardRow } from "./board.js";
import { applyCrop, skipCrop } from "./crop.js";
import { openCropEditor } from "./cropeditor.js";
import {
  applyResultControls, loadGroups, selectionFiltersActive, updateGroupListItem,
} from "./groups.js";
import {
  renderMembers, reviewCandidate, selectGroup, setMemberSelected, trashReviewCandidate,
} from "./members.js";
import {
  currentGroup, isDecisionReview, isIndependentReview, isPagedIndependentReview,
  markGroupTouched, patchGroup,
} from "./model.js";
import { scheduleRender } from "./render.js";
import { noteReviewPair } from "./review-metrics.js";
import { state } from "./state.js";
import { decideSwipe, swipeActive, undoSwipe } from "./swipe.js";
import {
  $, formatBytes, formatMtime, isOverlayOpen,
  promoteToastTopLayer, syncAppInert, toast, withMediaVersion,
} from "./util.js";

const PLAYBACK_RATE_KEY = "dedupe.videoPlaybackRate";
const LOSSLESS_FORMATS = new Set(["PNG", "TIFF", "TIF", "BMP", "RAW", "CR2", "NEF", "ARW", "DNG"]);

// Focus is moved into the lightbox on open and restored on close.
let previousFocus = null;
let previousFocusPath = null;

// Continuous zoom state: 0 = Fit, >0 = pixel scale factor (1, 2, 4, or pinch).
let zoomFactor = 0;
let savedPan = { rx: 0.5, ry: 0.5 };
let splitPct = 50;
let flickerActive = false;
let diffToken = 0;

// Cached /api/media-details responses, keyed by `${path}:${version}`.
const mediaDetailsCache = new Map();

function previewUrl(path, version) {
  return withMediaVersion(`/api/thumbnail?path=${encodeURIComponent(path)}&variant=preview`, path, version);
}

function fullUrl(path, version) {
  return withMediaVersion(`/api/thumbnail?path=${encodeURIComponent(path)}&variant=full`, path, version);
}

function mediaUrl(path, version) {
  return withMediaVersion(`/api/media?path=${encodeURIComponent(path)}`, path, version);
}

function lightboxOpen() {
  return isOverlayOpen($("lightbox"));
}

function findGroupMember(path) {
  if (!path) return null;
  const group = currentGroup();
  return (group?.members || []).find((m) => m.path === path) || null;
}

function resolveItem(raw) {
  if (!raw) return null;
  const member = findGroupMember(raw.path);
  const group = currentGroup();
  return {
    ...raw,
    media_version: raw.media_version ?? member?.media_version ?? null,
    match_label: raw.match_label ?? raw.matchLabel ?? member?.match_label ?? null,
    similarityPercent: raw.similarityPercent ?? member?.similarity_percent ?? null,
    orientationLabel: raw.orientationLabel ?? member?.orientation_label ?? null,
    duration: raw.duration ?? member?.duration ?? null,
    protected: Boolean(raw.protected ?? member?.protected),
    match_basis: raw.match_basis ?? group?.match_basis ?? null,
    crop_state: raw.crop_state ?? member?.crop_state ?? null,
    crop: raw.crop ?? member?.crop ?? null,
    width: raw.width ?? member?.width ?? null,
    height: raw.height ?? member?.height ?? null,
    size: raw.size ?? member?.size ?? null,
    mtime: raw.mtime ?? member?.mtime ?? null,
  };
}

function isZoomed() {
  return zoomFactor > 0;
}

function canComparePair(item, isVideo) {
  return Boolean(
    !isVideo
    && item
    && item.mediaType !== "gif"
    && item.kind === "similar"
    && item.keeper
    && item.keeper !== item.path,
  );
}

function syncZoomSelect(factor) {
  const select = $("lbZoomLevel");
  if (!select) return;
  select.querySelector("option[data-custom-zoom]")?.remove();
  if (factor <= 0) {
    select.value = "fit";
    return;
  }
  const preset = [1, 2, 4].find((v) => Math.abs(v - factor) < 0.02);
  if (preset !== undefined) {
    select.value = String(preset);
    return;
  }
  const opt = new Option(`${Math.round(factor * 100)}%`, String(factor), true, true);
  opt.dataset.customZoom = "1";
  select.append(opt);
}

function captureNormalizedPan() {
  const stack = $("lbImageStack");
  if (!isZoomed() || !stack || stack.scrollWidth <= 0 || stack.scrollHeight <= 0) return null;
  return {
    rx: Math.max(0, Math.min(1, (stack.scrollLeft + stack.clientWidth / 2) / stack.scrollWidth)),
    ry: Math.max(0, Math.min(1, (stack.scrollTop + stack.clientHeight / 2) / stack.scrollHeight)),
  };
}

function applyNormalizedPan(pan = savedPan) {
  const stack = $("lbImageStack");
  if (!isZoomed() || !stack || !pan) return;
  const maxLeft = Math.max(0, stack.scrollWidth - stack.clientWidth);
  const maxTop = Math.max(0, stack.scrollHeight - stack.clientHeight);
  const targetLeft = pan.rx * stack.scrollWidth - stack.clientWidth / 2;
  const targetTop = pan.ry * stack.scrollHeight - stack.clientHeight / 2;
  stack.scrollLeft = Math.max(0, Math.min(maxLeft, targetLeft));
  stack.scrollTop = Math.max(0, Math.min(maxTop, targetTop));
}

function styleOverlayChrome() {
  const badge = $("lbStageBadge");
  if (badge) {
    badge.style.position = "absolute";
    badge.style.top = "10px";
    badge.style.left = "10px";
    badge.style.zIndex = "5";
    badge.style.pointerEvents = "none";
    badge.style.padding = "3px 8px";
    badge.style.borderRadius = "4px";
    badge.style.fontSize = "11px";
    badge.style.fontWeight = "600";
    badge.style.background = "rgba(14, 14, 14, 0.82)";
    badge.style.color = "#F2F2F0";
    badge.style.border = "1px solid rgba(255, 255, 255, 0.22)";
  }
  const pill = $("lbStatusPill");
  if (pill) {
    pill.style.position = "absolute";
    pill.style.top = "10px";
    pill.style.right = "10px";
    pill.style.zIndex = "5";
    pill.style.pointerEvents = "none";
    pill.style.padding = "3px 8px";
    pill.style.borderRadius = "4px";
    pill.style.fontSize = "11px";
    pill.style.fontWeight = "600";
    pill.style.background = "rgba(14, 14, 14, 0.82)";
    pill.style.color = "#F2F2F0";
    pill.style.border = "1px solid rgba(255, 255, 255, 0.22)";
  }
}

function layoutImagePlane() {
  const stack = $("lbImageStack");
  const plane = stack?.querySelector(".lb-image-plane");
  const image = $("lbImage");
  const keeper = $("lbKeeperImage");
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  if (!stack || !plane || !image || !item || item.mediaType === "video") return;

  const isCompare = canComparePair(item, false);
  const mode = $("lbCompareMode")?.value || "overlay";
  const sideMode = isCompare && mode === "side" && !flickerActive;
  const activeZoom = item.mediaType === "image" && isZoomed();

  stack.classList.toggle("zoomed", activeZoom);
  plane.classList.toggle("side-by-side", sideMode);

  const w = Number(item.width) || image.naturalWidth || 0;
  const h = Number(item.height) || image.naturalHeight || 0;

  if (sideMode && !activeZoom) {
    plane.style.width = "100%";
    plane.style.height = "100%";
    plane.style.maxWidth = "";
    plane.style.maxHeight = "";
    plane.style.display = "grid";
    plane.style.gridTemplateColumns = "minmax(0, 1fr) minmax(0, 1fr)";
    plane.style.gap = "12px";
    plane.style.alignItems = "center";
    plane.style.justifyItems = "center";
    for (const [el, col] of [[image, "1 / 1"], [keeper, "1 / 2"]]) {
      el.style.position = "static";
      el.style.inset = "";
      el.style.gridArea = col;
      el.style.width = "100%";
      el.style.height = "100%";
      el.style.maxWidth = "100%";
      el.style.maxHeight = "100%";
      el.style.objectFit = "contain";
    }
    return;
  }

  if (sideMode && activeZoom) {
    const paneW = Math.max(1, Math.round((w || stack.clientWidth / 2 || 400) * zoomFactor));
    const paneH = Math.max(1, Math.round((h || stack.clientHeight || 400) * zoomFactor));
    plane.style.display = "grid";
    plane.style.gridTemplateColumns = `${paneW}px ${paneW}px`;
    plane.style.gap = "12px";
    plane.style.alignItems = "center";
    plane.style.justifyItems = "center";
    plane.style.width = `${paneW * 2 + 12}px`;
    plane.style.height = `${paneH}px`;
    plane.style.maxWidth = "";
    plane.style.maxHeight = "";
    for (const [el, col] of [[image, "1 / 1"], [keeper, "1 / 2"]]) {
      el.style.position = "static";
      el.style.inset = "";
      el.style.gridArea = col;
      el.style.width = `${paneW}px`;
      el.style.height = `${paneH}px`;
      el.style.maxWidth = "none";
      el.style.maxHeight = "none";
      el.style.objectFit = "contain";
    }
    return;
  }

  plane.style.display = "";
  plane.style.gridTemplateColumns = "";
  plane.style.gap = "";
  plane.style.alignItems = "";
  plane.style.justifyItems = "";
  image.style.gridArea = "";
  keeper.style.gridArea = "";

  if (activeZoom && w > 0 && h > 0) {
    const targetW = Math.max(1, Math.round(w * zoomFactor));
    const targetH = Math.max(1, Math.round(h * zoomFactor));
    plane.style.width = `${targetW}px`;
    plane.style.height = `${targetH}px`;
    plane.style.maxWidth = "";
    plane.style.maxHeight = "";
    image.style.position = "";
    image.style.inset = "";
    image.style.width = "100%";
    image.style.height = "100%";
    image.style.maxWidth = "none";
    image.style.maxHeight = "none";
    image.style.objectFit = "contain";
    keeper.style.position = "absolute";
    keeper.style.inset = "0";
    keeper.style.width = "100%";
    keeper.style.height = "100%";
    keeper.style.maxWidth = "none";
    keeper.style.maxHeight = "none";
    keeper.style.objectFit = "contain";
    return;
  }

  // Constrain #lbImage to #lbImageStack's current content box so frame-0
  // layout never lags behind .lb-media container-query sizing, while letting
  // width/height: auto preserve exact aspect ratio inside fit-content plane.
  const maxW = stack.clientWidth > 0 ? `${stack.clientWidth}px` : "100%";
  const maxH = stack.clientHeight > 0 ? `${stack.clientHeight}px` : "100cqh";
  plane.style.width = "fit-content";
  plane.style.height = "fit-content";
  plane.style.maxWidth = maxW;
  plane.style.maxHeight = maxH;
  image.style.position = "";
  image.style.inset = "";
  image.style.width = "auto";
  image.style.height = "auto";
  image.style.maxWidth = maxW;
  image.style.maxHeight = maxH;
  image.style.objectFit = "contain";
  keeper.style.position = "absolute";
  keeper.style.inset = "0";
  keeper.style.width = "100%";
  keeper.style.height = "100%";
  keeper.style.maxWidth = "100%";
  keeper.style.maxHeight = "100%";
  keeper.style.objectFit = "contain";
}

const stackResizeObserver = typeof ResizeObserver === "function"
  ? new ResizeObserver(() => {
    if (lightboxOpen()) layoutImagePlane();
  })
  : null;
if ($("lbImageStack") && stackResizeObserver) {
  stackResizeObserver.observe($("lbImageStack"));
}

function loadFullImageVariant(imgEl, path, version, expectedPath, expectedKeeper = null) {
  const targetSrc = fullUrl(path, version);
  if (imgEl.getAttribute("src") === targetSrc) return;
  const loader = new Image();
  loader.decoding = "async";
  loader.onload = () => {
    const current = resolveItem(state.lightboxItems[state.lightboxIndex]);
    if (!isZoomed() || !current || current.path !== expectedPath) return;
    if (expectedKeeper !== null && current.keeper !== expectedKeeper) return;
    imgEl.src = targetSrc;
    imgEl.dataset.loadedPath = path;
    imgEl.dataset.loadedMode = "full";
    layoutImagePlane();
    requestAnimationFrame(() => applyNormalizedPan(savedPan));
  };
  loader.src = targetSrc;
}

function setZoomLevel(nextFactor, { anchorClientX = null, anchorClientY = null } = {}) {
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  const clamped = item && item.mediaType === "image"
    ? (nextFactor > 0 ? Math.max(0.5, Math.min(4, Number(nextFactor))) : 0)
    : 0;
  const prevZoomed = isZoomed();
  const stack = $("lbImageStack");
  const plane = stack?.querySelector(".lb-image-plane");

  if (anchorClientX != null && anchorClientY != null && plane) {
    const rect = plane.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      savedPan = {
        rx: Math.max(0, Math.min(1, (anchorClientX - rect.left) / rect.width)),
        ry: Math.max(0, Math.min(1, (anchorClientY - rect.top) / rect.height)),
      };
    }
  } else if (prevZoomed) {
    const currentPan = captureNormalizedPan();
    if (currentPan) savedPan = currentPan;
  } else {
    savedPan = { rx: 0.5, ry: 0.5 };
  }

  zoomFactor = clamped;
  const zoomedNow = isZoomed();
  const zoomButton = $("lbZoom");
  if (zoomButton) {
    zoomButton.setAttribute("aria-pressed", zoomedNow ? "true" : "false");
    zoomButton.textContent = zoomedNow ? "Zoom out" : "Zoom";
  }
  syncZoomSelect(zoomFactor);

  if (!item || item.mediaType !== "image") return;
  const image = $("lbImage");
  layoutImagePlane();
  syncCompareTools(item, false);

  if (!zoomedNow) {
    const targetPreview = previewUrl(item.path, item.media_version);
    if (image.getAttribute("src") !== targetPreview) {
      image.src = targetPreview;
      image.dataset.loadedPath = item.path;
      image.dataset.loadedMode = "preview";
    }
    if (stack) {
      stack.scrollLeft = 0;
      stack.scrollTop = 0;
    }
    return;
  }

  loadFullImageVariant(image, item.path, item.media_version, item.path, null);
  requestAnimationFrame(() => applyNormalizedPan(savedPan));
}

function setZoom(on) {
  setZoomLevel(on ? 1 : 0);
}

function openLightbox(index) {
  if (!state.lightboxItems.length) return;
  document.querySelectorAll("#members video").forEach((video) => video.pause());
  previousFocus = document.activeElement;
  previousFocusPath = (
    previousFocus?.closest?.("#members .card")
    || document.querySelector("#members .card.focused")
  )?.dataset.path || null;
  zoomFactor = 0;
  savedPan = { rx: 0.5, ry: 0.5 };
  flickerActive = false;
  state.lightboxIndex = Math.max(0, Math.min(index, state.lightboxItems.length - 1));
  const lb = $("lightbox");
  updateLightbox();
  if (!lightboxOpen()) lb.showPopover?.();
  syncAppInert();
  promoteToastTopLayer();
  layoutImagePlane();
  requestAnimationFrame(() => layoutImagePlane());
  $("lbClose").focus();
}

function closeLightbox() {
  diffToken += 1;
  setFlicker(false);
  if (isZoomed()) setZoomLevel(0);
  const video = $("lbVideo");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
  const lb = $("lightbox");
  if (lightboxOpen()) lb.hidePopover?.();
  syncAppInert();
  const focusTarget = previousFocus?.classList?.contains("enlarge-candidate")
    ? ".enlarge-candidate"
    : ".thumb-wrap";
  const refreshedMember = previousFocusPath
    ? $("members")?.querySelector(
      `.card[data-path="${CSS.escape(previousFocusPath)}"] ${focusTarget}`,
    )
    : null;
  const restoreFocus = refreshedMember
    || (previousFocus?.isConnected ? previousFocus : null)
    || document.querySelector("#similarBoard:not([hidden]) .board-tile.focused .board-thumb")
    || document.querySelector("#members .card.focused .thumb-wrap")
    || $("main");
  restoreFocus?.focus?.();
  previousFocus = null;
  previousFocusPath = null;
}

function stepLightbox(delta) {
  const count = state.lightboxItems.length;
  if (count <= 1) return;
  if (isZoomed()) {
    const pan = captureNormalizedPan();
    if (pan) savedPan = pan;
  }
  setFlicker(false);
  // Wrap around: past the last item returns to the first, and back.
  state.lightboxIndex = (state.lightboxIndex + delta + count) % count;
  updateLightbox();
}

function prefetchLightboxNeighbors() {
  const count = state.lightboxItems.length;
  const seen = new Set([state.lightboxItems[state.lightboxIndex]?.path]);
  for (const delta of [-1, 1, 2, 3]) {
    const neighbor = resolveItem(state.lightboxItems[(state.lightboxIndex + delta + count) % count]);
    if (!neighbor || neighbor.mediaType === "video" || seen.has(neighbor.path)) continue;
    seen.add(neighbor.path);
    const image = new Image();
    image.decoding = "async";
    image.src = previewUrl(neighbor.path, neighbor.media_version);
  }
}

function lightboxDetails(item) {
  const parts = [];
  const width = Number(item.width);
  const height = Number(item.height);
  if (Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
    parts.push(`${width}×${height}`);
  }
  if (Number.isFinite(Number(item.size))) parts.push(formatBytes(item.size));
  if (item.duration != null) parts.push(`${Number(item.duration).toFixed(1)}s`);
  if (item.mtime) parts.push(formatMtime(item.mtime));
  if (item.kind === "similar") {
    if (item.keeper && item.keeper === item.path) {
      parts.push(swipeActive() ? "Reference copy" : "Suggested keeper");
    } else if (item.match_label) {
      parts.push(item.match_label);
    } else if (item.match_basis === "video_version") {
      parts.push("Matching video section");
    } else if (item.match_basis === "semantic") {
      parts.push("Look-alike match");
    } else {
      const similarity = item.similarityPercent == null ? null : Number(item.similarityPercent);
      if (Number.isFinite(similarity)) {
        parts.push(`${similarity.toFixed(1).replace(/\.0$/, "")}% similar to keeper`);
      }
    }
    if (item.orientationLabel) parts.push(`${item.orientationLabel} copy`);
  } else if (item.kind === "sets" && item.match_label) {
    parts.push(item.match_label);
  }
  return parts.join(" · ");
}

function extensionFormat(path) {
  const ext = (path || "").split(".").pop();
  return ext && ext !== path ? ext.toUpperCase() : "";
}

function formatDetailSummary(detail, item) {
  if (!detail) return null;
  if (detail.audio !== undefined || detail.video_codec !== undefined) {
    const parts = [];
    if (detail.video_codec) parts.push(String(detail.video_codec).toUpperCase());
    if (Number(detail.fps) > 0) parts.push(`${Number(Number(detail.fps).toFixed(2))} fps`);
    if (Number(detail.bit_rate) > 0) {
      parts.push(detail.bit_rate >= 1_000_000
        ? `${(detail.bit_rate / 1_000_000).toFixed(1).replace(/\.0$/, "")} Mbps`
        : `${Math.round(detail.bit_rate / 1000)} kbps`);
    }
    if (detail.hdr) parts.push("HDR");
    const audio = Array.isArray(detail.audio) && detail.audio.length
      ? detail.audio.map((a) => `${a.codec_name || "audio"} ${a.channels || "?"}ch`).join("/")
      : "no audio";
    parts.push(audio);
    return parts.join(" · ");
  }
  return detail.format || extensionFormat(item?.path) || null;
}

function createDiffChip(label, valueText, pillText = "", pillWinner = false) {
  const chip = document.createElement("span");
  chip.className = "lb-diff-chip";
  chip.style.display = "inline-flex";
  chip.style.alignItems = "center";
  chip.style.gap = "5px";
  chip.style.padding = "2px 8px";
  chip.style.borderRadius = "999px";
  chip.style.border = "1px solid #333333";
  chip.style.background = "rgba(255, 255, 255, 0.04)";
  chip.style.fontSize = "11px";
  chip.style.color = "#D6D6D2";

  const text = document.createElement("span");
  text.textContent = label ? `${label}: ${valueText}` : valueText;
  chip.append(text);

  if (pillText) {
    const pill = document.createElement("span");
    pill.className = pillWinner ? "lb-diff-pill winner" : "lb-diff-pill";
    pill.textContent = pillText;
    pill.style.padding = "1px 6px";
    pill.style.borderRadius = "999px";
    pill.style.fontSize = "10px";
    pill.style.fontWeight = "600";
    if (pillWinner) {
      pill.style.color = "#3ECF8E";
      pill.style.background = "rgba(62, 207, 142, 0.16)";
      pill.style.border = "1px solid rgba(62, 207, 142, 0.45)";
    } else {
      pill.style.color = "#A0A0A0";
      pill.style.background = "rgba(255, 255, 255, 0.06)";
    }
    chip.append(pill);
  }
  return chip;
}

async function fetchMediaDetailsSafe(member) {
  if (!member?.path) return null;
  const cacheKey = `${member.path}:${member.media_version || ""}`;
  if (mediaDetailsCache.has(cacheKey)) return mediaDetailsCache.get(cacheKey);
  try {
    const detail = await api(`/api/media-details?path=${encodeURIComponent(member.path)}`);
    mediaDetailsCache.set(cacheKey, detail);
    return detail;
  } catch {
    return null;
  }
}

function renderDiffStripContent(strip, cand, ref, candDetail, refDetail) {
  const chips = [];
  const cw = Number(cand.width) || 0;
  const ch = Number(cand.height) || 0;
  const rw = Number(ref.width) || 0;
  const rh = Number(ref.height) || 0;
  if (cw > 0 && ch > 0 && rw > 0 && rh > 0) {
    const cpx = cw * ch;
    const rpx = rw * rh;
    let pill = "";
    let win = false;
    if (cpx !== rpx) {
      const diffMp = Math.abs(cpx - rpx) / 1_000_000;
      const mpNote = diffMp >= 0.1 ? `+${diffMp.toFixed(1)} MP` : "+MP";
      pill = cpx > rpx ? `Cand ${mpNote}` : `Ref ${mpNote}`;
      win = true;
    }
    chips.push(createDiffChip("Dims", `${cw}×${ch} vs ${rw}×${rh}`, pill, win));
  }

  if (Number.isFinite(Number(cand.size)) && Number.isFinite(Number(ref.size))) {
    const cs = Number(cand.size);
    const rs = Number(ref.size);
    const pill = cs === rs ? "Same size" : cs < rs ? "Cand smaller" : "Ref smaller";
    chips.push(createDiffChip("Size", `${formatBytes(cs)} vs ${formatBytes(rs)}`, pill, false));
  }

  const candFmt = (candDetail?.format || extensionFormat(cand.path) || "").toUpperCase();
  const refFmt = (refDetail?.format || extensionFormat(ref.path) || "").toUpperCase();
  if (candFmt || refFmt) {
    const candLossless = LOSSLESS_FORMATS.has(candFmt);
    const refLossless = LOSSLESS_FORMATS.has(refFmt);
    let pill = "";
    let win = false;
    if (candLossless !== refLossless) {
      pill = candLossless ? "Cand Lossless" : "Ref Lossless";
      win = true;
    }
    chips.push(createDiffChip("Format", `${candFmt || "?"} vs ${refFmt || "?"}`, pill, win));
  }

  const cm = Number(cand.mtime) || 0;
  const rm = Number(ref.mtime) || 0;
  if (cm > 0 && rm > 0 && Math.abs(cm - rm) >= 1) {
    const olderSide = cm < rm ? "Cand Original" : "Ref Original";
    chips.push(createDiffChip("Date", `${formatMtime(cm)} vs ${formatMtime(rm)}`, olderSide, true));
  }

  if (cand.duration != null || ref.duration != null) {
    const cd = Number(cand.duration) || 0;
    const rd = Number(ref.duration) || 0;
    const pill = Math.abs(cd - rd) > 0.5 ? (cd > rd ? "Cand longer" : "Ref longer") : "";
    chips.push(createDiffChip("Duration", `${cd.toFixed(1)}s vs ${rd.toFixed(1)}s`, pill, Boolean(pill)));
  }

  if (cand.mediaType === "video" || ref.media_type === "video") {
    const candTrack = formatDetailSummary(candDetail, cand);
    const refTrack = formatDetailSummary(refDetail, ref);
    if (candTrack || refTrack) {
      const candAudio = Array.isArray(candDetail?.audio) ? candDetail.audio.length : null;
      const refAudio = Array.isArray(refDetail?.audio) ? refDetail.audio.length : null;
      let pill = "";
      let win = false;
      if (candDetail?.hdr !== undefined && refDetail?.hdr !== undefined && candDetail.hdr !== refDetail.hdr) {
        pill = candDetail.hdr ? "Cand HDR" : "Ref HDR";
        win = true;
      } else if (candAudio !== null && refAudio !== null && candAudio !== refAudio) {
        pill = refAudio > candAudio ? "Ref has audio" : "Cand has audio";
        win = true;
      }
      chips.push(createDiffChip("Tracks", `${candTrack || "?"} vs ${refTrack || "?"}`, pill, win));
    }
  }

  strip.replaceChildren(...chips);
  strip.hidden = chips.length === 0;
}

function updateDiffStrip(item, isVideo) {
  const strip = $("lbDiffStrip");
  if (!strip) return;
  const token = ++diffToken;
  strip.style.display = "flex";
  strip.style.flexWrap = "wrap";
  strip.style.justifyContent = "center";
  strip.style.gap = "6px";

  const member = findGroupMember(item?.path);
  const keeperMember = item?.keeper && item.keeper !== item.path ? findGroupMember(item.keeper) : null;

  if (keeperMember && member) {
    renderDiffStripContent(strip, item, keeperMember, null, null);
    Promise.all([fetchMediaDetailsSafe(member), fetchMediaDetailsSafe(keeperMember)])
      .then(([candDetail, refDetail]) => {
        if (token !== diffToken || !lightboxOpen()) return;
        renderDiffStripContent(strip, item, keeperMember, candDetail, refDetail);
        layoutImagePlane();
      });
    return;
  }

  if (isVideo && member) {
    strip.hidden = true;
    fetchMediaDetailsSafe(member).then((detail) => {
      if (token !== diffToken || !lightboxOpen() || !detail) return;
      const summary = formatDetailSummary(detail, item);
      if (!summary) return;
      strip.replaceChildren(createDiffChip("Track", summary));
      strip.hidden = false;
    });
    return;
  }

  strip.replaceChildren();
  strip.hidden = true;
}

function setSplitPct(nextPct) {
  splitPct = Math.max(0, Math.min(100, Number(nextPct) || 50));
  const plane = $("lbImageStack")?.querySelector(".lb-image-plane");
  const handle = $("lbSplitHandle");
  const keeperImage = $("lbKeeperImage");
  if (plane) plane.style.setProperty("--split-pct", `${splitPct}%`);
  if (keeperImage && $("lbCompareMode")?.value === "split" && !flickerActive) {
    keeperImage.style.clipPath = `inset(0 calc(100% - var(--split-pct, ${splitPct}%)) 0 0)`;
  }
  if (handle) {
    handle.style.left = `var(--split-pct, ${splitPct}%)`;
    handle.setAttribute("aria-valuenow", String(Math.round(splitPct)));
  }
}

function applyCompareMode() {
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  const isVideo = item?.mediaType === "video";
  const canCompare = canComparePair(item, isVideo);
  const keeperImage = $("lbKeeperImage");
  const plane = $("lbImageStack")?.querySelector(".lb-image-plane");
  const splitHandle = $("lbSplitHandle");
  const diffGainWrap = $("lbDiffGainWrap");
  const stageBadge = $("lbStageBadge");

  if (!canCompare || !keeperImage || !plane) {
    if (plane) {
      plane.classList.remove("side-by-side");
      plane.style.filter = "";
    }
    if (splitHandle) {
      splitHandle.hidden = true;
      splitHandle.style.display = "";
      splitHandle.setAttribute("aria-hidden", "true");
      splitHandle.removeAttribute("tabindex");
    }
    if (diffGainWrap) diffGainWrap.hidden = true;
    if (stageBadge) stageBadge.hidden = true;
    layoutImagePlane();
    return;
  }

  const mode = $("lbCompareMode").value || "overlay";
  const opacitySlider = $("lbOpacity");
  const difference = mode === "difference";
  const split = mode === "split";
  const side = mode === "side";

  keeperImage.hidden = false;
  opacitySlider.disabled = difference || split || side;
  if (diffGainWrap) diffGainWrap.hidden = !difference;

  if (difference) {
    const gain = Number($("lbDiffGain")?.value || 1);
    plane.style.filter = gain > 1
      ? `contrast(${gain}) brightness(${gain === 4 ? 1.4 : 1.8})`
      : "";
    keeperImage.style.mixBlendMode = "difference";
    keeperImage.style.opacity = "1";
    keeperImage.style.clipPath = "";
  } else if (split) {
    plane.style.filter = "";
    keeperImage.style.mixBlendMode = "normal";
    keeperImage.style.opacity = "1";
    plane.style.setProperty("--split-pct", `${splitPct}%`);
    keeperImage.style.clipPath = `inset(0 calc(100% - var(--split-pct, ${splitPct}%)) 0 0)`;
  } else if (side) {
    plane.style.filter = "";
    keeperImage.style.mixBlendMode = "normal";
    keeperImage.style.opacity = "1";
    keeperImage.style.clipPath = "";
  } else {
    plane.style.filter = "";
    keeperImage.style.mixBlendMode = "normal";
    keeperImage.style.opacity = String(Number(opacitySlider.value) / 100);
    keeperImage.style.clipPath = "";
  }

  if (splitHandle) {
    splitHandle.hidden = !split;
    splitHandle.style.display = split ? "flex" : "";
    if (split) {
      splitHandle.style.position = "absolute";
      splitHandle.style.top = "0";
      splitHandle.style.bottom = "0";
      splitHandle.style.left = `var(--split-pct, ${splitPct}%)`;
      splitHandle.style.transform = "translateX(-50%)";
      splitHandle.style.width = "24px";
      splitHandle.style.zIndex = "6";
      splitHandle.style.cursor = "ew-resize";
      splitHandle.style.alignItems = "center";
      splitHandle.style.justifyContent = "center";
      splitHandle.style.touchAction = "none";
      splitHandle.style.background = "linear-gradient(to right, transparent calc(50% - 1px), rgba(255, 255, 255, 0.82) calc(50% - 1px), rgba(255, 255, 255, 0.82) calc(50% + 1px), transparent calc(50% + 1px))";
      const knob = splitHandle.querySelector(".lb-split-knob");
      if (knob) {
        knob.style.padding = "4px 6px";
        knob.style.borderRadius = "999px";
        knob.style.background = "rgba(14, 14, 14, 0.88)";
        knob.style.color = "#F2F2F0";
        knob.style.border = "1px solid rgba(255, 255, 255, 0.45)";
        knob.style.fontSize = "12px";
        knob.style.lineHeight = "1";
        knob.style.userSelect = "none";
      }
      splitHandle.setAttribute("aria-hidden", "false");
      splitHandle.setAttribute("role", "slider");
      splitHandle.setAttribute("aria-label", "Split comparison position");
      splitHandle.setAttribute("aria-valuemin", "0");
      splitHandle.setAttribute("aria-valuemax", "100");
      splitHandle.setAttribute("aria-valuenow", String(Math.round(splitPct)));
      splitHandle.tabIndex = 0;
    } else {
      splitHandle.setAttribute("aria-hidden", "true");
      splitHandle.removeAttribute("tabindex");
    }
  }

  if (stageBadge) {
    stageBadge.hidden = false;
    if (difference) stageBadge.textContent = "Difference";
    else if (split) stageBadge.textContent = "Reference | Candidate";
    else if (side) stageBadge.textContent = "Candidate | Reference";
    else stageBadge.textContent = Number(opacitySlider.value) >= 50 ? "Reference" : "Candidate";
  }

  layoutImagePlane();
}

// Both images share the comparison surface at preview or full fidelity.
function syncCompareTools(item, isVideo) {
  const canCompare = canComparePair(item, isVideo);
  const keeperImage = $("lbKeeperImage");
  $("lbCompareTools").hidden = !canCompare;
  keeperImage.hidden = !canCompare;
  if (canCompare) {
    const keeperMember = findGroupMember(item.keeper);
    const keeperVersion = keeperMember?.media_version ?? null;
    if (isZoomed()) {
      const fullKeeper = fullUrl(item.keeper, keeperVersion);
      if (!keeperImage.getAttribute("src")) {
        keeperImage.src = previewUrl(item.keeper, keeperVersion);
      }
      if (keeperImage.getAttribute("src") !== fullKeeper) {
        loadFullImageVariant(keeperImage, item.keeper, keeperVersion, item.path, item.keeper);
      }
    } else {
      const targetPreview = previewUrl(item.keeper, keeperVersion);
      if (keeperImage.getAttribute("src") !== targetPreview) {
        keeperImage.src = targetPreview;
        keeperImage.dataset.loadedPath = item.keeper;
        keeperImage.dataset.loadedMode = "preview";
      }
    }
    if (flickerActive) {
      keeperImage.style.mixBlendMode = "normal";
      keeperImage.style.opacity = "1";
      keeperImage.style.clipPath = "";
      $("lbStageBadge").hidden = false;
      $("lbStageBadge").textContent = "Reference";
    } else {
      applyCompareMode();
    }
  } else {
    keeperImage.removeAttribute("src");
    delete keeperImage.dataset.loadedPath;
    delete keeperImage.dataset.loadedMode;
    applyCompareMode();
  }
}

function updateStatusPill(item, group, inSwipe) {
  const pill = $("lbStatusPill");
  if (!pill) return;
  let text = "";
  if (item.protected) {
    text = "Protected";
  } else if (group?.kind === "exact" || group?.kind === "similar") {
    if (inSwipe) {
      if (item.path === item.keeper) text = "Reference";
    } else if ((group.selected_for_removal || []).includes(item.path)) {
      text = "Marked for removal";
    } else if (item.path === group.suggested_keep) {
      text = "Suggested keeper";
    } else {
      text = "Keeping";
    }
  } else if (isDecisionReview(group)) {
    if ((group.selected_for_removal || []).includes(item.path)) text = "Staged for removal";
    else if ((group.reviewed_paths || []).includes(item.path)) text = "Kept";
  } else if (group?.kind === "crop") {
    if (item.crop_state === "cropped") text = "Cropped";
    else if (item.crop_state === "skipped") text = "Skipped";
  }
  pill.hidden = !text;
  pill.textContent = text;
}

function syncLightboxActions(item, group) {
  const inSwipe = swipeActive();
  const isDecision = isDecisionReview(group);
  const isCrop = group?.kind === "crop";
  const isTriage = isPagedIndependentReview(item) || isPagedIndependentReview(group);
  const isPairSelect = Boolean(
    group && !inSwipe && !isIndependentReview(group)
    && (item.kind === "exact" || item.kind === "similar"),
  );

  updateStatusPill(item, group, inSwipe);

  // 1. Exact/Similar Grid or Board selection + pair actions
  $("lbSelectWrap").hidden = !isPairSelect;
  if (isPairSelect) {
    const isSelected = (group.selected_for_removal || []).includes(item.path);
    const button = $("lbSelect");
    button.setAttribute("aria-pressed", isSelected && !item.protected ? "true" : "false");
    button.textContent = item.protected
      ? "Protected library"
      : isSelected
        ? "Marked for removal"
        : "Mark for removal";
    button.classList.toggle("danger", isSelected && !item.protected);
    button.disabled = item.protected || state.deleteBusy.has(item.path);
  }

  // 2. Independent paged triage Trash
  $("lbActions").hidden = !isTriage;
  $("lbDelete").disabled = item.protected || state.deleteBusy.has(item.path);

  // 3. Decision review OR Swipe decisions
  const showDecisions = isDecision || inSwipe;
  $("lbDecisions").hidden = !showDecisions;
  if (showDecisions) {
    const stageDeleteBtn = $("lbStageDelete");
    const keepBtn = $("lbKeep");
    const hint = $("lbDecisions").querySelector(".lb-action-hint");
    if (inSwipe) {
      const isRef = item.path === item.keeper;
      stageDeleteBtn.textContent = "← Same (Trash)";
      keepBtn.textContent = "Different →";
      stageDeleteBtn.disabled = isRef || item.protected || state.swipeBusy;
      keepBtn.disabled = isRef || state.swipeBusy;
      if (hint) {
        hint.textContent = isRef
          ? "Reference copy stays on disk · use [ / ] to inspect candidates."
          : "← Same moves this copy to Trash (undoable) · → Different keeps both.";
      }
    } else {
      stageDeleteBtn.textContent = "← Stage for removal";
      keepBtn.textContent = "Keep →";
      stageDeleteBtn.disabled = item.protected || state.reviewingCandidate;
      keepBtn.disabled = state.reviewingCandidate;
      if (hint) hint.textContent = "Nothing moves until you confirm staged removals.";
    }
  }

  // 4. Pair actions: Keep only this / Not duplicates / Swipe Undo
  const pairActions = $("lbPairActions");
  const keepOnlyBtn = $("lbKeepOnly");
  const notDupBtn = $("lbNotDup");
  const undoBtn = $("lbUndo");
  if (pairActions) {
    if (inSwipe) {
      const undoStack = group ? (state.swipeUndo.get(group.id) || []) : [];
      pairActions.hidden = undoStack.length === 0;
      keepOnlyBtn.hidden = true;
      notDupBtn.hidden = true;
      undoBtn.hidden = undoStack.length === 0;
      undoBtn.disabled = undoStack.length === 0;
    } else if (isPairSelect) {
      pairActions.hidden = false;
      keepOnlyBtn.hidden = false;
      notDupBtn.hidden = group.kind !== "similar";
      undoBtn.hidden = true;
      const selectedSet = new Set(group.selected_for_removal || []);
      const allOthersSelected = (group.members || []).every(
        (m) => m.path === item.path || m.protected || selectedSet.has(m.path),
      );
      keepOnlyBtn.disabled = (!selectedSet.has(item.path) && allOthersSelected) || state.actionBusy;
      notDupBtn.disabled = state.actionBusy;
    } else {
      pairActions.hidden = true;
    }
  }

  // 5. Crop actions
  const cropActions = $("lbCropActions");
  if (cropActions) {
    cropActions.hidden = !isCrop;
    if (isCrop) {
      const pending = (item.crop_state || "pending") === "pending" && !item.protected;
      $("lbCropAdjust").disabled = !pending;
      $("lbCropApply").disabled = !pending || !item.crop;
      $("lbCropSkip").disabled = !pending;
    }
  }
}

function updateLightbox() {
  if (!state.lightboxItems.length) {
    if (lightboxOpen()) closeLightbox();
    return;
  }
  state.lightboxIndex = Math.max(0, Math.min(state.lightboxIndex, state.lightboxItems.length - 1));
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  if (!item) return;

  styleOverlayChrome();

  const image = $("lbImage");
  const video = $("lbVideo");
  const isVideo = item.mediaType === "video";
  const isGif = item.mediaType === "gif";
  const canZoom = !isVideo && !isGif;

  video.pause();
  video.hidden = !isVideo;
  $("lbVideoTools").hidden = !isVideo;
  $("lbStageTools").hidden = !canZoom;
  $("lbImageStack").hidden = isVideo;
  image.hidden = isVideo;

  if (isVideo) {
    image.removeAttribute("src");
    delete image.dataset.loadedPath;
    delete image.dataset.loadedMode;
    const vSrc = mediaUrl(item.path, item.media_version);
    if (video.getAttribute("src") !== vSrc) video.src = vSrc;
    video.playbackRate = Number($("lbSpeed").value);
    syncCompareTools(item, true);
  } else {
    video.removeAttribute("src");
    video.load();
    const targetMode = canZoom && isZoomed() ? "full" : "preview";
    if (isGif) {
      const gSrc = mediaUrl(item.path, item.media_version);
      if (image.getAttribute("src") !== gSrc) {
        image.src = gSrc;
        image.dataset.loadedPath = item.path;
        image.dataset.loadedMode = "gif";
      }
    } else if (targetMode === "full") {
      const fSrc = fullUrl(item.path, item.media_version);
      if (image.dataset.loadedPath !== item.path) {
        image.src = previewUrl(item.path, item.media_version);
        image.dataset.loadedPath = item.path;
        image.dataset.loadedMode = "preview";
      }
      if (image.getAttribute("src") !== fSrc) {
        loadFullImageVariant(image, item.path, item.media_version, item.path, null);
      }
    } else {
      const pSrc = previewUrl(item.path, item.media_version);
      if (image.getAttribute("src") !== pSrc) {
        image.src = pSrc;
        image.dataset.loadedPath = item.path;
        image.dataset.loadedMode = "preview";
      }
    }
    syncCompareTools(item, false);
  }

  const group = currentGroup();
  const count = state.lightboxItems.length;
  $("lbCounter").textContent = count > 1 ? `${state.lightboxIndex + 1} / ${count}` : "";
  $("lbMeta").textContent = item.path;
  $("lbDetails").textContent = lightboxDetails(item);
  updateDiffStrip(item, isVideo);
  if (canComparePair(item, isVideo)) noteReviewPair(item.keeper, item.path);

  const single = count <= 1;
  $("lbPrev").disabled = single;
  $("lbNext").disabled = single;

  syncLightboxActions(item, group);
  layoutImagePlane();
  if (canZoom && isZoomed()) {
    requestAnimationFrame(() => applyNormalizedPan(savedPan));
  }
  prefetchLightboxNeighbors();
}

function setFlicker(show) {
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  const canCompare = canComparePair(item, item?.mediaType === "video");
  const next = Boolean(show) && canCompare && lightboxOpen();
  if (flickerActive === next && !next) {
    $("lbFlicker")?.setAttribute("aria-pressed", "false");
    return;
  }
  flickerActive = next;
  $("lbFlicker")?.setAttribute("aria-pressed", next ? "true" : "false");
  if (next) {
    const plane = $("lbImageStack")?.querySelector(".lb-image-plane");
    const keeper = $("lbKeeperImage");
    if (plane?.classList.contains("side-by-side")) {
      plane.classList.remove("side-by-side");
      layoutImagePlane();
    }
    if (plane) plane.style.filter = "";
    if (keeper) {
      keeper.hidden = false;
      keeper.style.mixBlendMode = "normal";
      keeper.style.opacity = "1";
      keeper.style.clipPath = "none";
    }
    const badge = $("lbStageBadge");
    if (badge) {
      badge.hidden = false;
      badge.textContent = "Reference";
    }
  } else {
    applyCompareMode();
  }
}

try {
  const savedRate = Number(localStorage.getItem(PLAYBACK_RATE_KEY));
  if ([0.5, 1, 1.5, 2, 3, 4].includes(savedRate)) $("lbSpeed").value = String(savedRate);
} catch {
  /* ignore */
}
$("lbSpeed").addEventListener("change", () => {
  const rate = Number($("lbSpeed").value);
  $("lbVideo").playbackRate = rate;
  try {
    localStorage.setItem(PLAYBACK_RATE_KEY, String(rate));
  } catch {
    /* ignore */
  }
});
$("lbVideo").addEventListener("loadedmetadata", () => {
  $("lbVideo").playbackRate = Number($("lbSpeed").value);
});

$("lbImage")?.addEventListener("load", () => {
  if (lightboxOpen()) {
    layoutImagePlane();
    if (isZoomed()) applyNormalizedPan(savedPan);
  }
});
$("lbKeeperImage")?.addEventListener("load", () => {
  if (lightboxOpen()) layoutImagePlane();
});

$("lbCompareMode").addEventListener("change", () => {
  flickerActive = false;
  applyCompareMode();
});
$("lbOpacity").addEventListener("input", () => {
  flickerActive = false;
  applyCompareMode();
});
$("lbDiffGain")?.addEventListener("change", () => {
  flickerActive = false;
  applyCompareMode();
});

["pointerdown", "keydown"].forEach((eventName) => $("lbFlicker").addEventListener(eventName, (event) => {
  if (eventName === "pointerdown" && event.pointerType === "mouse" && event.button !== 0) return;
  if (eventName === "keydown" && (![" ", "Enter"].includes(event.key) || event.repeat)) return;
  setFlicker(true);
}));
["pointerup", "pointercancel", "pointerleave", "keyup", "blur"].forEach((eventName) =>
  $("lbFlicker").addEventListener(eventName, () => setFlicker(false)),
);

// Split-curtain drag handle & keyboard accessibility
let splitDragging = false;
$("lbSplitHandle")?.addEventListener("pointerdown", (event) => {
  if (event.pointerType === "mouse" && event.button !== 0) return;
  event.preventDefault();
  event.stopPropagation();
  splitDragging = true;
  $("lbSplitHandle").setPointerCapture?.(event.pointerId);
});
$("lbSplitHandle")?.addEventListener("pointermove", (event) => {
  if (!splitDragging) return;
  const plane = $("lbImageStack")?.querySelector(".lb-image-plane");
  const rect = plane?.getBoundingClientRect();
  if (!rect || rect.width <= 0) return;
  setSplitPct(((event.clientX - rect.left) / rect.width) * 100);
});
["pointerup", "pointercancel"].forEach((eventName) =>
  $("lbSplitHandle")?.addEventListener(eventName, () => {
    splitDragging = false;
  }),
);
$("lbSplitHandle")?.addEventListener("keydown", (event) => {
  const step = event.shiftKey ? 10 : 2;
  if (event.key === "ArrowLeft") setSplitPct(splitPct - step);
  else if (event.key === "ArrowRight") setSplitPct(splitPct + step);
  else if (event.key === "Home") setSplitPct(0);
  else if (event.key === "End") setSplitPct(100);
  else return;
  event.preventDefault();
  event.stopPropagation();
});

$("lbClose").addEventListener("click", closeLightbox);
$("lbPrev").addEventListener("click", () => stepLightbox(-1));
$("lbNext").addEventListener("click", () => stepLightbox(1));
$("lightbox").addEventListener("click", (e) => {
  if (e.target === $("lightbox")) closeLightbox();
});
$("lbDelete").addEventListener("click", () => {
  const item = state.lightboxItems[state.lightboxIndex];
  const group = currentGroup();
  if (!item || !isPagedIndependentReview(group)) return;
  trashReviewCandidate(group, item.path, { fromLightbox: true });
});

for (const [id, remove] of [["lbStageDelete", true], ["lbKeep", false]]) {
  $(id).addEventListener("click", async () => {
    const item = state.lightboxItems[state.lightboxIndex];
    if (!item) return;
    if (swipeActive()) {
      await decideSwipe(remove ? "same" : "distinct", item.path);
      if (lightboxOpen() && (!currentGroup() || !state.lightboxItems.length)) {
        closeLightbox();
      }
      return;
    }
    reviewCandidate(currentGroup(), item.path, remove);
  });
}

// Reveal works for every kind — it answers "what is this file?" mid-sift.
$("lbReveal").addEventListener("click", () => {
  const item = state.lightboxItems[state.lightboxIndex];
  if (!item) return;
  api(`/api/reveal?path=${encodeURIComponent(item.path)}&open=1`)
    .catch((error) => toast(error.message || String(error), "error"));
});

// —— Select-for-removal toggle (exact/similar groups) ——
$("lbSelect").addEventListener("click", async () => {
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  const group = currentGroup();
  if (!item || !group || isIndependentReview(group) || swipeActive()) return;
  if (item.protected) return toast("This file is in a protected library");
  const wantSelected = !(group.selected_for_removal || []).includes(item.path);
  const button = $("lbSelect");
  button.disabled = true;
  try {
    const updated = await setMemberSelected(group, item.path, wantSelected);
    if (updated) updateLightbox();
  } catch (error) {
    toast(error.message || String(error), "error");
  } finally {
    button.disabled = false;
  }
});

// —— Pair actions (Keep only this / Not duplicates / Swipe Undo) ——
$("lbKeepOnly")?.addEventListener("click", async () => {
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  const group = currentGroup();
  if (!item || !group || isIndependentReview(group) || swipeActive()) return;
  const wanted = (group.members || [])
    .filter((m) => !m.protected && m.path !== item.path)
    .map((m) => m.path);
  const btn = $("lbKeepOnly");
  btn.disabled = true;
  try {
    const updated = await api("/api/selection", {
      method: "POST",
      body: JSON.stringify({ group_id: group.id, selected: wanted, scan_id: state.scanId }),
    });
    markGroupTouched(group.id);
    patchGroup(updated);
    if (boardActive()) refreshBoardRow(updated);
    else renderMembers(updated);
    if (selectionFiltersActive() || !updateGroupListItem(updated)) {
      scheduleRender({ groupList: true });
    } else {
      applyResultControls();
    }
    scheduleRender({ selection: true });
    updateLightbox();
  } catch (error) {
    toast(error.message || String(error), "error");
  } finally {
    btn.disabled = false;
  }
});

$("lbNotDup")?.addEventListener("click", async () => {
  const group = currentGroup();
  if (!group || group.kind !== "similar" || swipeActive()) return;
  const inBoard = boardActive();
  const groupId = group.id;
  const btn = $("lbNotDup");
  btn.disabled = true;
  try {
    await api("/api/similar/mark-distinct", {
      method: "POST",
      body: JSON.stringify({
        group_id: groupId,
        scan_id: state.scanId,
        source: inBoard ? "board" : "list",
      }),
    });
    closeLightbox();
    if (inBoard) {
      const rows = [...document.querySelectorAll("#boardRows .board-row")];
      const idx = rows.findIndex((r) => r.dataset.id === groupId);
      state.boardDismissed.set(groupId, { group, index: Math.max(0, idx) });
      toast("Not duplicates — every file stays; undo from the row", "ok");
      await loadGroups();
    } else {
      await loadGroups();
      toast("Marked as distinct — this group won't come back", "ok", {
        actionLabel: "Undo",
        onAction: async () => {
          try {
            await api("/api/similar/restore-group", {
              method: "POST",
              body: JSON.stringify({ group_id: groupId, scan_id: state.scanId }),
            });
            await loadGroups();
            await selectGroup(groupId);
            toast("Back in review — these files can match again", "ok");
          } catch (error) {
            toast(error.message || "Could not undo", "error");
          }
        },
      });
    }
  } catch (error) {
    toast(error.message || String(error), "error");
  } finally {
    btn.disabled = false;
  }
});

$("lbUndo")?.addEventListener("click", () => {
  if (swipeActive()) undoSwipe();
});

// —— Crop actions inside Lightbox ——
$("lbCropAdjust")?.addEventListener("click", () => {
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  const group = currentGroup();
  if (!item || group?.kind !== "crop") return;
  closeLightbox();
  openCropEditor(item.path);
});

$("lbCropApply")?.addEventListener("click", async () => {
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  const group = currentGroup();
  if (!item || group?.kind !== "crop") return;
  const btn = $("lbCropApply");
  btn.disabled = true;
  try {
    await applyCrop(group, item.path);
    if (lightboxOpen()) updateLightbox();
  } finally {
    btn.disabled = false;
  }
});

$("lbCropSkip")?.addEventListener("click", async () => {
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  const group = currentGroup();
  if (!item || group?.kind !== "crop") return;
  const btn = $("lbCropSkip");
  btn.disabled = true;
  try {
    await skipCrop(group, item.path, true);
    if (lightboxOpen()) updateLightbox();
  } finally {
    btn.disabled = false;
  }
});

// —— Continuous zoom & pan ——
$("lbZoom").addEventListener("click", () => setZoom(!isZoomed()));
$("lbZoomLevel")?.addEventListener("change", (event) => {
  const val = event.target.value;
  setZoomLevel(val === "fit" ? 0 : Number(val));
});
$("lbImageStack").addEventListener("dblclick", (event) => {
  if (event.target.closest("#lbSplitHandle, #lbStageBadge, #lbStatusPill")) return;
  setZoomLevel(isZoomed() ? 0 : 1, { anchorClientX: event.clientX, anchorClientY: event.clientY });
});
$("lbImageStack").addEventListener("wheel", (event) => {
  const item = resolveItem(state.lightboxItems[state.lightboxIndex]);
  if (!item || item.mediaType !== "image") return;
  const pinch = event.ctrlKey || event.metaKey;
  if (!pinch && (isZoomed() || event.deltaY >= 0 || Math.abs(event.deltaY) <= Math.abs(event.deltaX))) {
    return;
  }
  event.preventDefault();
  if (!isZoomed()) {
    const startFactor = pinch ? Math.max(1, Math.min(4, Math.exp(-event.deltaY * 0.01))) : 1;
    setZoomLevel(startFactor, { anchorClientX: event.clientX, anchorClientY: event.clientY });
    return;
  }
  const next = zoomFactor * Math.exp(-event.deltaY * 0.01);
  if (next < 0.9) {
    setZoomLevel(0);
  } else {
    setZoomLevel(Math.max(1, Math.min(4, next)), {
      anchorClientX: event.clientX,
      anchorClientY: event.clientY,
    });
  }
}, { passive: false });

let panStart = null;
$("lbImageStack").addEventListener("scroll", () => {
  if (!isZoomed()) return;
  const pan = captureNormalizedPan();
  if (pan) savedPan = pan;
}, { passive: true });
$("lbImageStack").addEventListener("pointerdown", (event) => {
  if (!isZoomed() || event.target.closest("#lbSplitHandle")) return;
  const stack = $("lbImageStack");
  panStart = {
    x: event.clientX,
    y: event.clientY,
    left: stack.scrollLeft,
    top: stack.scrollTop,
  };
  stack.setPointerCapture(event.pointerId);
  event.preventDefault(); // keep the drag from selecting/swiping
});
$("lbImageStack").addEventListener("pointermove", (event) => {
  if (!panStart) return;
  const stack = $("lbImageStack");
  stack.scrollLeft = panStart.left - (event.clientX - panStart.x);
  stack.scrollTop = panStart.top - (event.clientY - panStart.y);
});
["pointerup", "pointercancel"].forEach((eventName) =>
  $("lbImageStack").addEventListener(eventName, () => {
    panStart = null;
  }),
);

// Swipe left/right to navigate on touch screens.
let touchStartX = null;
$("lightbox").addEventListener("touchstart", (e) => {
  if (splitDragging) return;
  touchStartX = e.changedTouches.length === 1 ? e.changedTouches[0].clientX : null;
}, { passive: true });
$("lightbox").addEventListener("touchend", (e) => {
  if (touchStartX == null || e.changedTouches.length !== 1) return;
  const delta = e.changedTouches[0].clientX - touchStartX;
  touchStartX = null;
  // While zoomed or dragging split handle, horizontal drags pan instead of stepping.
  if (isZoomed() || splitDragging || Math.abs(delta) < 40) return;
  stepLightbox(delta < 0 ? 1 : -1);
}, { passive: true });

export { closeLightbox, lightboxOpen, openLightbox, setFlicker, stepLightbox, updateLightbox };
