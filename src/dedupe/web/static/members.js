// The detail pane: member cards, review flows, per-candidate trash/undo.

import { api } from "./api.js";
import { applyResultControls, ensureGroupVisible, loadGroups, markGroupListActive, rememberFocusedGroup, selectionFiltersActive, updateGroupListItem } from "./groups.js";
import { closeLightbox, openLightbox, updateLightbox } from "./lightbox.js";
import { currentGroup, isDecisionReview, isIndependentReview, isPagedIndependentReview, isGridPagedGroup, markGroupTouched, patchGroup } from "./model.js";
import { renderSwipeReview, swipeActive } from "./swipe.js";
import { scheduleRender } from "./render.js";
import { state } from "./state.js";
import { $, basename, escapeHtml, formatBytes, formatMtime, setPreviewAspectRatio, sleep, toast } from "./util.js";

const MEMBER_PAGE_SIZE = 50;
let renderedGroupSnapshot = "";

// Fit the stage and its actual controls above the action bar. This responds to
// open guides, wrapped captions, and short laptop screens, not a guessed vh.
function fitReviewStage() {
  const box = $("members");
  const media = box.querySelector(".swipe-media, .focus-card .thumb-wrap");
  if (!media || $("detailBody").hidden) return;
  const bounds = box.getBoundingClientRect();
  const controls = bounds.height - media.getBoundingClientRect().height;
  const footer = $("actionBar").getBoundingClientRect().height;
  const height = Math.max(180, Math.min(680, window.innerHeight - bounds.top - controls - footer - 28));
  box.style.setProperty("--review-stage-height", `${Math.floor(height)}px`);
}
const stageObserver = new ResizeObserver(() => requestAnimationFrame(fitReviewStage));
for (const id of ["main", "scanPanel", "exactRecovery", "actionBar"]) stageObserver.observe($(id));
window.addEventListener("resize", fitReviewStage);

let activeVideo = null;
function stopInlineVideo() {
  if (!activeVideo) return;
  activeVideo.pause();
  activeVideo.removeAttribute("src");
  activeVideo.load(); // Cancel buffering and restore the poster.
  activeVideo = null;
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopInlineVideo();
    $("members").querySelectorAll("video").forEach((video) => video.pause());
  }
});
$("members").addEventListener("click", (event) => {
  if (!event.target.closest("video")) stopInlineVideo();
}, true);

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

// Modified-time windows (seconds) for the Files tab's member filter.
// A month counts as 30 days.
const MODIFIED_CUTOFF_SECONDS = {
  hour: 3600,
  day: 86400,
  week: 7 * 86400,
  month: 30 * 86400,
};

const MODIFIED_LABELS = {
  hour: "the last hour",
  day: "the last day",
  week: "the last week",
  month: "the last month",
};

// Narrow a member list to files modified inside the selected window.
// "any" (or an unknown value) returns the list untouched; files without a
// recorded mtime never match an active window.
function applyModifiedFilter(members) {
  const window = MODIFIED_CUTOFF_SECONDS[state.memberModified];
  if (window == null) return members;
  const cutoff = Date.now() / 1000 - window;
  return (members || []).filter(
    (member) => member?.mtime != null && member.mtime >= cutoff,
  );
}

function syncMemberPagination(pageCount, summaryText, decisionReview) {
  const bars = [
    $("memberPagination"),
    $("memberPaginationBottom"),
  ].filter(Boolean);
  for (const bar of bars) {
    bar.hidden = !decisionReview && bar.id === "memberPaginationBottom";
    const prev = bar.querySelector(".member-prev");
    const next = bar.querySelector(".member-next");
    const summary = bar.querySelector(".member-page-summary");
    if (prev) prev.hidden = !decisionReview;
    if (next) next.hidden = !decisionReview;
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
      `${reviewed.size} of ${g.member_count} reviewed · ${deleted} in Trash · deletions are undoable`;
    return;
  }
  if (g.kind === "no_humans") {
    const reviewed = new Set(g.reviewed_paths || []);
    const selected = new Set(g.selected_for_removal || []);
    $("detailMeta").textContent =
      `${reviewed.size} of ${g.member_count} reviewed · ${selected.size} selected · detection can miss people`;
    return;
  }
  if (g.kind === "faces") {
    const reviewed = new Set(g.reviewed_paths || []);
    const selected = new Set(g.selected_for_removal || []);
    $("detailMeta").textContent =
      `${reviewed.size} of ${g.member_count} reviewed · ${selected.size} selected · face counts are estimates`;
    return;
  }
  if (isDecisionReview(g)) {
    const reviewed = new Set(g.reviewed_paths || []);
    const selected = new Set(g.selected_for_removal || []);
    const remaining = Math.max(0, g.member_count - reviewed.size);
    $("detailMeta").textContent =
      `${reviewed.size} reviewed · ${selected.size} marked Delete · ${remaining} remaining · confirm staged removals below`;
    return;
  }

  const keeper = (g.members || []).find((member) => member.path === g.suggested_keep);
  const keeperWhy = keeper
    ? ` Suggested keeper: ${basename(keeper.path)} (${keeper.width && keeper.height ? `${keeper.width}×${keeper.height}, ` : ""}${formatBytes(keeper.size)}), ranked by resolution, size, date, and path.`
    : "";
  $("detailMeta").textContent =
    `${formatBytes(g.reclaimable_bytes)} reclaimable · every member was directly verified against the suggested keeper.${keeperWhy}`;
}

