// The detail pane: member cards, review flows, per-candidate trash/undo.

import { api } from "./api.js";
import { applyResultControls, ensureGroupVisible, markGroupListActive, rememberFocusedGroup, selectionFiltersActive, updateGroupListItem } from "./groups.js";
import { closeLightbox, openLightbox, updateLightbox } from "./lightbox.js";
import { currentGroup, isDecisionReview, isIndependentReview, isPagedIndependentReview, markGroupTouched, patchGroup } from "./model.js";
import { scheduleRender } from "./render.js";
import { state } from "./state.js";
import { $, basename, escapeHtml, formatBytes, formatMtime, setPreviewAspectRatio, sleep, toast } from "./util.js";

const MEMBER_PAGE_SIZE = 50;

// The member sort select is kind-aware: each listed kind gets its own option
// set, and its first option is the server order (no client re-sort).
const MEMBER_SORT_OPTIONS = {
  faces: [
    ["faces-desc", "Most faces first"],
    ["faces-asc", "Fewest faces first"],
    ["newest", "Newest first"],
  ],
  all_files: [
    ["path", "Folder order (path)"],
    ["largest", "Largest first"],
    ["newest", "Newest first"],
    ["oldest", "Oldest first"],
  ],
};

function memberSortFor(kind) {
  const options = MEMBER_SORT_OPTIONS[kind];
  if (!options) return null;
  const saved = state.memberSortByKind[kind];
  return options.some(([value]) => value === saved) ? saved : options[0][0];
}

function syncMemberPagination(pageCount, summaryText) {
  const bars = [
    $("memberPagination"),
    $("memberPaginationBottom"),
  ].filter(Boolean);
  for (const bar of bars) {
    bar.hidden = pageCount <= 1;
    const prev = bar.querySelector(".member-prev");
    const next = bar.querySelector(".member-next");
    const summary = bar.querySelector(".member-page-summary");
    if (prev) prev.disabled = state.memberPage === 0;
    if (next) next.disabled = state.memberPage >= pageCount - 1;
    if (summary) summary.textContent = summaryText;
  }
}

function syncDeletedToggle(g) {
  const btn = $("btnToggleDeleted");
  if (!btn) return;
  const count = (g?.deleted_paths || []).length;
  const show = isPagedIndependentReview(g) && count > 0;
  btn.hidden = !show;
  if (show) {
    btn.textContent = state.showDeleted
      ? `Hide ${count} in Trash`
      : `${count} in Trash · Show`;
  }
}

function prefetchThumbnails(members) {
  for (const member of (members || []).slice(0, 8)) {
    if (!member?.path) continue;
    const image = new Image();
    image.decoding = "async";
    image.src = `/api/thumbnail?path=${encodeURIComponent(member.path)}`;
  }
}

// Update the "N of M selected" summary without rebuilding the member cards.
function updateGroupSelectionText(g) {
  const selected = new Set(g.selected_for_removal || []);
  const reviewedPaths = new Set(g.reviewed_paths || []);
  const sourceMembers = g.members || [];
  const reviewedCount = sourceMembers.filter((member) => reviewedPaths.has(member.path)).length;
  if (isIndependentReview(g)) {
    $("groupSelectionSummary").textContent =
      `${selected.size} selected · ${reviewedCount} of ${sourceMembers.length} reviewed`;
    return;
  }
  const base = `${selected.size} of ${sourceMembers.length} selected for removal`;
  // Groups arrive pre-selected by the smart-select suggestion; until the user
  // changes anything, label it as a suggestion rather than a done decision.
  $("groupSelectionSummary").textContent =
    selected.size > 0 && !state.touchedGroups.has(g.id)
      ? `Suggested selection — ${base} · adjust freely`
      : base;
}

// Full lightbox item for one member: the lightbox shows metadata and selection
// state, so it needs more than the path.
function lightboxItemFor(member, group) {
  return {
    path: member.path,
    mediaType: member.media_type,
    keeper: group.suggested_keep,
    kind: group.kind,
    size: member.size,
    width: member.width,
    height: member.height,
    mtime: member.mtime,
    similarityPercent: member.similarity_percent,
  };
}

// Sync one card's selection affordances (badge, classes, copy, checkbox) after
// a selection toggle, leaving the rest of the grid — and keyboard focus — intact.
function syncCardSelection(card, g, path) {
  const isSel = (g.selected_for_removal || []).includes(path);
  const reviewed = new Set(g.reviewed_paths || []).has(path);
  const isKeep = (path === g.suggested_keep || (isDecisionReview(g) && reviewed)) && !isSel;
  card.classList.toggle("keep", isKeep);
  card.classList.toggle("selected", isSel);
  const wrap = card.querySelector(".thumb-wrap");
  wrap?.querySelector(".thumb-badge")?.remove();
  const badgeText = isSel ? "Remove" : isKeep ? "Keep" : null;
  if (badgeText && wrap) {
    wrap.insertAdjacentHTML(
      "afterbegin",
      `<span class="thumb-badge ${isSel ? "remove" : "keep"}">${badgeText}</span>`,
    );
  }
  const title = card.querySelector(".selection-copy strong");
  const hint = card.querySelector(".selection-copy small");
  if (title) title.textContent = isSel ? "Selected for removal" : "Not selected";
  if (hint) hint.textContent = isSel ? "Click to keep this file" : "Click to remove this file";
  const checkbox = card.querySelector(".sel-cb");
  if (checkbox) checkbox.checked = isSel;
}

