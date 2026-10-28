// Exact-match review: byte-identical files need no visual comparison, only a
// survivor. The view shows one shared preview and a compact list of copies —
// pick the keeper and every other copy is marked for removal; a per-row
// Remove toggle keeps fine control (keeping more than one copy) possible.

import { api } from "./api.js";
import { applyResultControls, selectionFiltersActive, updateGroupListItem } from "./groups.js";
import { openLightbox } from "./lightbox.js";
import { markGroupTouched, patchGroup } from "./model.js";
import { scheduleRender } from "./render.js";
import { state } from "./state.js";
import { $, basename, escapeHtml, formatBytes, formatMtime, setPreviewAspectRatio, toast } from "./util.js";

function previewUrl(path) {
  return `/api/thumbnail?path=${encodeURIComponent(path)}&variant=preview`;
}

function thumbUrl(path) {
  return `/api/thumbnail?path=${encodeURIComponent(path)}`;
}

function copyRowHtml(g, member, index, survivors, lightboxIndex) {
  const selected = new Set(g.selected_for_removal || []);
  const isSelected = selected.has(member.path);
  const isSurvivor = !isSelected;
  const isSuggested = member.path === g.suggested_keep;
  const soleSurvivor = isSurvivor && survivors.size === 1;
  const dims = Number.isFinite(member.width) && Number.isFinite(member.height)
    && member.width > 0 && member.height > 0
    ? `${member.width}×${member.height}`
    : "—";
  const chips = [
    isSurvivor ? '<span class="copy-chip keeping">Keeping</span>' : "",
    isSuggested ? '<span class="copy-chip suggested">Suggested</span>' : "",
    isSelected ? '<span class="copy-chip removing">Will be removed</span>' : "",
    member.error ? `<span class="copy-chip error" title="${escapeHtml(member.error)}">Scan issue</span>` : "",
  ].join("");
  const keepTitle = soleSurvivor
    ? "The surviving copy"
    : "Keep only this copy — every other copy is marked for removal";
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
        <button class="copy-keep" data-path="${escapeHtml(member.path)}" type="button" aria-pressed="${soleSurvivor}" title="${keepTitle}">
          <span class="copy-radio" aria-hidden="true"></span>Keep
        </button>
        <label class="copy-remove" title="Mark this copy for removal">
          <input type="checkbox" class="sel-cb" data-path="${escapeHtml(member.path)}" ${isSelected ? "checked" : ""} />
          Remove
        </label>
        <button class="linkish reveal" data-path="${escapeHtml(member.path)}" type="button">Reveal</button>
      </div>
    </article>`;
}

async function postSelection(g, selected) {
  const updated = await api("/api/selection", {
    method: "POST",
    body: JSON.stringify({
      group_id: g.id,
      selected: [...selected],
      scan_id: state.scanId,
    }),
  });
  markGroupTouched(g.id);
  patchGroup(updated);
  return updated;
}

function afterSelection(updated) {
  renderExactReview(updated);
  if (selectionFiltersActive() || !updateGroupListItem(updated)) {
    scheduleRender({ groupList: true });
  } else {
    applyResultControls();
  }
  scheduleRender({ selection: true });
}

function renderExactReview(g) {
  const box = $("members");
  box.classList.remove("triage-grid");
  for (const id of ["memberPagination", "memberPaginationBottom", "memberSort", "memberModified"]) {
    const el = $(id);
    if (el) el.hidden = true;
  }

  const members = g.members || [];
  const selected = new Set(g.selected_for_removal || []);
  const deleted = new Set(g.deleted_paths || []);
  const survivors = new Set(members.filter((member) => !selected.has(member.path)).map((member) => member.path));
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
    `${formatBytes(g.reclaimable_bytes)} reclaimable · ${members.length} byte-identical copies · pick the one to keep, the rest are marked for removal`;

  // The toolbar is hidden in this layout, but keep the selection summary text
  // current — the action bar and other consumers still read it.
  const selectionBase = `${selected.size} of ${members.length} selected for removal`;
  $("groupSelectionSummary").textContent =
    selected.size > 0 && !state.touchedGroups.has(g.id)
      ? `Suggested selection — ${selectionBase} · adjust freely`
      : selectionBase;

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
          <span class="muted">Byte-identical SHA-256 match — only the locations differ. Choose the copy that stays; the rest move to Trash when you run the action below.</span>
        </div>
      </div>
      <div class="copy-list">
        ${members.map((member, index) => copyRowHtml(g, member, index, survivors, lightboxIndexByPath.get(member.path) ?? index)).join("")}
      </div>
    </div>`;

  box.querySelectorAll(".copy-thumb").forEach((thumb) => {
    setPreviewAspectRatio(thumb, thumb.dataset.previewWidth, thumb.dataset.previewHeight);
  });

  box.querySelectorAll(".copy-keep").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const keepPath = btn.dataset.path;
      const picks = members.map((member) => member.path).filter((path) => path !== keepPath);
      try {
        afterSelection(await postSelection(g, picks));
      } catch (error) {
        toast(error.message, "error");
      }
    });
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

  box.querySelectorAll(".sel-cb").forEach((cb) => {
    cb.addEventListener("change", async () => {
      const next = new Set(g.selected_for_removal || []);
      if (cb.checked) next.add(cb.dataset.path);
      else next.delete(cb.dataset.path);
      try {
        afterSelection(await postSelection(g, next));
      } catch (error) {
        cb.checked = !cb.checked;
        toast(error.message, "error");
      }
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
