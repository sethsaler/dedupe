// The full-screen comparison overlay.

import { trashReviewCandidate } from "./members.js";
import { currentGroup, isPagedIndependentReview } from "./model.js";
import { state } from "./state.js";
import { $ } from "./util.js";

const PLAYBACK_RATE_KEY = "dedupe.videoPlaybackRate";

// Focus is moved into the lightbox on open and restored on close.
let previousFocus = null;

// Lightbox images use the 2560px cached "preview" variant instead of the
// untouched original: stepping through 20 MP originals is needlessly slow.
function previewUrl(path) {
  return `/api/thumbnail?path=${encodeURIComponent(path)}&variant=preview`;
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
  for (const delta of [-1, 1]) {
    const neighbor = state.lightboxItems[(state.lightboxIndex + delta + count) % count];
    if (!neighbor || neighbor.mediaType === "video") continue;
    const image = new Image();
    image.decoding = "async";
    image.src = previewUrl(neighbor.path);
  }
}

function updateLightbox() {
  const item = state.lightboxItems[state.lightboxIndex];
  if (!item) return;
  const image = $("lbImage");
  const video = $("lbVideo");
  const isVideo = item.mediaType === "video";
  const canCompare = !isVideo && item.kind === "similar" && item.keeper && item.keeper !== item.path;

  video.pause();
  video.hidden = !isVideo;
  $("lbVideoTools").hidden = !isVideo;
  $("lbCompareTools").hidden = !canCompare;
  $("lbImageStack").hidden = isVideo;
  image.hidden = isVideo;
  if (isVideo) {
    image.removeAttribute("src");
    video.src = `/api/media?path=${encodeURIComponent(item.path)}`;
    video.playbackRate = Number($("lbSpeed").value);
  } else {
    video.removeAttribute("src");
    video.load();
    image.src = previewUrl(item.path);
    const keeperImage = $("lbKeeperImage");
    keeperImage.hidden = !canCompare;
    if (canCompare) {
      keeperImage.src = previewUrl(item.keeper);
      keeperImage.style.opacity = String(Number($("lbOpacity").value) / 100);
    } else keeperImage.removeAttribute("src");
  }
  const count = state.lightboxItems.length;
  $("lbCounter").textContent = count > 1 ? `${state.lightboxIndex + 1} / ${count}` : "";
  $("lbMeta").textContent = item.path;
  const single = count <= 1;
  $("lbPrev").disabled = single;
  $("lbNext").disabled = single;
  const canTrash = isPagedIndependentReview(item) || isPagedIndependentReview(currentGroup());
  $("lbActions").hidden = !canTrash;
  $("lbDelete").disabled = state.deleteBusy.has(item.path);
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

// Swipe left/right to navigate on touch screens.
let touchStartX = null;
$("lightbox").addEventListener("touchstart", (e) => {
  touchStartX = e.changedTouches.length === 1 ? e.changedTouches[0].clientX : null;
}, { passive: true });
$("lightbox").addEventListener("touchend", (e) => {
  if (touchStartX == null || e.changedTouches.length !== 1) return;
  const delta = e.changedTouches[0].clientX - touchStartX;
  touchStartX = null;
  if (Math.abs(delta) < 40) return;
  stepLightbox(delta < 0 ? 1 : -1);
}, { passive: true });

export { openLightbox, closeLightbox, updateLightbox };
