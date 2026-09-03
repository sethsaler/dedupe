// Linger-to-preview: hovering a member-card image for more than a second
// shows a full-image quick-look overlay. It is purely visual — pointer-events
// pass through it and it never takes focus — so the lightbox (click or Enter)
// stays the interactive full view. Videos are excluded: their hover-to-play
// inline preview already answers "what is this?".

import { $ } from "./util.js";

// The hover must be uninterrupted for this long before the preview opens.
const LINGER_MS = 1000;

let pendingWrap = null;
let timer = null;

// Only cards whose thumb is a still image (GIFs included) can preview; video
// cards play inline on hover, and Trash placeholders have nothing to show.
function previewableImage(wrap) {
  return wrap?.querySelector?.(".thumb-image") || null;
}

function hidePreview() {
  clearTimeout(timer);
  timer = null;
  pendingWrap = null;
  const overlay = $("hoverPreview");
  if (overlay.hidden) return;
  overlay.hidden = true;
  $("hoverPreviewImage").removeAttribute("src");
}

function showPreview(wrap) {
  timer = null;
  // A re-render may have swapped the card out from under the cursor.
  if (!wrap.isConnected || wrap !== pendingWrap) return;
  const thumb = previewableImage(wrap);
  if (!thumb) return;
  // GIFs preview their animated original (the URL hover-play swaps in); still
  // images use the cached 2560px "preview" variant, same as the lightbox.
  const fullSrc = thumb.classList.contains("hover-gif")
    ? thumb.dataset.src
    : `/api/thumbnail?path=${encodeURIComponent(wrap.dataset.path)}&variant=preview`;
  const image = $("hoverPreviewImage");
  // The card's thumbnail is already in the browser cache, so it paints
  // instantly; swap in the larger variant once it finishes loading.
  image.src = thumb.currentSrc || thumb.src;
  $("hoverPreview").hidden = false;
  const loader = new Image();
  loader.decoding = "async";
  loader.onload = () => {
    if (pendingWrap === wrap && !$("hoverPreview").hidden) image.src = fullSrc;
  };
  loader.src = fullSrc;
}

const members = $("members");

members.addEventListener("pointerover", (event) => {
  const wrap = event.target.closest?.(".thumb-wrap");
  if (!wrap || wrap === pendingWrap || !previewableImage(wrap)) return;
  clearTimeout(timer);
  pendingWrap = wrap;
  timer = setTimeout(() => showPreview(wrap), LINGER_MS);
});

members.addEventListener("pointerout", (event) => {
  const wrap = event.target.closest?.(".thumb-wrap");
  // Moving between children of the same thumbnail keeps the linger going.
  if (!wrap || wrap.contains(event.relatedTarget)) return;
  if (wrap === pendingWrap) hidePreview();
});

// A click opens the lightbox; the quick-look must not linger above it.
members.addEventListener("pointerdown", hidePreview);

// Scrolling moves thumbnails under a stationary cursor; the preview would end
// up hovering over the wrong image. Only cancel when the cursor is no longer
// on the thumbnail — scrolls can also come from the browser scrolling the
// hovered card into view, which must not cancel the linger.
document.addEventListener("scroll", () => {
  if (pendingWrap && !pendingWrap.matches(":hover")) hidePreview();
}, true);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hidePreview();
});

document.addEventListener("pointermove", () => {
  // If a re-render removed the hovered card, no pointerout ever fires.
  if (pendingWrap && !pendingWrap.isConnected) hidePreview();
});