function updateDetailMeta(g) {
  if (g.kind === "all_files") {
    const reviewed = new Set(g.reviewed_paths || []);
    const deleted = (g.deleted_paths || []).length;
    $("detailMeta").textContent =
      `${reviewed.size} of ${g.member_count} reviewed · ${deleted} in Trash · every scanned media file in this folder, whether or not it matched a category · Trash is one click and undoable`;
    return;
  }
  if (g.kind === "no_humans") {
    const reviewed = new Set(g.reviewed_paths || []);
    const selected = new Set(g.selected_for_removal || []);
    $("detailMeta").textContent =
      `${reviewed.size} of ${g.member_count} reviewed · ${selected.size} selected for removal · Trash is one click and undoable · detector output is not a guarantee`;
    return;
  }
  if (g.kind === "faces") {
    const reviewed = new Set(g.reviewed_paths || []);
    const selected = new Set(g.selected_for_removal || []);
    $("detailMeta").textContent =
      `${reviewed.size} of ${g.member_count} reviewed · ${selected.size} selected for removal · Trash is one click and undoable · face counts are heuristic`;
    return;
  }
  if (isDecisionReview(g)) {
    const reviewed = new Set(g.reviewed_paths || []);
    const selected = new Set(g.selected_for_removal || []);
    const remaining = Math.max(0, g.member_count - reviewed.size);
    $("detailMeta").textContent =
      `${reviewed.size} reviewed · ${selected.size} marked Delete · ${remaining} remaining · staged deletions confirm with the Low-res + Random button below`;
    return;
  }

  const keeper = (g.members || []).find((member) => member.path === g.suggested_keep);
  const keeperWhy = keeper
    ? ` Suggested keeper: ${basename(keeper.path)} (${keeper.width && keeper.height ? `${keeper.width}×${keeper.height}, ` : ""}${formatBytes(keeper.size)}), ranked by resolution, size, date, and path.`
    : "";
  $("detailMeta").textContent =
    `${formatBytes(g.reclaimable_bytes)} reclaimable · every member was directly verified against the suggested keeper.${keeperWhy}`;
}

