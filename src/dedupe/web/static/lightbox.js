// The full-screen comparison overlay.

import { api } from "./api.js";
import { setMemberSelected, trashReviewCandidate } from "./members.js";
import { currentGroup, isIndependentReview, isPagedIndependentReview } from "./model.js";
import { state } from "./state.js";
import { $, formatBytes, formatMtime, toast } from "./util.js";

const PLAYBACK_RATE_KEY = "dedupe.videoPlaybackRate";

// Focus is moved into the lightbox on open and restored on close.
let previousFocus = null;

// Full-resolution zoom state (images only); panning is the zoomed stack's
// native scroll, driven by drag.
let zoomed = false;

// Lightbox images use the 2560px cached "preview" variant instead of the
// untouched original: stepping through 20 MP originals is needlessly slow.
function previewUrl(path) {
  return `/api/thumbnail?path=${encodeURIComponent(path)}&variant=preview`;
}

function fullUrl(path) {
  return `/api/thumbnail?path=${encodeURIComponent(path)}&variant=full`;
}

function openLightbox(index) {
  if (!state.lightboxItems.length) return;
  previousFocus = document.activeElement;
  state.lightboxIndex = Math.max(0, Math.min(index, state.lightboxItems.length - 1));
  updateLightbox();
  $("lightbox").hidden = false;
  $("lbClose").focus();
}

function closeLightbox() {
  if (zoomed) setZoom(false);
  $("lbVideo").pause();
  $("lbVideo").removeAttribute("src");
  $("lbVideo").load();
  $("lightbox").hidden = true;
  if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus();
  previousFocus = null;
}

function stepLightbox(delta) {
  const count = state.lightboxItems.length;
  if (count <= 1) return;
  // Wrap around: past the last item returns to the first, and back.
  state.lightboxIndex = (state.lightboxIndex + delta + count) % count;
  updateLightbox();
}

