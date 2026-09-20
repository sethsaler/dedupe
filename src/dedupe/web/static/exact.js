// Exact matches are automatically trashed after scanning. This read-only view
// also covers streamed groups, protected copies, and files that could not move.

import { api } from "./api.js";
import { openLightbox } from "./lightbox.js";
import { state } from "./state.js";
import { $, basename, escapeHtml, formatBytes, formatMtime, setPreviewAspectRatio, toast } from "./util.js";

function previewUrl(path) {
  return `/api/thumbnail?path=${encodeURIComponent(path)}&variant=preview`;
}

function thumbUrl(path) {
  return `/api/thumbnail?path=${encodeURIComponent(path)}`;
}

function copyRowHtml(g, member, index, lightboxIndex) {
  const selected = new Set(g.selected_for_removal || []);
  const isSelected = selected.has(member.path);
  const isSurvivor = !isSelected;
  const isSuggested = member.path === g.suggested_keep;
  const dims = Number.isFinite(member.width) && Number.isFinite(member.height)
    && member.width > 0 && member.height > 0
    ? `${member.width}×${member.height}`
    : "—";
  const chips = [
    isSurvivor ? '<span class="copy-chip keeping">Keeping</span>' : "",
    isSuggested ? '<span class="copy-chip suggested">Suggested</span>' : "",
    isSelected ? `<span class="copy-chip removing">${state.scanning ? "Automatic removal" : "Not auto-deleted"}</span>` : "",
    member.error ? `<span class="copy-chip error" title="${escapeHtml(member.error)}">Scan issue</span>` : "",
  ].join("");
  const thumb = thumbUrl(member.path);
  // Same hover affordances as the member grid: videos play on hover, GIFs
  // animate, stills linger-preview through the .thumb-wrap/.thumb-image hooks.
  const mediaPreview = member.media_type === "video"
    ? `<video class="hover-video" poster="${thumb}" data-src="/api/media?path=${encodeURIComponent(member.path)}" muted loop playsinline preload="none"></video>`
    : `<img class="thumb-image ${member.media_type === "gif" ? "hover-gif" : ""}" src="${thumb}" ${member.media_type === "gif" ? `data-thumbnail="${thumb}" data-src="/api/media?path=${encodeURIComponent(member.path)}"` : ""} alt="Preview of ${escapeHtml(basename(member.path))}" loading="lazy" decoding="async" />`;
  const mediaBadge = ["video", "gif"].includes(member.media_type)
    ? '<span class="video-preview-badge" aria-hidden="true">▶</span>'
    : "";
  const hasDims = Number.isFinite(member.width) && Number.isFinite(member.height)
    && member.width > 0 && member.height > 0;
  const previewDims = hasDims
    ? ` data-preview-width="${member.width}" data-preview-height="${member.height}"`
    : "";
  return `
    <article class="card copy-row ${isSelected ? "selected" : "keep"} ${index === state.memberFocus ? "focused" : ""}" data-path="${escapeHtml(member.path)}" data-index="${index}">
      <button class="copy-thumb thumb-wrap" data-path="${escapeHtml(member.path)}" data-index="${lightboxIndex}"${previewDims} type="button" aria-label="Open ${escapeHtml(basename(member.path))} in the comparison view">
        ${mediaPreview}${mediaBadge}
      </button>
      <div class="copy-info">
        <div class="name" title="${escapeHtml(member.path)}">${escapeHtml(basename(member.path))}</div>
        ${chips ? `<div class="copy-chips">${chips}</div>` : ""}
        <div class="path" title="${escapeHtml(member.path)}">${escapeHtml(member.path)}</div>
        <div class="card-meta">
          <span>${formatBytes(member.size)}</span>
          <span>${dims}</span>
          <span title="Modified">${escapeHtml(formatMtime(member.mtime))}</span>
        </div>
      </div>
      <div class="copy-side">
        <button class="linkish reveal" data-path="${escapeHtml(member.path)}" type="button">Reveal</button>
      </div>
    </article>`;
}