async function selectGroup(id, { silent = false } = {}) {
  const myToken = ++state.selectToken;
  const selectionStartFocus = document.activeElement;
  const preserveMemberFocus = silent && state.currentId === id;
  state.currentId = id;
  rememberFocusedGroup(id);
  if (!preserveMemberFocus) {
    state.memberFocus = 0;
    state.memberPage = 0;
    state.trashedInPlace.clear();
  }
  ensureGroupVisible(id);
  markGroupListActive(id);
  const g = await api(`/api/groups/${id}`);
  // A newer selection (or a cleared one) supersedes this fetch: bail out
  // rather than paint a stale group into the detail pane.
  if (state.selectToken !== myToken || g.id !== state.currentId) return;
  // A scan-completion refresh can finish while the user is moving through
  // member cards. Preserve the latest position and real DOM focus instead of
  // replacing the focused button underneath the next keystroke.
  const focusedMemberCard = preserveMemberFocus
    ? document.activeElement?.closest?.("#members .card")
    : null;
  const focusedMemberPath = focusedMemberCard?.dataset.path;
  if (focusedMemberCard) {
    const focusedIndex = Number(focusedMemberCard.dataset.index);
    if (Number.isFinite(focusedIndex)) state.memberFocus = focusedIndex;
  }
  if (isDecisionReview(g)) {
    const reviewed = new Set(g.reviewed_paths || []);
    if (!preserveMemberFocus) {
      const firstUnreviewed = (g.members || []).findIndex((member) => !reviewed.has(member.path));
      state.memberFocus = firstUnreviewed >= 0 ? firstUnreviewed : 0;
    }
  }
  const idx = state.groups.findIndex((group) => group.id === g.id);
  if (idx >= 0) state.groups[idx] = g;
  const allIdx = state.allGroups.findIndex((group) => group.id === g.id);
  if (allIdx >= 0) state.allGroups[allIdx] = g;
  updateGroupListItem(g);
  scheduleRender({ selection: true });
  $("detailEmpty").hidden = true;
  $("detailBody").hidden = false;
  const kindLabel = {
    no_humans: "Non-Human · no person detected",
    low_resolution: "Low resolution · under 1 megapixel",
    random_review: "Random review · fresh sample",
    faces: "Faces · OpenCV face counts",
    all_files: `All files${g.root ? ` · ${basename(g.root)}` : ""}`,
  }[g.kind] || g.kind;
  $("detailTitle").textContent = isIndependentReview(g)
    ? `${kindLabel} · ${g.member_count} files`
    : `${kindLabel} · ${g.media_type} · ${g.member_count} files`;
  const deletedPaths = new Set(g.deleted_paths || []);
  $("btnMarkRemainingHuman").hidden =
    g.kind !== "no_humans" || !(g.members || []).some((member) => !deletedPaths.has(member.path));
  $("btnMarkDistinct").hidden = g.kind !== "similar";
  $("nonHumanBanner").hidden = g.kind !== "no_humans";
  syncDeletedToggle(g);
  $("candidateReviewBanner").hidden = !isDecisionReview(g);
  if (isDecisionReview(g)) {
    $("candidateReviewTitle").textContent = g.kind === "low_resolution"
      ? "Low-resolution deletion suggestions"
      : `${g.member_count}-file library check-in`;
    $("candidateReviewDescription").textContent = g.kind === "low_resolution"
      ? "These files are below 1 megapixel. Decide one at a time; nothing moves until final confirmation."
      : "A fresh random sample from this scan. Use the arrow keys to decide quickly. Keep decisions here are not remembered between scans.";
  }
  document.querySelector(".selection-toolbar").hidden = isIndependentReview(g);
  $("smartRule").querySelectorAll("option").forEach((option) => {
    const candidateOnly = option.value === "select_candidates";
    option.disabled = isIndependentReview(g)
      ? !candidateOnly && option.value !== "deselect_all"
      : candidateOnly;
  });
  if ($("smartRule").selectedOptions[0]?.disabled) {
    $("smartRule").value = isIndependentReview(g) ? "deselect_all" : "automatic";
  }
  $("btnSelectSuggested").textContent =
    isIndependentReview(g) ? "Select reviewed candidates" : "Use suggested";
  renderMembers(g);
  if (focusedMemberPath) {
    const focusedCard = $("members").querySelector(
      `.card[data-path="${CSS.escape(focusedMemberPath)}"]`,
    );
    if (focusedCard) {
      $("members").querySelectorAll(".card").forEach((card) => card.classList.remove("focused"));
      focusedCard.classList.add("focused");
      focusedCard.querySelector(".thumb-wrap")?.focus({ preventScroll: true });
    }
  }
  // keep list item in view; explicit (non-silent) selection moves focus too,
  // so j/k navigation gives screen readers the group's announcement. Do not
  // steal focus back if the user reached a member or overlay while we fetched.
  const active = document.querySelector(`.group-item[data-id="${id}"]`);
  if (active && !silent && document.activeElement === selectionStartFocus) {
    active.scrollIntoView({ block: "nearest" });
    active.focus({ preventScroll: true });
  }
}