async function selectGroup(id, { silent = false, preservePlayback = false } = {}) {
  memberObserver.disconnect();
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
  let g;
  try {
    g = await api(`/api/groups/${id}`);
  } catch (error) {
    if (state.selectToken !== myToken) return;
    if (error.status !== 404) throw error;
    // Auto-trash can dissolve a streamed group between listing and opening
    // it. Refresh instead of placing a stale-group error ahead of Undo.
    state.currentId = null;
    $("detailBody").hidden = true;
    $("detailEmpty").hidden = false;
    await loadGroups();
    return;
  }
  // A newer selection (or a cleared one) supersedes this fetch: bail out
  // rather than paint a stale group into the detail pane.
  if (state.selectToken !== myToken || g.id !== state.currentId) return;
  // Keep native video nodes on unchanged passive refreshes, even while paused
  // or starting playback. Replacing them loses the playhead and can race Play.
  // Changed data and explicit view changes still repaint.
  if (preservePlayback && preserveMemberFocus && JSON.stringify(g) === renderedGroupSnapshot
    && $("members").querySelector(".review-video, .swipe-media video")) return;
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
    low_resolution: "Low resolution",
    random_review: "Random review",
    faces: "Faces",
    similar: "Similar",
    all_files: `All files${g.root ? ` · ${basename(g.root)}` : ""}`,
  }[g.kind] || g.kind;
  $("detailTitle").textContent = isIndependentReview(g)
    ? `${kindLabel} · ${g.member_count} files`
    : `${kindLabel} · ${g.media_type} · ${g.member_count} files`;
  const deletedPaths = new Set(g.deleted_paths || []);
  const swipeMode = g.kind === "similar" && state.similarView === "swipe";
  $("btnMarkRemainingHuman").hidden =
    g.kind !== "no_humans" || !(g.members || []).some((member) => !deletedPaths.has(member.path));
  // In swipe mode the deck decides one pair at a time; the whole-group
  // distinct button only belongs to the list view.
  $("btnMarkDistinct").hidden = g.kind !== "similar" || swipeMode;
  updateSimilarViewToggle(g);
  $("nonHumanBanner").hidden = g.kind !== "no_humans";
  syncDeletedToggle(g);
  $("candidateReviewBanner").hidden = !(isDecisionReview(g) || swipeMode);
  $("candidateKeys").hidden = swipeMode;
  $("swipeKeys").hidden = !swipeMode;
  if (isDecisionReview(g)) {
    $("candidateReviewTitle").textContent = g.kind === "low_resolution"
      ? "Low-resolution deletion suggestions"
      : `${g.member_count}-file library check-in`;
    $("candidateReviewDescription").textContent = g.kind === "low_resolution"
      ? "These files are below 1 megapixel. Decide one at a time; nothing moves until final confirmation."
      : "A fresh random sample from this scan. Use the arrow keys to decide quickly. Keep decisions here are not remembered between scans.";
  } else if (swipeMode) {
    $("candidateReviewTitle").textContent = "Same photo, or different?";
    $("candidateReviewDescription").textContent =
      "Compare each copy against the reference on the left. Swipe left — or press ← — when it is the same photo (the copy moves to Trash, undoable); swipe right or press → for a different photo (keeps both and never re-pairs them).";
  }
  // The custom review layouts (exact copy list, similar swipe deck) own
  // selection themselves, so the checkbox toolbar stays hidden for them.
  document.querySelector(".selection-toolbar").hidden =
    isIndependentReview(g) || swipeMode || g.kind === "exact";
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