function prefetchLightboxNeighbors() {
  const count = state.lightboxItems.length;
  // Warm further ahead than behind: sifting holds →, not ←.
  for (const delta of [-1, 1, 2, 3]) {
    const neighbor = state.lightboxItems[(state.lightboxIndex + delta + count) % count];
    if (!neighbor || neighbor.mediaType === "video") continue;
    const image = new Image();
    image.decoding = "async";
    image.src = previewUrl(neighbor.path);
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
  if (item.mtime) parts.push(formatMtime(item.mtime));
  if (item.kind === "similar") {
    const similarity = item.similarityPercent == null ? null : Number(item.similarityPercent);
    parts.push(
      Number.isFinite(similarity)
        ? `${similarity.toFixed(1).replace(/\.0$/, "")}% similar to keeper`
        : "similarity score unavailable",
    );
  }
  return parts.join(" · ");
}

// The keeper overlay compares at preview fidelity; zoom is single-file full
// detail, so the compare tools hide while zoomed.
function syncCompareTools(item, isVideo) {
  const canCompare = !zoomed && !isVideo && item
    && item.kind === "similar" && item.keeper && item.keeper !== item.path;
  const keeperImage = $("lbKeeperImage");
  $("lbCompareTools").hidden = !canCompare;
  keeperImage.hidden = !canCompare;
  if (canCompare) {
    keeperImage.src = previewUrl(item.keeper);
    keeperImage.style.opacity = String(Number($("lbOpacity").value) / 100);
  } else {
    keeperImage.removeAttribute("src");
  }
}

function setZoom(on) {
  const item = state.lightboxItems[state.lightboxIndex];
  const next = Boolean(on) && item && item.mediaType !== "video";
  if (zoomed === next) return;
  zoomed = next;
  const stack = $("lbImageStack");
  const image = $("lbImage");
  stack.classList.toggle("zoomed", zoomed);
  const zoomButton = $("lbZoom");
  zoomButton.setAttribute("aria-pressed", zoomed ? "true" : "false");
  zoomButton.textContent = zoomed ? "Zoom out" : "Zoom";
  syncCompareTools(item, false);
  if (!zoomed) {
    if (item) image.src = previewUrl(item.path);
    return;
  }
  // Swap in the full-resolution variant; the preview stays until it loads.
  const path = item.path;
  const loader = new Image();
  loader.onload = () => {
    const current = state.lightboxItems[state.lightboxIndex];
    if (!zoomed || !current || current.path !== path) return;
    image.src = fullUrl(path);
    // Start centered, not at the top-left corner of a large image.
    requestAnimationFrame(() => {
      stack.scrollLeft = Math.max(0, (stack.scrollWidth - stack.clientWidth) / 2);
      stack.scrollTop = Math.max(0, (stack.scrollHeight - stack.clientHeight) / 2);
    });
  };
  loader.src = fullUrl(path);
}

function syncLightboxSelect(item) {
  const group = currentGroup();
  const keeperGroup = group && !isIndependentReview(group)
    && (item.kind === "exact" || item.kind === "similar");
  $("lbSelectWrap").hidden = !keeperGroup;
  if (!keeperGroup) return;
  const isSelected = (group.selected_for_removal || []).includes(item.path);
  const button = $("lbSelect");
  button.setAttribute("aria-pressed", isSelected ? "true" : "false");
  button.textContent = isSelected ? "Marked for removal" : "Mark for removal";
  button.classList.toggle("danger", isSelected);
  button.disabled = state.deleteBusy.has(item.path);
}

function updateLightbox() {
  const item = state.lightboxItems[state.lightboxIndex];
  if (!item) return;
  if (zoomed) setZoom(false);
  const image = $("lbImage");
  const video = $("lbVideo");
  const isVideo = item.mediaType === "video";

  video.pause();
  video.hidden = !isVideo;
  $("lbVideoTools").hidden = !isVideo;
  $("lbStageTools").hidden = isVideo;
  $("lbImageStack").hidden = isVideo;
  image.hidden = isVideo;
  if (isVideo) {
    image.removeAttribute("src");
    video.src = `/api/media?path=${encodeURIComponent(item.path)}`;
    video.playbackRate = Number($("lbSpeed").value);
    syncCompareTools(item, true);
  } else {
    video.removeAttribute("src");
    video.load();
    // While zoomed the full-resolution variant stays; navigation resets zoom.
    if (!zoomed) image.src = previewUrl(item.path);
    syncCompareTools(item, false);
  }
  const count = state.lightboxItems.length;
  $("lbCounter").textContent = count > 1 ? `${state.lightboxIndex + 1} / ${count}` : "";
  $("lbMeta").textContent = item.path;
  $("lbDetails").textContent = lightboxDetails(item);
  const single = count <= 1;
  $("lbPrev").disabled = single;
  $("lbNext").disabled = single;
  const canTrash = isPagedIndependentReview(item) || isPagedIndependentReview(currentGroup());
  $("lbActions").hidden = !canTrash;
  $("lbDelete").disabled = state.deleteBusy.has(item.path);
  syncLightboxSelect(item);
  prefetchLightboxNeighbors();
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
$("lbOpacity").addEventListener("input", () => {
  $("lbKeeperImage").style.opacity = String(Number($("lbOpacity").value) / 100);
});
const showKeeper = (show) => {
  $("lbKeeperImage").style.opacity = show ? "1" : "0";
  $("lbFlicker").setAttribute("aria-pressed", show ? "true" : "false");
};
["pointerdown", "keydown"].forEach((eventName) => $("lbFlicker").addEventListener(eventName, (event) => {
  if (eventName === "keydown" && ![" ", "Enter"].includes(event.key)) return;
  showKeeper(true);
}));
["pointerup", "pointerleave", "keyup", "blur"].forEach((eventName) => $("lbFlicker").addEventListener(eventName, () => showKeeper(false)));

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

// Reveal works for every kind — it answers "what is this file?" mid-sift.
$("lbReveal").addEventListener("click", () => {
  const item = state.lightboxItems[state.lightboxIndex];
  if (!item) return;
  api(`/api/reveal?path=${encodeURIComponent(item.path)}&open=1`)
    .catch((error) => toast(error.message || String(error), "error"));
});

// —— Select-for-removal toggle (exact/similar groups) ——
$("lbSelect").addEventListener("click", async () => {
  const item = state.lightboxItems[state.lightboxIndex];
  const group = currentGroup();
  if (!item || !group || isIndependentReview(group)) return;
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

// —— Full-resolution zoom (images): drag pans the zoomed view ——
$("lbZoom").addEventListener("click", () => setZoom(!zoomed));
$("lbImage").addEventListener("dblclick", () => setZoom(!zoomed));

let panStart = null;
$("lbImageStack").addEventListener("pointerdown", (event) => {
  if (!zoomed) return;
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
  touchStartX = e.changedTouches.length === 1 ? e.changedTouches[0].clientX : null;
}, { passive: true });
$("lightbox").addEventListener("touchend", (e) => {
  if (touchStartX == null || e.changedTouches.length !== 1) return;
  const delta = e.changedTouches[0].clientX - touchStartX;
  touchStartX = null;
  // While zoomed, horizontal drags pan the image instead of stepping.
  if (zoomed || Math.abs(delta) < 40) return;
  stepLightbox(delta < 0 ? 1 : -1);
}, { passive: true });

export { openLightbox, closeLightbox, updateLightbox };