function renderMembers(g) {
  const box = $("members");
  const selected = new Set(g.selected_for_removal || []);
  const reviewedPaths = new Set(g.reviewed_paths || []);
  const deletedPaths = new Set(g.deleted_paths || []);
  let allMembers = g.members || [];
  const memberSort = memberSortFor(g.kind);
  if (g.kind === "faces" && memberSort !== "faces-desc") {
    allMembers = [...allMembers].sort(
      memberSort === "newest"
        ? (a, b) => (b.mtime || 0) - (a.mtime || 0)
        : (a, b) => (a.face_count || 0) - (b.face_count || 0),
    );
  }
  if (g.kind === "all_files" && memberSort !== "path") {
    const byPath = (a, b) => a.path.localeCompare(b.path);
    allMembers = [...allMembers].sort(
      memberSort === "largest"
        ? (a, b) => (b.size || 0) - (a.size || 0) || byPath(a, b)
        : memberSort === "oldest"
          ? (a, b) => (a.mtime || 0) - (b.mtime || 0) || byPath(a, b)
          : (a, b) => (b.mtime || 0) - (a.mtime || 0) || byPath(a, b),
    );
  }
  const triage = isPagedIndependentReview(g);
  box.classList.toggle("triage-grid", triage);
  if (triage && !state.showDeleted) {
    allMembers = allMembers.filter(
      (member) => !deletedPaths.has(member.path) || state.trashedInPlace.has(member.path),
    );
  }
  syncDeletedToggle(g);
  const decisionReview = isDecisionReview(g);
  const pageCount = decisionReview
    ? Math.max(1, allMembers.length)
    : isPagedIndependentReview(g)
      ? Math.max(1, Math.ceil(allMembers.length / MEMBER_PAGE_SIZE))
      : 1;
  state.memberPage = Math.max(0, Math.min(pageCount - 1, state.memberPage));
  if (decisionReview) {
    state.memberFocus = Math.max(0, Math.min(allMembers.length - 1, state.memberFocus));
    state.memberPage = state.memberFocus;
  }
  const pageStart = decisionReview ? state.memberFocus : state.memberPage * MEMBER_PAGE_SIZE;
  const members = decisionReview
    ? allMembers.slice(state.memberFocus, state.memberFocus + 1)
    : isPagedIndependentReview(g)
    ? allMembers.slice(pageStart, pageStart + MEMBER_PAGE_SIZE)
    : allMembers;
  const summaryText = allMembers.length
    ? decisionReview
      ? `${pageStart + 1} of ${allMembers.length}`
      : `${pageStart + 1}–${Math.min(pageStart + members.length, allMembers.length)} of ${allMembers.length}`
    : "0 results";
  syncMemberPagination(pageCount, summaryText);
  const sortSelect = $("memberSort");
  if (sortSelect) {
    const options = MEMBER_SORT_OPTIONS[g.kind] || null;
    sortSelect.hidden = !options;
    if (options && sortSelect.dataset.kind !== g.kind) {
      sortSelect.replaceChildren(
        ...options.map(([value, label]) => new Option(label, value)),
      );
      sortSelect.dataset.kind = g.kind;
    }
    if (options) sortSelect.value = memberSortFor(g.kind);
  }
  // The sort select lives in the top pagination bar, so sortable kinds keep
  // that bar visible even when the group fits on one page.
  if (MEMBER_SORT_OPTIONS[g.kind] && $("memberPagination")) $("memberPagination").hidden = false;
  if (isPagedIndependentReview(g)) {
    prefetchThumbnails(allMembers.slice(pageStart + members.length, pageStart + members.length + 8));
  }
  // Paged triage reviews sift through the whole group in the lightbox, not
  // just the 50-card page: the page slice is only a grid-rendering concern.
  const lightboxSource = isPagedIndependentReview(g) ? allMembers : members;
  state.lightboxItems = lightboxSource
    .filter((member) => !deletedPaths.has(member.path))
    .map((member) => lightboxItemFor(member, g));
  updateDetailMeta(g);
  updateGroupSelectionText(g);
  if (triage && !members.length) {
    const hiddenDeleted = !state.showDeleted && deletedPaths.size;
    box.innerHTML = `<div class="triage-empty">${
      hiddenDeleted
        ? `Every remaining file is in Trash. Use <strong>${deletedPaths.size} in Trash · Show</strong> to restore one.`
        : "Nothing left in this review pile."
    }</div>`;
    return;
  }

  box.innerHTML = members
    .map((m, i) => {
      const isSel = selected.has(m.path);
      const reviewed = reviewedPaths.has(m.path);
      const isKeep = (m.path === g.suggested_keep || (decisionReview && reviewed)) && !isSel;
      const deleted = deletedPaths.has(m.path);
      const mediaWidth = Number(m.width);
      const mediaHeight = Number(m.height);
      const hasDimensions = Number.isFinite(mediaWidth) && Number.isFinite(mediaHeight)
        && mediaWidth > 0 && mediaHeight > 0;
      const dims = hasDimensions ? `${mediaWidth}×${mediaHeight}` : "—";
      const previewDimensions = hasDimensions
        ? ` data-preview-width="${mediaWidth}" data-preview-height="${mediaHeight}"`
        : "";
      const thumb = `/api/thumbnail?path=${encodeURIComponent(m.path)}`;
      const memberIndex = decisionReview ? state.memberFocus : i;
      const focused = decisionReview || i === state.memberFocus ? "focused" : "";
      const lightboxIndex = state.lightboxItems.findIndex((item) => item.path === m.path);
      const fileName = basename(m.path);
      const badge = isSel
        ? `<span class="thumb-badge remove">Remove</span>`
        : isKeep
          ? `<span class="thumb-badge keep">Keep</span>`
          : "";
      const similarity = m.similarity_percent == null ? null : Number(m.similarity_percent);
      const similarityEvidence = Number.isFinite(similarity)
        ? `${similarity.toFixed(1).replace(/\.0$/, "")}% Similar to suggested keeper · fingerprint agreement, not a probability`
        : "Perceptual match to suggested keeper · similarity score unavailable";
      const evidence = g.kind === "exact"
        ? "Byte-identical SHA-256 match"
        : g.kind === "similar"
          ? similarityEvidence
          : g.kind === "low_resolution"
            ? `${dims} · ${((m.width || 0) * (m.height || 0) / 1_000_000).toFixed(2)} megapixels · below the 1 MP review threshold`
            : g.kind === "random_review"
              ? "Randomly selected from this scan for a quick keep-or-delete check"
              : g.kind === "faces"
                ? `OpenCV face detection found ${m.face_count} face${m.face_count === 1 ? "" : "s"} (heuristic, not a guarantee)`
                : g.kind === "all_files"
                  ? "Every scanned media file in this folder appears here, category or not"
                  : `OpenCV person detection analyzed ${m.human_frames_analyzed || 0} frame(s); no person detected — likely non-human`;
      const selectionTitle = isSel
        ? (isPagedIndependentReview(g) ? "Reviewed · selected" : "Selected for removal")
        : (isPagedIndependentReview(g) && reviewed ? "Reviewed · not selected" : "Not selected");
      const selectionHint = isSel
        ? "Click to keep this file"
        : (isPagedIndependentReview(g) ? "Click to review and remove" : "Click to remove this file");
      const mediaPreview = m.media_type === "video"
        ? `<video class="hover-video" poster="${thumb}" data-src="/api/media?path=${encodeURIComponent(m.path)}" muted loop playsinline preload="none"></video>`
        : `<img class="thumb-image ${m.media_type === "gif" ? "hover-gif" : ""}" src="${thumb}" ${m.media_type === "gif" ? `data-thumbnail="${thumb}" data-src="/api/media?path=${encodeURIComponent(m.path)}"` : ""} alt="Preview of ${escapeHtml(fileName)}" loading="lazy" />`;
      const overlayDelete = isPagedIndependentReview(g) && !deleted
        ? `<button class="thumb-delete delete-candidate" data-path="${escapeHtml(m.path)}" type="button" title="Move to Trash — one click, undo from the toast" aria-label="Move ${escapeHtml(fileName)} to Trash">Trash</button>`
        : "";
      const preview = deleted
        ? `<div class="thumb-wrap deleted-preview"${previewDimensions}><div class="thumb-fallback">Moved to Trash — undo available</div></div>`
        : `<div class="thumb-stack">
            <button class="thumb-wrap" data-path="${escapeHtml(m.path)}" data-index="${lightboxIndex}"${previewDimensions} type="button" aria-label="Open preview for ${escapeHtml(fileName)}">
              ${badge}
              ${mediaPreview}
              ${["video", "gif"].includes(m.media_type) ? '<span class="video-preview-badge" aria-hidden="true">▶ Hover to play</span>' : ""}
            </button>
            ${overlayDelete}
          </div>`;
      const actions = decisionReview
        ? `<div class="candidate-actions" role="group" aria-label="Keep or delete ${escapeHtml(fileName)}">
              <button class="candidate-decision candidate-delete" data-path="${escapeHtml(m.path)}" type="button"><kbd>←</kbd><span><strong>Delete</strong><small>Stage for removal</small></span></button>
              <button class="candidate-decision candidate-keep" data-path="${escapeHtml(m.path)}" type="button"><span><strong>Keep</strong><small>Leave untouched</small></span><kbd>→</kbd></button>
            </div>`
        : isPagedIndependentReview(g)
        ? `<button class="btn ${deleted ? "ghost undo-delete" : "danger delete-candidate"}" data-path="${escapeHtml(m.path)}" type="button" title="${deleted ? "Restore from Trash" : "Move to Trash — one click"}">${deleted ? "Undo" : "Trash"}</button>${deleted ? "" : `<button class="linkish reveal" data-path="${escapeHtml(m.path)}" type="button">Reveal</button>`}`
        : `<label class="selection-control">
                <input type="checkbox" class="sel-cb" data-path="${escapeHtml(m.path)}" ${isSel ? "checked" : ""} />
                <span class="selection-copy">
                  <strong>${selectionTitle}</strong>
                  <small>${selectionHint}</small>
                </span>
              </label>
              <button class="linkish reveal" data-path="${escapeHtml(m.path)}" type="button">Reveal</button>`;
      return `
        <article class="card ${decisionReview ? "decision-card" : ""} ${isPagedIndependentReview(g) ? "triage-card" : ""} ${isKeep ? "keep" : ""} ${isSel ? "selected" : ""} ${deleted ? "deleted" : ""} ${focused}" data-path="${escapeHtml(m.path)}" data-index="${memberIndex}">
          ${preview}
          <div class="card-body">
            <div class="name" title="${escapeHtml(m.path)}">${escapeHtml(fileName)}</div>
            <div class="path" title="${escapeHtml(m.path)}">${escapeHtml(m.path)}</div>
            <div class="card-meta">
              <span>${formatBytes(m.size)}</span>
              <span>${dims}</span>
              <span title="Modified">${escapeHtml(formatMtime(m.mtime))}</span>
              ${m.face_count != null ? `<span class="face-count ${m.face_count > 1 ? "multi" : ""}" title="Faces detected by OpenCV (heuristic)">${m.face_count === 0 ? "No faces" : `${m.face_count} face${m.face_count === 1 ? "" : "s"}`}</span>` : ""}
              ${(m.male_face_count || 0) > 0 ? `<span class="face-count multi" title="Male faces estimated by InsightFace genderage (heuristic)">${m.male_face_count} male${m.male_face_count === 1 ? "" : "s"}</span>` : ""}
            </div>
            <div class="evidence">${escapeHtml(evidence)}</div>
            <div class="card-actions">
              ${actions}
            </div>
          </div>
        </article>
      `;
    })
    .join("");

  box.querySelectorAll(".thumb-wrap").forEach((preview) => {
    setPreviewAspectRatio(
      preview,
      preview.dataset.previewWidth,
      preview.dataset.previewHeight,
    );
  });

  box.querySelectorAll(".thumb-image").forEach((image) => {
    const syncAspectRatio = () => {
      setPreviewAspectRatio(image.closest(".thumb-wrap"), image.naturalWidth, image.naturalHeight);
    };
    image.addEventListener("load", syncAspectRatio);
    image.addEventListener("error", () => {
      const fallback = document.createElement("div");
      fallback.className = "thumb-fallback";
      fallback.textContent = "No preview";
      image.replaceWith(fallback);
    });
    if (image.complete) syncAspectRatio();
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
      if (video.readyState > 0) video.currentTime = 0;
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
      const changedPath = cb.dataset.path;
      const checks = [...box.querySelectorAll(".sel-cb")];
      const selectedPaths = checks.filter((c) => c.checked).map((c) => c.dataset.path);
      const previousSelected = new Set(g.selected_for_removal || []);
      try {
        const updated = await api("/api/selection", {
          method: "POST",
          body: JSON.stringify({
            group_id: g.id,
            selected: selectedPaths,
            scan_id: state.scanId,
          }),
        });
        markGroupTouched(g.id);
        const idx = state.groups.findIndex((x) => x.id === g.id);
        if (idx >= 0) state.groups[idx] = updated;
        const aidx = state.allGroups.findIndex((x) => x.id === g.id);
        if (aidx >= 0) state.allGroups[aidx] = updated;
        // Patch only the cards whose selection flipped (keeper retention can
        // flip one other card) instead of rebuilding all cards — the rebuild
        // churned DOM and focus on every toggle.
        const nowSelected = new Set(updated.selected_for_removal || []);
        const flipped = new Set(
          [...previousSelected, ...nowSelected]
            .filter((path) => previousSelected.has(path) !== nowSelected.has(path)),
        );
        for (const path of flipped) {
          const card = box.querySelector(`.card[data-path="${CSS.escape(path)}"]`);
          if (card) syncCardSelection(card, updated, path);
          else if (path === changedPath) {
            // The card is gone (page changed under us); fall back to a render.
            renderMembers(updated);
            break;
          }
        }
        updateGroupSelectionText(updated);
        // Patch the single sidebar row unless a filter depends on selection.
        if (selectionFiltersActive() || !updateGroupListItem(updated)) {
          scheduleRender({ groupList: true });
        } else {
          applyResultControls();
        }
        scheduleRender({ selection: true });
      } catch (e) {
        toast(e.message, "error");
        cb.checked = !cb.checked;
      }
    });
  });

  box.querySelectorAll(".candidate-delete").forEach((btn) => {
    btn.addEventListener("click", () => reviewCandidate(g, btn.dataset.path, true));
  });
  box.querySelectorAll(".candidate-keep").forEach((btn) => {
    btn.addEventListener("click", () => reviewCandidate(g, btn.dataset.path, false));
  });

  box.querySelectorAll(".reveal").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/reveal?path=${encodeURIComponent(btn.dataset.path)}&open=1`);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  });

  box.querySelectorAll(".delete-candidate").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      trashReviewCandidate(g, btn.dataset.path);
    });
  });

  box.querySelectorAll(".undo-delete").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      undoReviewCandidate(g, btn.dataset.path);
    });
  });

  box.querySelectorAll("button.thumb-wrap").forEach((el) => {
    el.addEventListener("click", () => {
      const i = Number(el.dataset.index);
      state.memberFocus = Number(el.closest(".card")?.dataset.index || 0);
      openLightbox(i);
    });
  });

  box.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("input, button, label, a")) return;
      state.memberFocus = Number(card.dataset.index);
      box.querySelectorAll(".card").forEach((c) => c.classList.remove("focused"));
      card.classList.add("focused");
    });
  });

  if (decisionReview) {
    // Keep the candidate's media pinned to the vertical center of the screen:
    // each ← / → decision re-renders the card, and without this the scroll
    // position drifts until part of the image sits off-screen. The wrap's
    // aspect ratio is already set from the scan dimensions, so centering is
    // correct even before the thumbnail finishes loading.
    box.querySelector(".decision-card .thumb-wrap")
      ?.scrollIntoView({ block: "center", behavior: "instant" });
  }
}

async function reviewCandidate(group, path, remove) {
  if (!isDecisionReview(group)) return;
  if (state.reviewingCandidate) {
    // Held arrow keys repeat faster than the network round-trip: keep only
    // the latest decision and apply it when the in-flight one finishes.
    state.pendingReviewDecision = { direction: remove };
    return;
  }
  state.reviewingCandidate = true;
  const selected = new Set(group.selected_for_removal || []);
  const reviewed = new Set(group.reviewed_paths || []);
  if (remove) selected.add(path);
  else selected.delete(path);
  reviewed.add(path);
  const currentIndex = Math.max(
    0,
    (group.members || []).findIndex((member) => member.path === path),
  );
  try {
    const updated = await api("/api/selection", {
      method: "POST",
      body: JSON.stringify({
        group_id: group.id,
        selected: [...selected],
        reviewed: [...reviewed],
        decision_path: path,
        decision_remove: remove,
        scan_id: state.scanId,
      }),
    });
    const idx = state.groups.findIndex((candidate) => candidate.id === updated.id);
    if (idx >= 0) state.groups[idx] = updated;
    const allIdx = state.allGroups.findIndex((candidate) => candidate.id === updated.id);
    if (allIdx >= 0) state.allGroups[allIdx] = updated;
    for (const groups of [state.groups, state.allGroups]) {
      for (const candidate of groups) {
        if (!(candidate.members || []).some((member) => member.path === path)) continue;
        const candidateSelected = new Set(candidate.selected_for_removal || []);
        if (isIndependentReview(candidate)) {
          candidate.reviewed_paths = [...new Set([...(candidate.reviewed_paths || []), path])];
          if (remove) candidateSelected.add(path);
          else candidateSelected.delete(path);
        } else if (!remove) {
          candidateSelected.delete(path);
        }
        candidate.selected_for_removal = (candidate.members || [])
          .map((member) => member.path)
          .filter((candidatePath) => candidateSelected.has(candidatePath));
      }
    }

    const updatedReviewed = new Set(updated.reviewed_paths || []);
    const count = (updated.members || []).length;
    // Tell the user when the pile drained instead of silently staying put.
    const pileJustCompleted =
      count > 0
      && updatedReviewed.size >= count
      && (group.reviewed_paths || []).length < count;
    let nextIndex = currentIndex;
    for (let step = 1; step <= count; step += 1) {
      const candidateIndex = (currentIndex + step) % count;
      if (!updatedReviewed.has(updated.members[candidateIndex].path)) {
        nextIndex = candidateIndex;
        break;
      }
    }
    state.memberFocus = nextIndex;
    renderMembers(updated);
    if (pileJustCompleted) {
      toast("Review complete — every file in this group has a decision", "ok");
    }
    if (selectionFiltersActive() || !updateGroupListItem(updated)) {
      scheduleRender({ groupList: true });
    } else {
      applyResultControls();
    }
    scheduleRender({ selection: true });
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.reviewingCandidate = false;
    const pending = state.pendingReviewDecision;
    state.pendingReviewDecision = null;
    if (pending) {
      const current = currentGroup();
      const member = isDecisionReview(current)
        ? (current.members || [])[state.memberFocus]
        : null;
      if (member) await reviewCandidate(current, member.path, pending.direction);
    }
  }
}

function optimisticTrashGroup(group, path) {
  return {
    ...group,
    deleted_paths: [...new Set([...(group.deleted_paths || []), path])],
    selected_for_removal: (group.selected_for_removal || []).filter((item) => item !== path),
    reviewed_paths: [...new Set([...(group.reviewed_paths || []), path])],
  };
}

async function requestWithLockRetry(path, body) {
  let lastError;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    try {
      return await api(path, { method: "POST", body: JSON.stringify(body) });
    } catch (error) {
      lastError = error;
      if (error.status !== 409 || !/locked during active work/i.test(error.message || "")) {
        throw error;
      }
      await sleep(70 * (attempt + 1));
    }
  }
  throw lastError;
}

async function trashReviewCandidate(group, path, { fromLightbox = false } = {}) {
  if (!group || !path || (group.deleted_paths || []).includes(path) || state.deleteBusy.has(path)) {
    return;
  }
  state.deleteBusy.add(path);
  state.trashedInPlace.add(path);
  const previous = group;
  const optimistic = optimisticTrashGroup(group, path);
  patchGroup(optimistic);
  renderMembers(optimistic);
  scheduleRender({ groupList: true, selection: true });
  if (fromLightbox) {
    state.lightboxItems = state.lightboxItems.filter((item) => item.path !== path);
    if (!state.lightboxItems.length) closeLightbox();
    else {
      state.lightboxIndex = Math.min(state.lightboxIndex, state.lightboxItems.length - 1);
      updateLightbox();
    }
  }
  try {
    const updated = await requestWithLockRetry("/api/review-candidate/delete", {
      group_id: group.id,
      path,
      scan_id: state.scanId,
      dry_run: false,
    });
    patchGroup(updated);
    renderMembers(updated);
    scheduleRender({ groupList: true, selection: true });
    toast(`Moved ${basename(path)} to Trash`, "ok", {
      actionLabel: "Undo",
      onAction: () => undoReviewCandidate(updated, path, { fromLightbox }),
    });
  } catch (error) {
    state.trashedInPlace.delete(path);
    patchGroup(previous);
    renderMembers(previous);
    scheduleRender({ groupList: true, selection: true });
    if (fromLightbox && !$("lightbox").hidden) {
      const failedMember = (previous.members || []).find((member) => member.path === path);
      state.lightboxItems = [
        failedMember
          ? lightboxItemFor(failedMember, previous)
          : { path, mediaType: undefined, keeper: previous.suggested_keep, kind: previous.kind },
        ...state.lightboxItems,
      ];
      updateLightbox();
    }
    toast(error.message || "Could not move that file to Trash", "error");
  } finally {
    state.deleteBusy.delete(path);
  }
}

async function undoReviewCandidate(group, path, { fromLightbox = false } = {}) {
  if (!group || !path) return;
  while (state.deleteBusy.has(path)) await sleep(40);
  const live = currentGroup()?.id === group.id ? currentGroup() : group;
  try {
    const updated = await requestWithLockRetry("/api/review-candidate/undo", {
      group_id: live.id,
      path,
      scan_id: state.scanId,
    });
    patchGroup(updated);
    state.trashedInPlace.delete(path);
    renderMembers(updated);
    scheduleRender({ groupList: true, selection: true });
    // renderMembers rebuilt the lightbox list with the restored file back at
    // its sorted position; when the overlay is open, jump back to it.
    if (fromLightbox && !$("lightbox").hidden) {
      const restoredIndex = state.lightboxItems.findIndex((item) => item.path === path);
      if (restoredIndex >= 0) {
        state.lightboxIndex = restoredIndex;
        updateLightbox();
      }
    }
    toast("Image restored", "ok");
  } catch (error) {
    toast(error.message, "error");
  }
}

// Toggle one member's removal selection from outside the card grid (the
// lightbox). Keeper retention runs server-side and may flip a neighbor, so
// every visible card resyncs from the response.
async function setMemberSelected(group, path, wantSelected) {
  if (!group || isIndependentReview(group)) return null;
  const selected = new Set(group.selected_for_removal || []);
  if (wantSelected) selected.add(path);
  else selected.delete(path);
  const updated = await api("/api/selection", {
    method: "POST",
    body: JSON.stringify({
      group_id: group.id,
      selected: [...selected],
      scan_id: state.scanId,
    }),
  });
  markGroupTouched(group.id);
  patchGroup(updated);
  const box = $("members");
  box.querySelectorAll(".card[data-path]").forEach((card) => {
    syncCardSelection(card, updated, card.dataset.path);
  });
  updateGroupSelectionText(updated);
  if (selectionFiltersActive() || !updateGroupListItem(updated)) {
    scheduleRender({ groupList: true });
  } else {
    applyResultControls();
  }
  scheduleRender({ selection: true });
  return updated;
}

function changeMemberPage(delta) {
  const current = currentGroup();
  if (isDecisionReview(current)) {
    const nextIndex = Math.max(
      0,
      Math.min((current.members || []).length - 1, state.memberFocus + delta),
    );
    if (nextIndex === state.memberFocus) return;
    state.memberFocus = nextIndex;
    state.memberPage = nextIndex;
    // renderMembers centers the candidate's media; no pager scroll here, or
    // it would yank the view back to the top of the pane.
    renderMembers(current);
    return;
  }
  if (!current || !isPagedIndependentReview(current)) return;
  const visible = !state.showDeleted
    ? (current.members || []).filter((member) => !(current.deleted_paths || []).includes(member.path))
    : (current.members || []);
  const pageCount = Math.max(1, Math.ceil(visible.length / MEMBER_PAGE_SIZE));
  const nextPage = Math.max(0, Math.min(pageCount - 1, state.memberPage + delta));
  if (nextPage === state.memberPage) return;
  state.memberPage = nextPage;
  state.memberFocus = 0;
  state.trashedInPlace.clear();
  renderMembers(current);
  // Jump to the top pager so the next page of results is immediately visible.
  const topPager = $("memberPagination");
  if (topPager) topPager.scrollIntoView({ block: "start", behavior: "instant" });
}

document.querySelectorAll(".member-prev").forEach((btn) => {
  btn.addEventListener("click", () => changeMemberPage(-1));
});
document.querySelectorAll(".member-next").forEach((btn) => {
  btn.addEventListener("click", () => changeMemberPage(1));
});

$("memberSort")?.addEventListener("change", (event) => {
  const current = currentGroup();
  if (current && MEMBER_SORT_OPTIONS[current.kind]) {
    state.memberSortByKind[current.kind] = event.target.value;
  }
  state.memberPage = 0;
  state.memberFocus = 0;
  state.trashedInPlace.clear();
  if (current) renderMembers(current);
});

export { selectGroup, renderMembers, reviewCandidate, trashReviewCandidate, undoReviewCandidate, changeMemberPage, setMemberSelected };