function renderMembers(g, { append = false } = {}) {
  memberObserver.disconnect();
  renderedGroupSnapshot = JSON.stringify(g);
  const box = $("members");
  if (!append) {
    stopInlineVideo();
    box.querySelectorAll("video").forEach((video) => {
      video.pause();
      video.removeAttribute("src");
      video.load();
    });
  }
  const focusReview = isPagedIndependentReview(g) && state.reviewView === "focus";
  const singleReview = isDecisionReview(g) || focusReview;
  $("reviewViewSwitch").hidden = !isPagedIndependentReview(g);
  $("btnGalleryView").setAttribute("aria-pressed", String(!focusReview));
  $("btnFocusView").setAttribute("aria-pressed", String(focusReview));
  $("previewSizeWrap").hidden = singleReview || swipeActive();
  $("reviewToolbar").hidden = isDecisionReview(g) || swipeActive();
  box.classList.toggle("focus-review", singleReview);
  // Custom review layouts own the member area entirely (their own pagination
  // model, lightbox list, and detail meta).
  if (g.kind === "similar" && state.similarView === "swipe") {
    renderSwipeReview(g);
    requestAnimationFrame(fitReviewStage);
    return;
  }
  if (g.kind === "exact") {
    box.replaceChildren();
    state.lightboxItems = [];
    return;
  }
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
  // The Files tab's modified-time filter narrows the cards (and the
  // lightbox order, which follows this same list) before paging.
  if (g.kind === "all_files") {
    allMembers = applyModifiedFilter(allMembers);
  }
  const triage = isPagedIndependentReview(g);
  box.classList.toggle("triage-grid", triage);
  if (triage && !state.showDeleted) {
    allMembers = allMembers.filter(
      (member) => !deletedPaths.has(member.path) || (!focusReview && state.trashedInPlace.has(member.path)),
    );
  }
  syncDeletedToggle(g);
  const decisionReview = isDecisionReview(g);
  const gridPaged = isGridPagedGroup(g);
  const pageCount = singleReview
    ? Math.max(1, allMembers.length)
    : gridPaged
      ? Math.max(1, Math.ceil(allMembers.length / MEMBER_PAGE_SIZE))
      : 1;
  state.memberPage = Math.max(0, Math.min(pageCount - 1, state.memberPage));
  if (singleReview) {
    state.memberFocus = Math.max(0, Math.min(allMembers.length - 1, state.memberFocus));
    state.memberPage = state.memberFocus;
  }
  const pageStart = singleReview ? state.memberFocus : 0;
  const members = singleReview
    ? allMembers.slice(state.memberFocus, state.memberFocus + 1)
    : gridPaged
    ? allMembers.slice(0, (state.memberPage + 1) * MEMBER_PAGE_SIZE)
    : allMembers;
  const summaryText = allMembers.length
    ? singleReview
      ? `${pageStart + 1} of ${allMembers.length}`
      : `${pageStart + 1}–${Math.min(pageStart + members.length, allMembers.length)} of ${allMembers.length}`
    : "0 results";
  syncMemberPagination(pageCount, summaryText, singleReview);
  $("memberPaginationBottom").hidden = true;
  if (gridPaged && !focusReview && members.length < allMembers.length) {
    const bottom = $("memberPaginationBottom");
    bottom.hidden = false;
    bottom.querySelector(".member-page-summary").textContent =
      `Scroll for ${Math.min(MEMBER_PAGE_SIZE, allMembers.length - members.length)} more results`;
    memberObserver.observe(bottom);
  }
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
  const modifiedSelect = $("memberModified");
  if (modifiedSelect) {
    const showModified = g.kind === "all_files";
    modifiedSelect.hidden = !showModified;
    if (showModified) {
      modifiedSelect.value = MODIFIED_CUTOFF_SECONDS[state.memberModified] != null
        ? state.memberModified
        : "any";
    }
  }
  if (gridPaged) {
    prefetchThumbnails(allMembers.slice(pageStart + members.length, pageStart + members.length + 8));
  }
  // The lightbox includes the whole group, even cards not yet loaded.
  const lightboxSource = gridPaged ? allMembers : members;
  state.lightboxItems = lightboxSource
    .filter((member) => !deletedPaths.has(member.path))
    .map((member) => lightboxItemFor(member, g));
  updateDetailMeta(g);
  updateGroupSelectionText(g);
  if (triage && !members.length) {
    const hiddenDeleted = !state.showDeleted && deletedPaths.size;
    const modifiedWindow = g.kind === "all_files" ? MODIFIED_LABELS[state.memberModified] : null;
    box.innerHTML = `<div class="triage-empty">${
      hiddenDeleted
        ? `Every remaining file is in Trash. Use <strong>${deletedPaths.size} in Trash · Show</strong> to restore one.`
        : modifiedWindow
          ? `No files in this folder were modified in ${modifiedWindow}.`
          : "Nothing left in this review pile."
    }</div>`;
    return;
  }

  const offset = append ? box.querySelectorAll(".card").length : 0;
  const rendered = document.createElement("div");
  rendered.innerHTML = members.slice(offset)
    .map((m, i) => {
      i += offset;
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
      const thumb = `/api/thumbnail?path=${encodeURIComponent(m.path)}${singleReview ? "&variant=preview" : ""}`;
      const memberIndex = singleReview ? state.memberFocus : i;
      const focused = singleReview || i === state.memberFocus ? "focused" : "";
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
        ? `<video class="${singleReview ? "review-video" : "hover-video"}" poster="${thumb}" ${singleReview ? 'controls src' : 'data-src'}="/api/media?path=${encodeURIComponent(m.path)}" muted loop playsinline preload="${singleReview ? "metadata" : "none"}" aria-label="Play ${escapeHtml(fileName)}"></video>`
        : `<img class="thumb-image ${m.media_type === "gif" && !singleReview ? "hover-gif" : ""}" src="${m.media_type === "gif" && singleReview ? `/api/media?path=${encodeURIComponent(m.path)}` : thumb}" ${m.media_type === "gif" && !singleReview ? `data-thumbnail="${thumb}" data-src="/api/media?path=${encodeURIComponent(m.path)}"` : ""} alt="Preview of ${escapeHtml(fileName)}" loading="lazy" decoding="async" />`;
      const overlayDelete = isPagedIndependentReview(g) && !deleted
        ? `<button class="thumb-delete delete-candidate" data-path="${escapeHtml(m.path)}" type="button" title="Move to Trash — one click, undo from the toast" aria-label="Move ${escapeHtml(fileName)} to Trash">Trash</button>`
        : "";
      const preview = deleted
        ? `<div class="thumb-wrap deleted-preview"${previewDimensions}><div class="thumb-fallback">Moved to Trash — undo available</div></div>`
        : `<div class="thumb-stack">
            <${singleReview && m.media_type === "video" ? "div" : 'button type="button"'} class="thumb-wrap" data-path="${escapeHtml(m.path)}" data-index="${lightboxIndex}"${previewDimensions} aria-label="Open preview for ${escapeHtml(fileName)}">
              ${badge}
              ${mediaPreview}
              ${["video", "gif"].includes(m.media_type) && !singleReview ? '<span class="video-preview-badge" aria-hidden="true">▶ Hover to play</span>' : ""}
            </${singleReview && m.media_type === "video" ? "div" : "button"}>
            ${singleReview ? "" : overlayDelete}
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
        <article class="card ${singleReview ? "focus-card" : ""} ${decisionReview ? "decision-card" : ""} ${isPagedIndependentReview(g) ? "triage-card" : ""} ${isKeep ? "keep" : ""} ${isSel ? "selected" : ""} ${deleted ? "deleted" : ""} ${focused}" data-path="${escapeHtml(m.path)}" data-index="${memberIndex}">
          ${preview}
          <div class="card-body">
            <div class="name" title="${escapeHtml(m.path)}">${escapeHtml(fileName)}</div>
            <div class="path" title="${escapeHtml(m.path)}">${escapeHtml(m.path)}</div>
            <div class="card-meta">
              <span>${formatBytes(m.size)}</span>
              <span>${dims}</span>
              <span class="file-extra" title="Modified">${escapeHtml(formatMtime(m.mtime))}</span>
              <span class="media-kind">${escapeHtml(m.media_type)}</span>
              ${m.face_count != null ? `<span class="face-count ${m.face_count > 1 ? "multi" : ""}" title="Faces detected by OpenCV (heuristic)">${m.face_count === 0 ? "No faces" : `${m.face_count} face${m.face_count === 1 ? "" : "s"}`}</span>` : ""}
              ${(m.male_face_count || 0) > 0 ? `<span class="face-count multi" title="Male faces estimated by InsightFace genderage (heuristic)">${m.male_face_count} male${m.male_face_count === 1 ? "" : "s"}</span>` : ""}
            </div>
            <div class="evidence">${escapeHtml(evidence)}</div>
            <div class="card-actions">
              ${actions}
              ${singleReview && !deleted ? `<button class="linkish inspect-media" data-index="${lightboxIndex}" type="button">Expand ↗</button>` : ""}
            </div>
          </div>
        </article>
      `;
    })
    .join("");

  rendered.querySelectorAll(".thumb-wrap").forEach((preview) => {
    setPreviewAspectRatio(
      preview,
      preview.dataset.previewWidth,
      preview.dataset.previewHeight,
    );
  });

  rendered.querySelectorAll(".thumb-image").forEach((image) => {
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

  rendered.querySelectorAll(".hover-video").forEach((video) => {
    const wrap = video.closest(".thumb-wrap");
    video.addEventListener("loadedmetadata", () => {
      setPreviewAspectRatio(wrap, video.videoWidth, video.videoHeight);
    });
    wrap.addEventListener("pointerenter", () => {
      stopInlineVideo();
      activeVideo = video;
      video.muted = true;
      if (!video.src) video.src = video.dataset.src;
      video.play().catch(() => {
        /* The static poster remains when the browser cannot play this codec. */
      });
    });
    wrap.addEventListener("pointerleave", () => {
      if (activeVideo === video) stopInlineVideo();
    });
  });

  rendered.querySelectorAll(".hover-gif").forEach((image) => {
    const wrap = image.closest(".thumb-wrap");
    wrap.addEventListener("pointerenter", () => {
      image.src = image.dataset.src;
    });
    wrap.addEventListener("pointerleave", () => {
      image.src = image.dataset.thumbnail;
    });
  });

  rendered.querySelectorAll(".sel-cb").forEach((cb) => {
    cb.addEventListener("change", async () => {
      g = currentGroup();
      const changedPath = cb.dataset.path;
      const checks = [...box.querySelectorAll(".sel-cb")];
      const pagePaths = new Set(checks.map((c) => c.dataset.path));
      // Preserve picks in unloaded batches while updating loaded checkboxes.
      const offPageKept = (g.selected_for_removal || []).filter((path) => !pagePaths.has(path));
      const selectedPaths = [
        ...offPageKept,
        ...checks.filter((c) => c.checked).map((c) => c.dataset.path),
      ];
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

  rendered.querySelectorAll(".candidate-delete").forEach((btn) => {
    btn.addEventListener("click", () => reviewCandidate(g, btn.dataset.path, true));
  });
  rendered.querySelectorAll(".candidate-keep").forEach((btn) => {
    btn.addEventListener("click", () => reviewCandidate(g, btn.dataset.path, false));
  });

  rendered.querySelectorAll(".reveal").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/reveal?path=${encodeURIComponent(btn.dataset.path)}&open=1`);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  });

  rendered.querySelectorAll(".delete-candidate").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      trashReviewCandidate(g, btn.dataset.path);
    });
  });

  rendered.querySelectorAll(".undo-delete").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      undoReviewCandidate(g, btn.dataset.path);
    });
  });

  rendered.querySelectorAll("button.thumb-wrap").forEach((el) => {
    el.addEventListener("click", () => {
      const i = Number(el.dataset.index);
      state.memberFocus = Number(el.closest(".card")?.dataset.index || 0);
      openLightbox(i);
    });
  });
  rendered.querySelectorAll(".inspect-media").forEach((button) => {
    button.addEventListener("click", () => openLightbox(Number(button.dataset.index)));
  });

  rendered.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("input, button, label, a")) return;
      state.memberFocus = Number(card.dataset.index);
      box.querySelectorAll(".card").forEach((c) => c.classList.remove("focused"));
      card.classList.add("focused");
    });
  });

  if (!append) box.replaceChildren();
  box.append(...rendered.childNodes);

  // The single-file stage has a stable height; decisions never move the page.
  requestAnimationFrame(fitReviewStage);
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
    if (!$("lightbox").hidden) {
      state.lightboxIndex = 0;
      updateLightbox();
    }
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
  if (isDecisionReview(current) || (isPagedIndependentReview(current) && state.reviewView === "focus")) {
    const nextIndex = Math.max(
      0,
      state.memberFocus + delta,
    );
    if (nextIndex === state.memberFocus) return;
    state.memberFocus = nextIndex;
    state.memberPage = nextIndex;
    // renderMembers clamps to the filtered list and preserves page scroll.
    renderMembers(current);
    return;
  }
}

// The footer follows the loaded cards, so reaching it extends the same list.
const memberObserver = new IntersectionObserver((entries) => {
  if (!entries.some((entry) => entry.isIntersecting)) return;
  const current = currentGroup();
  if (!current || !isGridPagedGroup(current) || swipeActive()
    || (isPagedIndependentReview(current) && state.reviewView === "focus")) return;
  state.memberPage += 1;
  renderMembers(current, { append: true });
}, { rootMargin: "0px 0px 240px 0px" });

// Similar groups default to the swipe deck; the header toggle flips to the
// classic card list (which keeps checkboxes, bulk selection, and the
// whole-group "Mark as distinct" button).
const SIMILAR_VIEW_KEY = "dedupe.similarView";
try {
  const savedView = localStorage.getItem(SIMILAR_VIEW_KEY);
  if (savedView === "grid" || savedView === "swipe") state.similarView = savedView;
} catch {
  /* private mode */
}

function updateSimilarViewToggle(g) {
  const btn = $("btnSimilarView");
  if (!btn) return;
  const show = g?.kind === "similar";
  btn.hidden = !show;
  if (!show) return;
  const swipe = state.similarView === "swipe";
  btn.setAttribute("aria-pressed", swipe ? "true" : "false");
  btn.textContent = swipe ? "☰ List view" : "⇄ Swipe review";
}

$("btnSimilarView")?.addEventListener("click", () => {
  state.similarView = state.similarView === "swipe" ? "grid" : "swipe";
  try {
    localStorage.setItem(SIMILAR_VIEW_KEY, state.similarView);
  } catch {
    /* private mode */
  }
  const g = currentGroup();
  if (g) selectGroup(g.id, { silent: true }).catch((e) => toast(e.message, "error"));
});

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

// The Files tab's modified-time filter: re-render the member cards
// immediately, starting back on the first page.
$("memberModified")?.addEventListener("change", (event) => {
  state.memberModified = event.target.value;
  state.memberPage = 0;
  state.memberFocus = 0;
  state.trashedInPlace.clear();
  const current = currentGroup();
  if (current) renderMembers(current);
});

const REVIEW_VIEW_KEY = "dedupe.reviewView";
const PREVIEW_SIZE_KEY = "dedupe.previewSize";
try {
  if (localStorage.getItem(REVIEW_VIEW_KEY) === "focus") state.reviewView = "focus";
  const size = Number(localStorage.getItem(PREVIEW_SIZE_KEY));
  if (size >= 240 && size <= 520 && (size - 240) % 40 === 0) {
    $("previewSize").value = String(size);
    $("members").style.setProperty("--tile-size", `${size}px`);
  }
} catch { /* Storage may be unavailable in private browsing. */ }

for (const [id, view] of [["btnGalleryView", "gallery"], ["btnFocusView", "focus"]]) {
  $(id).addEventListener("click", () => {
    state.reviewView = view;
    try { localStorage.setItem(REVIEW_VIEW_KEY, view); } catch { /* ignore */ }
    state.memberPage = view === "gallery" ? Math.floor(state.memberFocus / MEMBER_PAGE_SIZE) : state.memberFocus;
    const group = currentGroup();
    if (group) renderMembers(group);
  });
}
$("previewSize").addEventListener("input", (event) => {
  $("members").style.setProperty("--tile-size", `${event.target.value}px`);
  try { localStorage.setItem(PREVIEW_SIZE_KEY, event.target.value); } catch { /* ignore */ }
});
$("btnMediaDetails").addEventListener("click", () => {
  const show = $("detailBody").classList.toggle("show-file-details");
  $("btnMediaDetails").setAttribute("aria-pressed", String(show));
});

export { selectGroup, renderMembers, reviewCandidate, trashReviewCandidate, undoReviewCandidate, changeMemberPage, setMemberSelected };