function renderExactReview(g) {
  const box = $("members");
  box.classList.remove("triage-grid");
  for (const id of ["memberPagination", "memberPaginationBottom", "memberSort", "memberModified"]) {
    const el = $(id);
    if (el) el.hidden = true;
  }

  const members = g.members || [];
  const deleted = new Set(g.deleted_paths || []);
  const keeper = members.find((member) => member.path === g.suggested_keep) || members[0];

  state.lightboxItems = members
    .filter((member) => !deleted.has(member.path))
    .map((member) => ({
      path: member.path,
      mediaType: member.media_type,
      keeper: g.suggested_keep,
      kind: g.kind,
      size: member.size,
      width: member.width,
      height: member.height,
      mtime: member.mtime,
    }));

  $("detailMeta").textContent =
    `${members.length} byte-identical copies · exact duplicates move to Trash automatically after scanning`;
  $("groupSelectionSummary").textContent = "";

  const lightboxIndexByPath = new Map(
    state.lightboxItems.map((item, lightboxIndex) => [item.path, lightboxIndex]),
  );

  const preview = keeper
    ? `<button class="exact-preview" type="button" aria-label="Open the identical preview in the comparison view">
         <img src="${previewUrl(keeper.path)}" alt="Preview shared by every copy" decoding="async" />
       </button>`
    : "";
  box.innerHTML = `
    <div class="exact-review">
      <div class="exact-banner">
        ${preview}
        <div class="exact-banner-copy">
          <strong>${members.length} identical copies</strong>
          <span class="muted">Byte-identical SHA-256 match. Exact duplicates move to Trash automatically after scanning, keeping one copy. Protected or unavailable files stay here; scan again to retry.</span>
        </div>
      </div>
      <div class="copy-list">
        ${members.map((member, index) => copyRowHtml(g, member, index, lightboxIndexByPath.get(member.path) ?? index)).join("")}
      </div>
    </div>`;

  box.querySelectorAll(".copy-thumb").forEach((thumb) => {
    setPreviewAspectRatio(thumb, thumb.dataset.previewWidth, thumb.dataset.previewHeight);
  });

  box.querySelectorAll(".hover-video").forEach((video) => {
    const wrap = video.closest(".thumb-wrap");
    video.addEventListener("loadedmetadata", () => {
      setPreviewAspectRatio(wrap, video.videoWidth, video.videoHeight);
    });
    wrap.addEventListener("pointerenter", () => {
      video.muted = true;
      if (!video.src) video.src = video.dataset.src;
      video.play().catch(() => {
        /* The static poster remains when the browser cannot play this codec. */
      });
    });
    wrap.addEventListener("pointerleave", () => {
      video.pause();
      video.removeAttribute("src");
      video.load();
    });
  });

  box.querySelectorAll(".hover-gif").forEach((image) => {
    const wrap = image.closest(".thumb-wrap");
    wrap.addEventListener("pointerenter", () => {
      image.src = image.dataset.src;
    });
    wrap.addEventListener("pointerleave", () => {
      image.src = image.dataset.thumbnail;
    });
  });

  box.querySelectorAll(".reveal").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        await api(`/api/reveal?path=${encodeURIComponent(btn.dataset.path)}&open=1`);
      } catch (error) {
        toast(error.message, "error");
      }
    });
  });

  box.querySelectorAll(".copy-thumb").forEach((thumb) => {
    thumb.addEventListener("click", () => {
      const path = thumb.closest(".copy-row")?.dataset.path;
      const lightboxIndex = state.lightboxItems.findIndex((item) => item.path === path);
      state.memberFocus = Number(thumb.closest(".copy-row")?.dataset.index || 0);
      openLightbox(lightboxIndex >= 0 ? lightboxIndex : 0);
    });
  });
  box.querySelector(".exact-preview")?.addEventListener("click", () => {
    const index = state.lightboxItems.findIndex((item) => item.path === keeper?.path);
    openLightbox(index >= 0 ? index : 0);
  });

  box.querySelectorAll(".copy-row").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("input, button, label, a")) return;
      state.memberFocus = Number(row.dataset.index);
      box.querySelectorAll(".copy-row").forEach((other) => other.classList.remove("focused"));
      row.classList.add("focused");
    });
  });
}

export { renderExactReview };
