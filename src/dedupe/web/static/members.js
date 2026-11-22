// The detail pane: member cards, review flows, per-candidate trash/undo.

import { api } from "./api.js";
import { renderCropReview } from "./crop.js";
import { renderExactReview, syncCopyRow, syncExactSelectionState } from "./exact.js";
import {
  activeMemberFilters,
  applyResultControls,
  ensureGroupVisible,
  markGroupListActive,
  memberFilterSignature,
  memberMatchesSidebarFilters,
  rememberFocusedGroup,
  selectionFiltersActive,
  updateGroupListItem,
} from "./groups.js";
import { closeLightbox, lightboxOpen, openLightbox, updateLightbox } from "./lightbox.js";
import {
  currentGroup,
  isDecisionReview,
  isGridPagedGroup,
  isIndependentReview,
  isPagedIndependentReview,
  lightboxItemFor,
  markGroupTouched,
  memberDiffPills,
  patchGroup,
} from "./model.js";
import { boardActive, refreshBoardRow } from "./board.js";
import { renderSwipeReview, swipeActive } from "./swipe.js";
import { scheduleRender } from "./render.js";
import { state } from "./state.js";
import {
  $,
  basename,
  escapeHtml,
  formatBytes,
  formatMtime,
  isOverlayOpen,
  setPreviewAspectRatio,
  sleep,
  toast,
  withMediaVersion,
} from "./util.js";

const MEMBER_PAGE_SIZE = 50;
let renderedGroupSnapshot = "";
let renderedGroupId = null;
let renderedMemberFilterSig = "";

function getRenderedMemberFilterSignature() {
  return renderedMemberFilterSig;
}

// Fit the stage and its actual controls above the action bar. This responds to
// open guides, wrapped captions, and short laptop screens, not a guessed vh.
function fitReviewStage() {
  const box = $("members");
  const media = box.querySelector(".swipe-media, .focus-card .thumb-wrap");
  if (!media || $("detailBody").hidden) return;
  const bounds = box.getBoundingClientRect();
  const controls = bounds.height - media.getBoundingClientRect().height;
  // In the app-shell layout the detail pane scrolls on its own and the footer
  // sits below it, so the pane's bottom edge is the limit; otherwise the page
  // scrolls and the fixed footer covers the bottom of the viewport.
  const detail = $("main");
  const bottom = getComputedStyle(detail).overflowY === "auto"
    ? detail.getBoundingClientRect().bottom - 16
    : window.innerHeight - $("actionBar").getBoundingClientRect().height - 28;
  const height = Math.max(180, Math.min(680, bottom - bounds.top - controls));
  box.style.setProperty("--review-stage-height", `${Math.floor(height)}px`);
}
const stageObserver = new ResizeObserver(() => requestAnimationFrame(fitReviewStage));
for (const id of ["main", "scanPanel", "actionBar"]) stageObserver.observe($(id));
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
// Both people metrics are computed on demand for the open group.
const PEOPLE_SORT = "people-asc";
const PEOPLE_SORT_OPTION = [PEOPLE_SORT, "Fewest people first"];
const COVERAGE_SORT = "non-person-coverage-desc";
const COVERAGE_HINT = "Estimated area outside detected person boxes, averaged over sampled video or animation frames. Not exact segmentation. For ordering only.";
const COVERAGE_SORT_OPTION = [COVERAGE_SORT, "Non-person coverage", COVERAGE_HINT];
const MEMBER_SORT_OPTIONS = {
  faces: [
    ["faces-desc", "Most faces first"],
    ["faces-asc", "Fewest faces first"],
    ["newest", "Newest first"],
    PEOPLE_SORT_OPTION,
    COVERAGE_SORT_OPTION,
  ],
  all_files: [
    ["path", "Folder order (path)"],
    ["largest", "Largest first"],
    ["newest", "Newest first"],
    ["oldest", "Oldest first"],
    PEOPLE_SORT_OPTION,
    COVERAGE_SORT_OPTION,
  ],
  no_humans: [
    ["newest", "Newest first"],
    PEOPLE_SORT_OPTION,
    COVERAGE_SORT_OPTION,
  ],
};

function memberSortOptions(kind) {
  const options = MEMBER_SORT_OPTIONS[kind];
  if (!options || state.capabilities?.people_score !== false) return options || null;
  return options.filter(([value]) => value !== PEOPLE_SORT && value !== COVERAGE_SORT);
}

// People-likelihood filter cutoffs, calibrated on a labeled COCO sample:
// ≤ 0.05 kept ~80% of people-free photos with 2–4% containing (tiny) people;
// ≤ 0.20 kept ~93% with 5–9%. Unscored files never pass an active filter.
const PEOPLE_FILTER_MAX = { "probably-none": 0.05, "maybe-none": 0.2 };
const PEOPLE_FILTER_LABELS = {
  "probably-none": "probably show no people",
  "maybe-none": "may show no people",
};

function peopleFilterFor(kind) {
  if (!memberSortOptions(kind)?.some(([value]) => value === PEOPLE_SORT)) return null;
  return PEOPLE_FILTER_MAX[state.memberPeople] ?? null;
}

// Least likely to show people first; unscored files last; then folder order.
function byPeopleScore(a, b) {
  const aMissing = a.people_score == null;
  const bMissing = b.people_score == null;
  if (aMissing !== bMissing) return aMissing ? 1 : -1;
  return (a.people_score || 0) - (b.people_score || 0) || a.path.localeCompare(b.path);
}

// Highest coverage first, using the raw value rather than the displayed percent.
function byNonPersonCoverage(a, b) {
  const aMissing = a.non_person_coverage == null;
  const bMissing = b.non_person_coverage == null;
  if (aMissing !== bMissing) return aMissing ? 1 : -1;
  return (b.non_person_coverage ?? 0) - (a.non_person_coverage ?? 0)
    || a.path.localeCompare(b.path);
}

const coveragePercent = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

function peopleMetricsFor(kind) {
  const sort = memberSortFor(kind);
  const metrics = [];
  if (sort === COVERAGE_SORT) metrics.push("non_person_coverage");
  if (sort === PEOPLE_SORT || peopleFilterFor(kind) != null) metrics.push("people_score");
  return metrics;
}

// The endpoint has one scoring job at a time, and the open group owns it:
// opening another group that needs scores takes the job over, and the group
// left behind resumes when it is opened again. Refresh the open group when a
// job finishes so a changed sort or filter can request its still-missing
// metric. Remember attempts per metric so unreadable media never loops.
let peopleScoreScanId = null;
const peopleScoreRequested = new Set();
let peopleJob = null;

function peopleJobCurrent(job) {
  return peopleJob === job && job.scanId === state.scanId && !state.scanning;
}

function peopleScoringSummary(scoring, g) {
  if (!scoring?.running || scoring.scan_id !== state.scanId || scoring.group_id !== g.id
    || !peopleMetricsFor(g.kind).includes(scoring.metric)) return null;
  const name = scoring.metric === "non_person_coverage" ? "non-person coverage" : "people likelihood";
  return `Ranking by ${name}… ${scoring.done}/${scoring.total}`;
}

function showPeopleScoringProgress(scoring) {
  const g = currentGroup();
  const text = g && peopleScoringSummary(scoring, g);
  // Don't paint progress into another group's detail while its fetch is in flight.
  if (!text || renderedGroupId !== g.id || $("detailBody").hidden) return;
  $("memberPagination").querySelector(".member-page-summary").textContent = text;
}

async function ensurePeopleScores(g) {
  if (!state.scanId || state.scanning) return;
  if (peopleScoreScanId !== state.scanId) {
    peopleScoreScanId = state.scanId;
    peopleScoreRequested.clear();
    peopleJob = null;
  }
  if (peopleJob?.groupId === g.id) return;
  const keyFor = (groupId, metric) => `${groupId}\0${metric}`;
  const deleted = new Set(g.deleted_paths || []);
  const metric = peopleMetricsFor(g.kind).find((field) =>
    !peopleScoreRequested.has(keyFor(g.id, field))
      && (g.members || []).some((m) => m[field] == null && !deleted.has(m.path)),
  );
  if (!metric) return;
  // The job being replaced did not finish: let its group ask again later.
  if (peopleJob?.requested) peopleScoreRequested.delete(peopleJob.requested);
  const job = { scanId: state.scanId, groupId: g.id, requested: null, status: null };
  peopleJob = job;
  let status;
  let failure;
  let refresh = false;
  try {
    try {
      status = await api("/api/people-scores", {
        method: "POST",
        body: JSON.stringify({
          scan_id: job.scanId,
          group_id: g.id,
          ...(metric === "non_person_coverage" ? { metric } : {}),
        }),
      });
    } catch (error) {
      if (error.status !== 409 || !peopleJobCurrent(job)) throw error;
      // Another browser tab may have started a job for a different group.
      status = await api("/api/people-scores");
      if (!status.running || status.scan_id !== job.scanId) throw error;
    }
    if (!peopleJobCurrent(job) || status.scan_id !== job.scanId) return;
    refresh = true;
    const groupId = status.group_id;
    const activeMetric = status.metric;
    job.groupId = groupId;
    job.requested = keyFor(groupId, activeMetric);
    peopleScoreRequested.add(job.requested);
    while (status.running && peopleJobCurrent(job)) {
      job.status = status;
      showPeopleScoringProgress(status);
      await sleep(400);
      if (!peopleJobCurrent(job)) return;
      status = await api("/api/people-scores");
      if (status.scan_id !== job.scanId) {
        refresh = false;
        return;
      }
      if (status.group_id !== groupId || status.metric !== activeMetric) break;
    }
    if (status.scan_id === job.scanId && status.group_id === state.currentId
      && status.group_id === groupId && status.metric === activeMetric
      && peopleMetricsFor(currentGroup()?.kind).includes(activeMetric)) failure = status.error;
  } catch (error) {
    if (peopleJobCurrent(job)) {
      if (state.currentId === g.id) failure = error.message || "Could not rank by people metrics";
      peopleScoreRequested.add(keyFor(g.id, metric));
    }
  } finally {
    const current = peopleJobCurrent(job);
    if (peopleJob === job) peopleJob = null;
    if (current) {
      if (failure) toast(failure, "error");
      const open = currentGroup();
      if (refresh && open && (open.id === g.id || peopleMetricsFor(open.kind).length)) {
        await selectGroup(open.id, { silent: true }).catch((error) => toast(error.message, "error"));
      }
    }
  }
}

function memberSortFor(kind) {
  const options = memberSortOptions(kind);
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

function ensureMemberFilterNote() {
  let note = $("memberFilterNote");
  if (!note) {
    note = document.createElement("p");
    note.id = "memberFilterNote";
    note.className = "member-filter-note muted small";
    note.setAttribute("role", "status");
    note.hidden = true;
    const anchor = $("scanPreviewNote") || $("memberPagination");
    if (anchor) anchor.after(note);
    else $("members")?.before(note);
  }
  return note;
}

function updateMemberFilterNote(active, shown, total) {
  const note = ensureMemberFilterNote();
  if (!note) return;
  note.hidden = !active;
  if (active) {
    note.textContent = `Showing ${shown} of ${total} matching filter`;
  }
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

function syncMarkReviewedButton(g) {
  const btn = $("btnMarkGroupReviewed");
  if (!btn) return;
  btn.hidden = !g;
  if (!g) return;
  const reviewed = Boolean(g.reviewed);
  btn.setAttribute("aria-pressed", reviewed ? "true" : "false");
  btn.textContent = reviewed ? "Reviewed" : "Mark reviewed";
  btn.disabled = Boolean(state.scanning);
}

function syncScanningLock(g) {
  const note = $("scanPreviewNote");
  if (note) note.hidden = !state.scanning;
  const lock = Boolean(state.scanning);
  for (const id of [
    "btnSelectSuggested",
    "btnClearGroup",
    "btnSmartGroup",
    "btnSmartAll",
    "smartRule",
    "btnMarkDistinct",
    "btnMarkRemainingHuman",
    "btnCropAll",
    "cropLevelAll",
    "btnSkipAllCrops",
  ]) {
    const el = $(id);
    if (el) el.disabled = lock;
  }
  syncMarkReviewedButton(g);
}

function prefetchThumbnails(members) {
  for (const member of (members || []).slice(0, 8)) {
    if (!member?.path) continue;
    const image = new Image();
    image.decoding = "async";
    image.src = withMediaVersion(
      `/api/thumbnail?path=${encodeURIComponent(member.path)}`,
      member.path,
      member.media_version,
    );
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

// Sync one card's selection affordances (badge, classes, copy, checkbox) after
// a selection toggle, leaving the rest of the grid — and keyboard focus — intact.
function syncCardSelection(card, g, path) {
  if (card.classList.contains("copy-row")) {
    syncCopyRow(card, g, path);
    return;
  }
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
  if (checkbox) {
    checkbox.checked = isSel;
    checkbox.disabled = Boolean(state.scanning);
  }
}

function updateDetailMeta(g) {
  if (g.kind === "all_files" || g.kind === "sets") {
    const reviewed = new Set(g.reviewed_paths || []);
    const deleted = (g.deleted_paths || []).length;
    $("detailMeta").textContent =
      `${reviewed.size} of ${g.member_count} reviewed · ${deleted} in Trash · click a picture to Trash it · Enlarge opens it · deletions are undoable`;
    return;
  }
  if (g.kind === "no_humans") {
    const reviewed = new Set(g.reviewed_paths || []);
    const selected = new Set(g.selected_for_removal || []);
    $("detailMeta").textContent =
      `${reviewed.size} of ${g.member_count} reviewed · ${selected.size} selected · click a picture to Trash it · Enlarge opens it · detection can miss people`;
    return;
  }
  if (g.kind === "faces") {
    const reviewed = new Set(g.reviewed_paths || []);
    const selected = new Set(g.selected_for_removal || []);
    $("detailMeta").textContent =
      `${reviewed.size} of ${g.member_count} reviewed · ${selected.size} selected · click a picture to Trash it · Enlarge opens it · face counts are estimates`;
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
    ? ` Suggested keeper: ${basename(keeper.path)} (${keeper.width && keeper.height ? `${keeper.width}×${keeper.height}, ` : ""}${formatBytes(keeper.size)}), ${g.cleanup?.keeper_reason || "ranked by resolution, size, date, and path"}.`
    : "";
  $("detailMeta").textContent =
    `${formatBytes(g.reclaimable_bytes)} reclaimable · ${g.match_basis === "video_version" ? "matching video sections; inspect audio and edits" : "every member was directly verified against the suggested keeper"}.${keeperWhy}`;
}

function groupRenderSignature(g) {
  if (!g) return "";
  const memberPart = (g.members || [])
    .map((m) => `${m.path}:${m.media_version || ""}:${m.error || ""}:${m.people_score ?? ""}:${m.non_person_coverage ?? ""}:${m.face_count ?? ""}:${m.crop_state || ""}`)
    .join("\n");
  return [
    g.id,
    g.kind,
    Boolean(g.reviewed),
    g.suggested_keep || "",
    g.member_count || 0,
    g.reclaimable_bytes || 0,
    g.match_basis || "",
    state.scanning,
    state.reviewView,
    state.similarView,
    state.showDeleted,
    state.memberModified,
    state.memberPeople,
    memberSortFor(g.kind) || "",
    memberFilterSignature(),
    (g.selected_for_removal || []).join("\n"),
    (g.reviewed_paths || []).join("\n"),
    (g.deleted_paths || []).join("\n"),
    memberPart,
  ].join("\0");
}

// A fresh member list (another group, sort, or filter) starts on its first
// page with the detail pane scrolled to the top. In the app-shell layout the
// pane keeps its scroll offset across re-renders, so a restarted list would
// otherwise open at the bottom with its "load more" footer already in view.
function restartMemberList() {
  state.memberPage = 0;
  state.memberFocus = 0;
  state.trashedInPlace.clear();
  $("main").scrollTop = 0;
}

async function selectGroup(id, { silent = false, preservePlayback = false } = {}) {
  if (state.currentId !== id) memberObserver.disconnect();
  const myToken = ++state.selectToken;
  if (!silent) state.pendingGroupFocus = { id, from: document.activeElement };
  const sameGroup = state.currentId === id;
  const preserveMemberFocus = silent && sameGroup;
  state.currentId = id;
  rememberFocusedGroup(id);
  if (!preserveMemberFocus) restartMemberList();
  ensureGroupVisible(id);
  markGroupListActive(id);
  const g = await api(`/api/groups/${id}`);
  // A newer selection (or a cleared one) supersedes this fetch: bail out
  // rather than paint a stale group into the detail pane.
  if (state.selectToken !== myToken || g.id !== state.currentId) return;
  syncScanningLock(g);
  // Preserve unchanged media during silent background refreshes of the same group
  // so video playback, image scroll position, and appended gallery cards survive.
  // Explicit clicks (`silent === false`) always re-render from page 1 as expected.
  if (preservePlayback && preserveMemberFocus && groupRenderSignature(g) === renderedGroupSnapshot
    && $("members").querySelector(".card, .swipe-review")) {
    applyPendingGroupFocus(id);
    return;
  }
  // A scan-completion refresh can finish while the user is moving through
  // member cards. Preserve the latest position and real DOM focus instead of
  // replacing the focused button underneath the next keystroke.
  const activeMemberCard = document.activeElement?.closest?.("#members .card");
  const overlayOverSameGroup = sameGroup
    && (lightboxOpen() || isOverlayOpen($("cropEditor")));
  const keepMemberState = preserveMemberFocus
    || Boolean(sameGroup && activeMemberCard)
    || overlayOverSameGroup;
  const focusedMemberCard = keepMemberState
    ? (activeMemberCard || (overlayOverSameGroup
      ? $("members").querySelector(".card.focused")
      : null))
    : null;
  const focusedMemberPath = activeMemberCard && keepMemberState
    ? activeMemberCard.dataset.path
    : null;
  const focusedCropZone = Boolean(
    activeMemberCard && document.activeElement?.classList?.contains("crop-box"),
  );
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
  patchGroup(g);
  updateGroupListItem(g);
  scheduleRender({ selection: true });
  $("detailEmpty").hidden = true;
  $("detailBody").hidden = false;
  const kindLabel = {
    no_humans: "Non-Human · no person detected",
    low_resolution: "Low resolution",
    random_review: "Random review",
    faces: "Faces",
    sets: "Set · same subject or shoot",
    crop: "Crop · images & videos · borders & screen bars",
    similar: "Similar",
    all_files: `All files${g.root ? ` · ${basename(g.root)}` : ""}`,
  }[g.kind] || g.kind;
  $("detailTitle").textContent = isIndependentReview(g)
    ? `${kindLabel} · ${g.member_count} files`
    : `${kindLabel} · ${g.media_type} · ${g.member_count} files`;
  const deletedPaths = new Set(g.deleted_paths || []);
  const swipeMode = g.kind === "similar" && state.similarView !== "grid";
  $("btnMarkRemainingHuman").hidden =
    g.kind !== "no_humans" || !(g.members || []).some((member) => !deletedPaths.has(member.path));
  // In swipe mode the deck decides one pair at a time; the whole-group
  // distinct button only belongs to the list view.
  $("btnMarkDistinct").hidden = g.kind !== "similar" || swipeMode;
  $("btnCropAll").hidden = g.kind !== "crop";
  $("cropLevelAllWrap").hidden = g.kind !== "crop";
  $("btnToggleCropZones").hidden = g.kind !== "crop";
  syncMarkReviewedButton(g);
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
      focusedCard.querySelector(focusedCropZone ? ".crop-box.editable" : ".thumb-wrap")
        ?.focus({ preventScroll: true });
    }
  }
  applyPendingGroupFocus(id);
}

// Keep the list item in view; an explicit (non-silent) selection moves focus
// too, so j/k navigation gives screen readers the group's announcement. The
// intent survives a superseded fetch (a tab switch's silent refresh of the
// same group can overtake it), and is dropped if the user moved focus
// elsewhere — a member card or an overlay — while the fetch ran.
function applyPendingGroupFocus(id) {
  const pending = state.pendingGroupFocus;
  if (!pending || pending.id !== id) return;
  state.pendingGroupFocus = null;
  if (document.activeElement !== pending.from) return;
  const active = document.querySelector(`.group-item[data-id="${id}"]`);
  if (!active) return;
  active.scrollIntoView({ block: "nearest" });
  active.focus({ preventScroll: true });
}

function cardPreviewHtml(g, m, lightboxIndex, { singleReview, isKeep, isSel, deleted, dims, previewDimensions, clickTrash }) {
  const fileName = basename(m.path);
  const thumb = withMediaVersion(
    `/api/thumbnail?path=${encodeURIComponent(m.path)}${singleReview ? "&variant=preview" : ""}`,
    m.path,
    m.media_version,
  );
  const badge = m.protected
    ? '<span class="thumb-badge keep">Protected library</span>'
    : isSel
      ? '<span class="thumb-badge remove">Remove</span>'
      : isKeep
        ? '<span class="thumb-badge keep">Keep</span>'
        : "";
  const mediaPreview = m.media_type === "video"
    ? `<video class="${singleReview ? "review-video" : "hover-video"}" poster="${escapeHtml(thumb)}" ${singleReview ? "controls src" : "data-src"}="/api/media?path=${encodeURIComponent(m.path)}" muted loop playsinline preload="${singleReview ? "metadata" : "none"}" aria-label="Play ${escapeHtml(fileName)}"></video>`
    : `<img class="thumb-image ${m.media_type === "gif" && !singleReview ? "hover-gif" : ""}" src="${escapeHtml(m.media_type === "gif" && singleReview ? `/api/media?path=${encodeURIComponent(m.path)}` : thumb)}" ${m.media_type === "gif" && !singleReview ? `data-thumbnail="${escapeHtml(thumb)}" data-src="/api/media?path=${encodeURIComponent(m.path)}"` : ""} alt="Preview of ${escapeHtml(fileName)}" loading="lazy" decoding="async" />`;
  const overlayEnlarge = clickTrash
    ? `<button class="thumb-enlarge inspect-media" data-index="${lightboxIndex}" type="button" title="Enlarge in the lightbox" aria-label="Enlarge ${escapeHtml(fileName)}">Enlarge</button>`
    : "";
  if (deleted) {
    return `<div class="thumb-wrap deleted-preview"${previewDimensions}><div class="thumb-fallback">Moved to Trash — undo available</div></div>`;
  }
  return `<div class="thumb-stack">
    <${singleReview && m.media_type === "video" ? "div" : 'button type="button"'} class="thumb-wrap" data-path="${escapeHtml(m.path)}" data-index="${lightboxIndex}"${clickTrash ? ' data-click-trash="1" title="Move to Trash — one click, undo from the toast"' : ""}${previewDimensions} aria-label="${clickTrash ? `Move ${escapeHtml(fileName)} to Trash` : `Open preview for ${escapeHtml(fileName)}`}">
      ${badge}
      ${mediaPreview}
      ${["video", "gif"].includes(m.media_type) && !singleReview ? '<span class="video-preview-badge" aria-hidden="true">▶ Hover to play</span>' : ""}
    </${singleReview && m.media_type === "video" ? "div" : "button"}>
    ${singleReview ? "" : overlayEnlarge}
  </div>`;
}

function cardActionsHtml(g, m, lightboxIndex, { singleReview, decisionReview, deleted, isSel, reviewed, clickTrash }) {
  const fileName = basename(m.path);
  const lock = Boolean(state.scanning);
  if (m.protected) {
    return `<span class="protected-label">Protected library</span><button class="linkish reveal" data-path="${escapeHtml(m.path)}" type="button">Reveal</button>`;
  }
  if (decisionReview && !deleted) {
    return `<div class="candidate-actions" role="group" aria-label="Keep or delete ${escapeHtml(fileName)}">
      <button class="candidate-decision candidate-delete" data-path="${escapeHtml(m.path)}" type="button"${lock ? " disabled" : ""}><kbd>←</kbd><span><strong>Delete</strong><small>Stage for removal</small></span></button>
      <button class="candidate-decision candidate-keep" data-path="${escapeHtml(m.path)}" type="button"${lock ? " disabled" : ""}><span><strong>Keep</strong><small>Leave untouched</small></span><kbd>→</kbd></button>
    </div><button class="linkish reveal" data-path="${escapeHtml(m.path)}" type="button">Reveal</button><button class="linkish inspect-media" data-index="${lightboxIndex}" type="button">Expand ↗</button>`;
  }
  if (isPagedIndependentReview(g)) {
    const base = deleted
      ? `<button class="btn ghost undo-delete" data-path="${escapeHtml(m.path)}" type="button" title="Restore from Trash"${lock ? " disabled" : ""}>Undo</button>`
      : clickTrash
        ? `<button class="btn ghost enlarge-candidate inspect-media" data-index="${lightboxIndex}" type="button" title="Enlarge in the lightbox" aria-label="Enlarge ${escapeHtml(fileName)}">Enlarge</button><button class="linkish reveal" data-path="${escapeHtml(m.path)}" type="button">Reveal</button>`
        : `<button class="btn danger delete-candidate" data-path="${escapeHtml(m.path)}" type="button" title="Move to Trash — one click"${lock ? " disabled" : ""}>Trash</button><button class="linkish reveal" data-path="${escapeHtml(m.path)}" type="button">Reveal</button>`;
    return `${base}${singleReview && !deleted && !clickTrash ? `<button class="linkish inspect-media" data-index="${lightboxIndex}" type="button">Expand ↗</button>` : ""}`;
  }
  const selectionTitle = isSel ? "Selected for removal" : "Not selected";
  const selectionHint = isSel ? "Click to keep this file" : "Click to remove this file";
  return `<label class="selection-control">
    <input type="checkbox" class="sel-cb" data-path="${escapeHtml(m.path)}" ${isSel ? "checked" : ""} ${lock ? "disabled" : ""} />
    <span class="selection-copy">
      <strong>${selectionTitle}</strong>
      <small>${selectionHint}</small>
    </span>
  </label>
  <button class="linkish reveal" data-path="${escapeHtml(m.path)}" type="button">Reveal</button>`;
}

function cardOptionsFor(g, m, { selected, reviewedPaths, deletedPaths, singleReview, decisionReview }) {
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
  const clickTrash = isPagedIndependentReview(g) && !deleted && !m.protected
    && !(singleReview && m.media_type === "video");
  return {
    singleReview,
    decisionReview,
    isSel,
    reviewed,
    isKeep,
    deleted,
    dims,
    previewDimensions,
    clickTrash,
  };
}

function currentLightboxSource(g) {
  const deletedPaths = new Set(g.deleted_paths || []);
  const focusReview = isPagedIndependentReview(g) && state.reviewView === "focus";
  const singleReview = isDecisionReview(g) || focusReview;
  let allMembers = g.members || [];
  const memberSort = memberSortFor(g.kind);
  const peopleMax = peopleFilterFor(g.kind);
  if (peopleMax != null) {
    allMembers = allMembers.filter((m) => m.people_score != null && m.people_score <= peopleMax);
  }
  if (memberSort === COVERAGE_SORT) allMembers = [...allMembers].sort(byNonPersonCoverage);
  else if (memberSort === PEOPLE_SORT) allMembers = [...allMembers].sort(byPeopleScore);
  else if (g.kind === "faces" && memberSort !== "faces-desc") {
    allMembers = [...allMembers].sort(
      memberSort === "newest"
        ? (a, b) => (b.mtime || 0) - (a.mtime || 0)
        : (a, b) => (a.face_count || 0) - (b.face_count || 0),
    );
  } else if (g.kind === "all_files" && memberSort !== "path") {
    const byPath = (a, b) => a.path.localeCompare(b.path);
    allMembers = [...allMembers].sort(
      memberSort === "largest"
        ? (a, b) => (b.size || 0) - (a.size || 0) || byPath(a, b)
        : memberSort === "oldest"
          ? (a, b) => (a.mtime || 0) - (b.mtime || 0) || byPath(a, b)
          : (a, b) => (b.mtime || 0) - (a.mtime || 0) || byPath(a, b),
    );
  }
  if (g.kind === "all_files") allMembers = applyModifiedFilter(allMembers);
  const triage = isPagedIndependentReview(g);
  if (triage && !state.showDeleted) {
    allMembers = allMembers.filter(
      (member) => !deletedPaths.has(member.path) || (!focusReview && state.trashedInPlace.has(member.path)),
    );
  }
  const sidebarFilters = isIndependentReview(g) ? activeMemberFilters() : null;
  if (sidebarFilters?.anyActive) {
    allMembers = allMembers.filter((member) => memberMatchesSidebarFilters(member, sidebarFilters));
  }
  const gridPaged = isGridPagedGroup(g);
  const members = singleReview
    ? allMembers.slice(state.memberFocus, state.memberFocus + 1)
    : gridPaged
      ? allMembers.slice(0, (state.memberPage + 1) * MEMBER_PAGE_SIZE)
      : allMembers;
  return (gridPaged ? allMembers : members).filter((member) => !deletedPaths.has(member.path));
}

function syncLightboxItemsAndIndices(g) {
  state.lightboxItems = currentLightboxSource(g).map((member) => lightboxItemFor(member, g));
  const indexByPath = new Map(state.lightboxItems.map((item, idx) => [item.path, idx]));
  $("members").querySelectorAll(":scope > .card[data-path]").forEach((card) => {
    const idx = indexByPath.get(card.dataset.path);
    if (idx == null) return;
    card.querySelectorAll(".thumb-wrap, .inspect-media").forEach((el) => {
      el.dataset.index = String(idx);
    });
  });
  return indexByPath;
}

function patchTriageCardDeleted(card, g, path, isDeleted) {
  const member = (g.members || []).find((item) => item.path === path);
  if (!card || !member) return false;
  const focusReview = isPagedIndependentReview(g) && state.reviewView === "focus";
  if (focusReview) return false;
  const hadFocus = card.contains(document.activeElement);
  const indexByPath = syncLightboxItemsAndIndices(g);
  const lightboxIndex = indexByPath.get(path) ?? -1;
  const selected = new Set(g.selected_for_removal || []);
  const reviewedPaths = new Set(g.reviewed_paths || []);
  const deletedPaths = new Set(g.deleted_paths || []);
  const opts = cardOptionsFor(g, member, {
    selected,
    reviewedPaths,
    deletedPaths,
    singleReview: false,
    decisionReview: isDecisionReview(g),
  });

  card.classList.toggle("deleted", isDeleted);
  syncCardSelection(card, g, path);

  const oldPreview = card.querySelector(":scope > .thumb-stack, :scope > .deleted-preview");
  if (oldPreview) {
    const temp = document.createElement("div");
    temp.innerHTML = cardPreviewHtml(g, member, lightboxIndex, opts).trim();
    const freshPreview = temp.firstElementChild;
    if (freshPreview) {
      oldPreview.replaceWith(freshPreview);
      const wrap = freshPreview.classList.contains("thumb-wrap")
        ? freshPreview
        : freshPreview.querySelector(".thumb-wrap");
      if (wrap) {
        setPreviewAspectRatio(wrap, wrap.dataset.previewWidth, wrap.dataset.previewHeight);
      }
      const img = freshPreview.querySelector(".thumb-image");
      if (img?.complete && img.naturalWidth > 0) {
        setPreviewAspectRatio(wrap, img.naturalWidth, img.naturalHeight);
      }
    }
  }

  const actionsEl = card.querySelector(".card-actions");
  if (actionsEl) {
    actionsEl.innerHTML = cardActionsHtml(g, member, lightboxIndex, opts);
  }

  if (hadFocus) {
    if (isDeleted) {
      card.querySelector(".undo-delete")?.focus({ preventScroll: true });
    } else {
      (card.querySelector("button.thumb-wrap") || card.querySelector(".enlarge-candidate"))
        ?.focus({ preventScroll: true });
    }
  }
  return true;
}

function renderMembers(g, { append = false } = {}) {
  memberObserver.disconnect();
  // The Similar board owns the screen; the hidden detail pane stays as is.
  if (boardActive()) {
    renderedGroupSnapshot = "";
    return;
  }
  renderedGroupSnapshot = groupRenderSignature(g);
  renderedGroupId = g.id;
  renderedMemberFilterSig = memberFilterSignature();
  syncScanningLock(g);
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
  if (g.kind !== "crop") {
    box.classList.remove("crop-grid");
    delete box.dataset.cropGroup;
  }
  // Custom review layouts own the member area entirely (their own pagination
  // model, lightbox list, and detail meta).
  if (g.kind === "similar" && state.similarView !== "grid") {
    updateMemberFilterNote(false, 0, 0);
    renderSwipeReview(g);
    requestAnimationFrame(fitReviewStage);
    return;
  }
  if (g.kind === "exact") {
    updateMemberFilterNote(false, 0, 0);
    renderExactReview(g);
    return;
  }
  if (g.kind === "crop") {
    const filters = activeMemberFilters();
    if (filters.anyActive) {
      const total = (g.members || []).length;
      const filteredMembers = (g.members || []).filter((m) => memberMatchesSidebarFilters(m, filters));
      updateMemberFilterNote(true, filteredMembers.length, total);
      renderCropReview({ ...g, members: filteredMembers, member_count: filteredMembers.length });
    } else {
      updateMemberFilterNote(false, 0, 0);
      renderCropReview(g);
    }
    return;
  }
  const selected = new Set(g.selected_for_removal || []);
  const reviewedPaths = new Set(g.reviewed_paths || []);
  const deletedPaths = new Set(g.deleted_paths || []);
  let allMembers = g.members || [];
  const memberSort = memberSortFor(g.kind);
  const peopleMax = peopleFilterFor(g.kind);
  if (peopleMetricsFor(g.kind).length) ensurePeopleScores(g);
  if (peopleMax != null) {
    allMembers = allMembers.filter((m) => m.people_score != null && m.people_score <= peopleMax);
  }
  if (memberSort === COVERAGE_SORT) {
    allMembers = [...allMembers].sort(byNonPersonCoverage);
  } else if (memberSort === PEOPLE_SORT) {
    allMembers = [...allMembers].sort(byPeopleScore);
  } else if (g.kind === "faces" && memberSort !== "faces-desc") {
    allMembers = [...allMembers].sort(
      memberSort === "newest"
        ? (a, b) => (b.mtime || 0) - (a.mtime || 0)
        : (a, b) => (a.face_count || 0) - (b.face_count || 0),
    );
  } else if (g.kind === "all_files" && memberSort !== "path") {
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
  const totalBeforeSidebarFilter = allMembers.length;
  const sidebarFilters = isIndependentReview(g) ? activeMemberFilters() : null;
  if (sidebarFilters?.anyActive) {
    allMembers = allMembers.filter((member) => memberMatchesSidebarFilters(member, sidebarFilters));
    updateMemberFilterNote(true, allMembers.length, totalBeforeSidebarFilter);
  } else {
    updateMemberFilterNote(false, 0, 0);
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
  const resultCount = allMembers.length
    ? singleReview
      ? `${pageStart + 1} of ${allMembers.length}`
      : `${pageStart + 1}–${Math.min(pageStart + members.length, allMembers.length)} of ${allMembers.length}`
    : "0 results";
  const summaryText = peopleScoringSummary(peopleJob?.status, g) ?? resultCount;
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
    const options = memberSortOptions(g.kind);
    sortSelect.hidden = !options;
    const optionsKey = options ? `${g.kind}:${options.length}` : "";
    if (options && sortSelect.dataset.kind !== optionsKey) {
      sortSelect.replaceChildren(
        ...options.map(([value, label, hint]) => {
          const option = new Option(label, value);
          if (hint) option.title = hint;
          return option;
        }),
      );
      sortSelect.dataset.kind = optionsKey;
    }
    if (options) sortSelect.value = memberSortFor(g.kind);
  }
  const peopleSelect = $("memberPeople");
  if (peopleSelect) {
    const showPeople = Boolean(memberSortOptions(g.kind)?.some(([value]) => value === PEOPLE_SORT));
    peopleSelect.hidden = !showPeople;
    if (showPeople) {
      peopleSelect.value = PEOPLE_FILTER_MAX[state.memberPeople] != null ? state.memberPeople : "any";
    }
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
  const lightboxIndexByPath = new Map(
    state.lightboxItems.map((item, lightboxIndex) => [item.path, lightboxIndex]),
  );
  updateDetailMeta(g);
  updateGroupSelectionText(g);
  if ((triage || decisionReview) && !members.length) {
    const hiddenDeleted = !state.showDeleted && deletedPaths.size;
    const modifiedWindow = g.kind === "all_files" ? MODIFIED_LABELS[state.memberModified] : null;
    const peopleLabel = peopleMax != null ? PEOPLE_FILTER_LABELS[state.memberPeople] : null;
    const scoringNow = peopleJob?.groupId === g.id;
    box.innerHTML = `<div class="triage-empty">${
      sidebarFilters?.anyActive
        ? "No files here match the active search or filters."
        : hiddenDeleted
          ? `Every remaining file is in Trash. Use <strong>${deletedPaths.size} in Trash · Show</strong> to restore one.`
          : peopleLabel && scoringNow
            ? "Ranking by people likelihood…"
            : peopleLabel
              ? `No ${modifiedWindow ? `files modified in ${modifiedWindow}` : "files"} here ${peopleLabel}.`
              : modifiedWindow
                ? `No files in this folder were modified in ${modifiedWindow}.`
                : "Nothing left in this review pile."
    }</div>`;
    return;
  }

  const keeperMember = g.kind === "similar"
    ? (g.members || []).find((item) => item.path === g.suggested_keep) || g.members?.[0]
    : null;
  const offset = append ? box.querySelectorAll(".card").length : 0;
  const rendered = document.createElement("div");
  rendered.innerHTML = members.slice(offset)
    .map((m, i) => {
      i += offset;
      const opts = cardOptionsFor(g, m, {
        selected,
        reviewedPaths,
        deletedPaths,
        singleReview,
        decisionReview,
      });
      const memberIndex = singleReview ? state.memberFocus : i;
      const focused = singleReview || i === state.memberFocus ? "focused" : "";
      const lightboxIndex = lightboxIndexByPath.get(m.path) ?? -1;
      const fileName = basename(m.path);
      const similarity = m.similarity_percent == null ? null : Number(m.similarity_percent);
      // A rotated or mirrored copy is named so the turned thumbnail reads as intended.
      const turnedEvidence = m.orientation_label ? ` · ${m.orientation_label} copy` : "";
      const similarityEvidence = g.match_basis === "video_version"
        ? "Matching video section; review the time ranges and audio before removing a version"
        : m.match_label
          ? `${m.match_label} of suggested keeper · EmbeddingGemma meaning match, not a pixel match`
          : Number.isFinite(similarity)
            ? `${similarity.toFixed(1).replace(/\.0$/, "")}% Similar to suggested keeper${turnedEvidence} · fingerprint agreement, not a probability`
            : `Perceptual match to suggested keeper${turnedEvidence} · similarity score unavailable`;
      const evidence = g.kind === "exact"
        ? "Byte-identical SHA-256 match"
        : g.kind === "similar"
          ? similarityEvidence
          : g.kind === "low_resolution"
            ? `${opts.dims} · ${((m.width || 0) * (m.height || 0) / 1_000_000).toFixed(2)} megapixels · below the 1 MP review threshold`
            : g.kind === "random_review"
              ? "Randomly selected from this scan for a quick keep-or-delete check"
              : g.kind === "faces"
                ? `OpenCV face detection found ${m.face_count} face${m.face_count === 1 ? "" : "s"} (heuristic, not a guarantee)`
                : g.kind === "all_files"
                  ? "Every scanned media file in this folder appears here, category or not"
                  : g.kind === "sets"
                    ? (m.match_label
                      ? `${m.match_label} · EmbeddingGemma meaning match, not a duplicate check`
                      : "First image of this set · the others are measured against it")
                    : `OpenCV person detection analyzed ${m.human_frames_analyzed || 0} frame(s); no person detected — likely non-human`;
      let similarExtras = "";
      if (g.kind === "similar") {
        if (m.path === g.suggested_keep && g.cleanup?.keeper_reason) {
          similarExtras = `<div class="keeper-reason">Suggested keeper: ${escapeHtml(g.cleanup.keeper_reason)}</div>`;
        } else if (keeperMember && m.path !== keeperMember.path) {
          const pills = memberDiffPills(m, keeperMember);
          if (pills.length) {
            similarExtras = `<div class="diff-pills">${pills.map((p) => `<span class="diff-pill ${escapeHtml(p.kind || "")}"${p.title ? ` title="${escapeHtml(p.title)}"` : ""}>${escapeHtml(p.label)}</span>`).join("")}</div>`;
          }
        }
      }
      const preview = cardPreviewHtml(g, m, lightboxIndex, opts);
      const actions = cardActionsHtml(g, m, lightboxIndex, opts);
      return `
        <article class="card ${singleReview ? "focus-card" : ""} ${decisionReview ? "decision-card" : ""} ${triage ? "triage-card" : ""} ${opts.isKeep ? "keep" : ""} ${opts.isSel ? "selected" : ""} ${opts.deleted ? "deleted" : ""} ${focused}" data-path="${escapeHtml(m.path)}" data-index="${memberIndex}">
          ${preview}
          <div class="card-body">
            <div class="name" title="${escapeHtml(m.path)}">${escapeHtml(fileName)}</div>
            <div class="path" title="${escapeHtml(m.path)}">${escapeHtml(m.path)}</div>
            <div class="card-meta">
              <span>${formatBytes(m.size)}</span>
              <span>${opts.dims}</span>
              <span class="file-extra" title="Modified">${escapeHtml(formatMtime(m.mtime))}</span>
              <span class="media-kind">${escapeHtml(m.media_type)}</span>
              ${m.face_count != null ? `<span class="face-count ${m.face_count > 1 ? "multi" : ""}" title="Faces detected by OpenCV (heuristic)">${m.face_count === 0 ? "No faces" : `${m.face_count} face${m.face_count === 1 ? "" : "s"}`}</span>` : ""}
              ${m.people_score != null ? `<span class="face-count people-score" title="How likely this shows a person (YOLOX person detector). Advice for ordering only.">People ${m.people_score < 0.01 ? "<1" : Math.round(m.people_score * 100)}%</span>` : ""}
              ${(m.male_face_count || 0) > 0 ? `<span class="face-count multi" title="Male faces estimated by InsightFace genderage (heuristic)">${m.male_face_count} male${m.male_face_count === 1 ? "" : "s"}</span>` : ""}
            </div>
            <div class="evidence">${escapeHtml(evidence)}</div>
            ${similarExtras}
            ${m.non_person_coverage != null || memberSort === COVERAGE_SORT ? `<div class="non-person-coverage" title="${COVERAGE_HINT}">Non-person coverage: ${m.non_person_coverage == null ? "Unknown" : `${coveragePercent.format(m.non_person_coverage)}%`}</div>` : ""}
            <div class="card-actions">
              ${actions}
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
    if (image.complete && image.naturalWidth > 0) {
      setPreviewAspectRatio(image.closest(".thumb-wrap"), image.naturalWidth, image.naturalHeight);
    }
  });

  if (!append) box.replaceChildren(...rendered.childNodes);
  else box.append(...rendered.childNodes);

  // The single-file stage has a stable height; decisions never move the page.
  requestAnimationFrame(fitReviewStage);
}

// —— Serialized checkbox queue for Similar / duplicate list view ——
const pendingListSelection = new Map(); // group id → ordered selected paths
const listSelectionInFlight = new Map(); // group id → Promise

function flushListSelection(id) {
  if (listSelectionInFlight.has(id)) return listSelectionInFlight.get(id);
  const promise = (async () => {
    try {
      while (pendingListSelection.has(id)) {
        const selectedPaths = pendingListSelection.get(id);
        pendingListSelection.delete(id);
        const updated = await api("/api/selection", {
          method: "POST",
          body: JSON.stringify({
            group_id: id,
            selected: selectedPaths,
            scan_id: state.scanId,
          }),
        });
        if (!pendingListSelection.has(id)) {
          patchGroup(updated);
          if (currentGroup()?.id === id) {
            const box = $("members");
            box.querySelectorAll(":scope > .card[data-path]").forEach((card) => {
              syncCardSelection(card, updated, card.dataset.path);
            });
            updateGroupSelectionText(updated);
            updateDetailMeta(updated);
            renderedGroupSnapshot = groupRenderSignature(updated);
          }
          if (selectionFiltersActive() || !updateGroupListItem(updated)) {
            scheduleRender({ groupList: true });
          } else {
            applyResultControls();
          }
          scheduleRender({ selection: true });
        }
      }
    } catch (error) {
      pendingListSelection.delete(id);
      toast(error.message || "Could not update selection", "error");
      const open = currentGroup();
      if (open && open.id === id) selectGroup(id, { silent: true }).catch(() => {});
    } finally {
      listSelectionInFlight.delete(id);
    }
  })();
  listSelectionInFlight.set(id, promise);
  return promise;
}

// —— Event delegation on #members (standard member & triage cards) ——
(function wireMembersDelegation() {
  const box = $("members");
  if (!box || box.dataset.membersDelegated === "1") return;
  box.dataset.membersDelegated = "1";

  box.addEventListener("click", async (event) => {
    const card = event.target.closest("#members > .card:not(.copy-row):not(.crop-card)");
    if (!card) return;
    const g = currentGroup();
    if (!g) return;

    const candDelete = event.target.closest(".candidate-delete");
    if (candDelete) {
      reviewCandidate(g, candDelete.dataset.path, true);
      return;
    }
    const candKeep = event.target.closest(".candidate-keep");
    if (candKeep) {
      reviewCandidate(g, candKeep.dataset.path, false);
      return;
    }
    const revealBtn = event.target.closest(".reveal");
    if (revealBtn) {
      event.stopPropagation();
      try {
        await api(`/api/reveal?path=${encodeURIComponent(revealBtn.dataset.path)}&open=1`);
      } catch (err) {
        toast(err.message, "error");
      }
      return;
    }
    const delBtn = event.target.closest(".delete-candidate");
    if (delBtn) {
      event.stopPropagation();
      trashReviewCandidate(g, delBtn.dataset.path);
      return;
    }
    const undoBtn = event.target.closest(".undo-delete");
    if (undoBtn) {
      event.stopPropagation();
      undoReviewCandidate(g, undoBtn.dataset.path);
      return;
    }
    const thumbBtn = event.target.closest("button.thumb-wrap");
    if (thumbBtn) {
      state.memberFocus = Number(card.dataset.index || 0);
      if (thumbBtn.dataset.clickTrash) {
        trashReviewCandidate(g, thumbBtn.dataset.path);
        return;
      }
      const path = card.dataset.path;
      const idx = path ? state.lightboxItems.findIndex((item) => item.path === path) : -1;
      openLightbox(idx >= 0 ? idx : Number(thumbBtn.dataset.index));
      return;
    }
    const inspectBtn = event.target.closest(".inspect-media");
    if (inspectBtn) {
      event.stopPropagation();
      const cardIndex = Number(card.dataset.index);
      if (Number.isFinite(cardIndex)) state.memberFocus = cardIndex;
      const path = card.dataset.path;
      const idx = path ? state.lightboxItems.findIndex((item) => item.path === path) : -1;
      openLightbox(idx >= 0 ? idx : Number(inspectBtn.dataset.index));
      return;
    }
    if (!event.target.closest("input, button, label, a")) {
      state.memberFocus = Number(card.dataset.index);
      box.querySelectorAll(".card").forEach((c) => c.classList.remove("focused"));
      card.classList.add("focused");
    }
  });

  // Capturing listener so Space's non-bubbling synthetic change event from
  // keyboard.js also reaches the delegated handler.
  box.addEventListener("change", (event) => {
    const cb = event.target.closest?.("#members > .card:not(.copy-row):not(.crop-card) .sel-cb");
    if (!cb) return;
    if (state.scanning || state.actionBusy) {
      cb.checked = !cb.checked;
      toast("Selections are locked while a scan or file action runs", "error");
      return;
    }
    const g = currentGroup();
    if (!g) return;
    const changedPath = cb.dataset.path;
    const next = new Set(g.selected_for_removal || []);
    if (cb.checked) next.add(changedPath);
    else next.delete(changedPath);
    const ordered = (g.members || []).map((m) => m.path).filter((p) => next.has(p));
    const optimistic = { ...g, selected_for_removal: ordered };
    state.selectToken += 1;
    markGroupTouched(g.id);
    patchGroup(optimistic);
    const card = cb.closest(".card");
    if (card) syncCardSelection(card, optimistic, changedPath);
    updateGroupSelectionText(optimistic);
    if (selectionFiltersActive() || !updateGroupListItem(optimistic)) {
      scheduleRender({ groupList: true });
    } else {
      applyResultControls();
    }
    scheduleRender({ selection: true });
    pendingListSelection.set(g.id, ordered);
    flushListSelection(g.id);
  }, true);

  box.addEventListener("load", (event) => {
    const img = event.target;
    if (!img?.classList?.contains("thumb-image")) return;
    if (!img.closest("#members > .card:not(.copy-row):not(.crop-card)")) return;
    setPreviewAspectRatio(img.closest(".thumb-wrap"), img.naturalWidth, img.naturalHeight);
  }, true);

  box.addEventListener("error", (event) => {
    const img = event.target;
    if (!img?.classList?.contains("thumb-image")) return;
    if (!img.closest("#members > .card:not(.copy-row):not(.crop-card)")) return;
    const fallback = document.createElement("div");
    fallback.className = "thumb-fallback";
    fallback.textContent = "No preview";
    img.replaceWith(fallback);
  }, true);

  box.addEventListener("loadedmetadata", (event) => {
    const video = event.target;
    if (!video?.classList?.contains("hover-video")) return;
    if (!video.closest("#members > .card:not(.copy-row):not(.crop-card)")) return;
    setPreviewAspectRatio(video.closest(".thumb-wrap"), video.videoWidth, video.videoHeight);
  }, true);

  box.addEventListener("pointerover", (event) => {
    const wrap = event.target.closest?.("#members > .card:not(.copy-row):not(.crop-card) .thumb-wrap");
    if (!wrap || wrap.contains(event.relatedTarget)) return;
    const video = wrap.querySelector(".hover-video");
    if (video) {
      stopInlineVideo();
      activeVideo = video;
      video.muted = true;
      if (!video.src) video.src = video.dataset.src;
      video.play().catch(() => {
        /* The static poster remains when the browser cannot play this codec. */
      });
    }
    const gif = wrap.querySelector(".hover-gif");
    if (gif?.dataset.src) gif.src = gif.dataset.src;
  });

  box.addEventListener("pointerout", (event) => {
    const wrap = event.target.closest?.("#members > .card:not(.copy-row):not(.crop-card) .thumb-wrap");
    if (!wrap || wrap.contains(event.relatedTarget)) return;
    const video = wrap.querySelector(".hover-video");
    if (video && activeVideo === video) stopInlineVideo();
    const gif = wrap.querySelector(".hover-gif");
    if (gif?.dataset.thumbnail) gif.src = gif.dataset.thumbnail;
  });
})();

async function reviewCandidate(group, path, remove) {
  if (state.scanning) {
    toast("Selections unlock when the scan finishes", "error");
    return;
  }
  if (remove && group.members.some((m) => m.path === path && m.protected)) return toast("This file is in a protected library");
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
    patchGroup(updated);
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
    // Unlock lightbox decision buttons before refreshing unless another held-key
    // decision is already queued to run next in finally.
    state.reviewingCandidate = Boolean(state.pendingReviewDecision);
    if (lightboxOpen()) {
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
    const pending = state.pendingReviewDecision;
    state.pendingReviewDecision = null;
    if (pending) {
      state.reviewingCandidate = false;
      const current = currentGroup();
      const member = isDecisionReview(current)
        ? (current.members || [])[state.memberFocus]
        : null;
      if (member) await reviewCandidate(current, member.path, pending.direction);
      else if (lightboxOpen()) updateLightbox();
    } else if (state.reviewingCandidate) {
      state.reviewingCandidate = false;
      if (lightboxOpen()) updateLightbox();
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

function mergeInFlightDeletes(group, finishedPath) {
  const stillBusy = new Set(
    [...state.deleteBusy].filter((item) => item !== finishedPath),
  );
  if (!stillBusy.size) return group;
  const memberPaths = new Set((group.members || []).map((m) => m.path));
  const extraDeletes = [...stillBusy].filter((item) => memberPaths.has(item));
  if (!extraDeletes.length) return group;
  return {
    ...group,
    deleted_paths: [...new Set([...(group.deleted_paths || []), ...extraDeletes])],
    selected_for_removal: (group.selected_for_removal || []).filter((item) => !stillBusy.has(item)),
    reviewed_paths: [...new Set([...(group.reviewed_paths || []), ...extraDeletes])],
  };
}

function revertSingleOptimisticTrash(group, path, { wasSelected, wasReviewed }) {
  const deleted = (group.deleted_paths || []).filter((item) => item !== path);
  const selectedSet = new Set(group.selected_for_removal || []);
  if (wasSelected) selectedSet.add(path);
  else selectedSet.delete(path);
  const reviewedSet = new Set(group.reviewed_paths || []);
  if (!wasReviewed) reviewedSet.delete(path);
  return {
    ...group,
    deleted_paths: deleted,
    selected_for_removal: (group.members || []).map((m) => m.path).filter((p) => selectedSet.has(p)),
    reviewed_paths: (group.members || []).map((m) => m.path).filter((p) => reviewedSet.has(p)),
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

function refreshSidebarAndFooter(updated) {
  if (selectionFiltersActive() || !updateGroupListItem(updated)) {
    scheduleRender({ groupList: true });
  } else {
    applyResultControls();
  }
  scheduleRender({ selection: true });
}

async function trashReviewCandidate(group, path, { fromLightbox = false } = {}) {
  const liveGroup = state.allGroups.find((item) => item.id === group?.id) || group;
  if (!liveGroup || !path || (liveGroup.deleted_paths || []).includes(path) || state.deleteBusy.has(path)) {
    return;
  }
  if (state.scanning) {
    toast("Selections unlock when the scan finishes", "error");
    return;
  }
  const wasSelected = (liveGroup.selected_for_removal || []).includes(path);
  const wasReviewed = (liveGroup.reviewed_paths || []).includes(path);

  state.deleteBusy.add(path);
  state.trashedInPlace.add(path);
  state.selectToken += 1;
  const optimistic = optimisticTrashGroup(liveGroup, path);
  patchGroup(optimistic);

  const box = $("members");
  const card = box.querySelector(`:scope > .card[data-path="${CSS.escape(path)}"]`);
  const inPlace = !boardActive() && renderedGroupId === optimistic.id
    && card && patchTriageCardDeleted(card, optimistic, path, true);
  if (inPlace) {
    updateDetailMeta(optimistic);
    updateGroupSelectionText(optimistic);
    syncDeletedToggle(optimistic);
    renderedGroupSnapshot = groupRenderSignature(optimistic);
  } else if (currentGroup()?.id === optimistic.id) {
    renderMembers(optimistic);
  }
  refreshSidebarAndFooter(optimistic);

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
      group_id: liveGroup.id,
      path,
      scan_id: state.scanId,
      dry_run: false,
    });
    const merged = mergeInFlightDeletes(updated, path);
    patchGroup(merged);
    if (currentGroup()?.id === merged.id) {
      if (inPlace && box.querySelector(`:scope > .card[data-path="${CSS.escape(path)}"]`)) {
        updateDetailMeta(merged);
        updateGroupSelectionText(merged);
        syncDeletedToggle(merged);
        syncLightboxItemsAndIndices(merged);
        renderedGroupSnapshot = groupRenderSignature(merged);
      } else {
        renderMembers(merged);
      }
    }
    refreshSidebarAndFooter(merged);
    toast(`Moved ${basename(path)} to Trash`, "ok", {
      actionLabel: "Undo",
      onAction: () => undoReviewCandidate(merged, path, { fromLightbox }),
    });
  } catch (error) {
    state.trashedInPlace.delete(path);
    const latest = state.allGroups.find((item) => item.id === liveGroup.id) || liveGroup;
    const reverted = revertSingleOptimisticTrash(latest, path, { wasSelected, wasReviewed });
    patchGroup(reverted);
    if (currentGroup()?.id === reverted.id) {
      const currentCard = box.querySelector(`:scope > .card[data-path="${CSS.escape(path)}"]`);
      if (currentCard && patchTriageCardDeleted(currentCard, reverted, path, false)) {
        updateDetailMeta(reverted);
        updateGroupSelectionText(reverted);
        syncDeletedToggle(reverted);
        renderedGroupSnapshot = groupRenderSignature(reverted);
      } else {
        renderMembers(reverted);
      }
    }
    refreshSidebarAndFooter(reverted);
    if (fromLightbox && lightboxOpen()) {
      const failedMember = (reverted.members || []).find((member) => member.path === path);
      state.lightboxItems = [
        failedMember
          ? lightboxItemFor(failedMember, reverted)
          : { path, mediaType: undefined, keeper: reverted.suggested_keep, kind: reverted.kind },
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
  if (state.scanning) {
    toast("Undo unlocks when the scan finishes", "error");
    return;
  }
  while (state.deleteBusy.has(path)) await sleep(40);
  const live = currentGroup()?.id === group.id ? currentGroup() : group;
  try {
    const updated = await requestWithLockRetry("/api/review-candidate/undo", {
      group_id: live.id,
      path,
      scan_id: state.scanId,
    });
    const merged = mergeInFlightDeletes(updated, null);
    patchGroup(merged);
    state.trashedInPlace.delete(path);
    const box = $("members");
    const card = box.querySelector(`:scope > .card[data-path="${CSS.escape(path)}"]`);
    if (currentGroup()?.id === merged.id) {
      if (!boardActive() && renderedGroupId === merged.id && card && patchTriageCardDeleted(card, merged, path, false)) {
        updateDetailMeta(merged);
        updateGroupSelectionText(merged);
        syncDeletedToggle(merged);
        renderedGroupSnapshot = groupRenderSignature(merged);
      } else {
        renderMembers(merged);
      }
    }
    refreshSidebarAndFooter(merged);
    // Lightbox list now includes the restored file at its sorted position;
    // when the overlay is open, jump back to it.
    if (fromLightbox && lightboxOpen()) {
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
  if (state.scanning || state.actionBusy) {
    toast("Selections are locked while a scan or file action runs", "error");
    return null;
  }
  if (listSelectionInFlight.has(group.id)) {
    await Promise.allSettled([listSelectionInFlight.get(group.id)]);
  }
  const latest = state.allGroups.find((item) => item.id === group.id) || group;
  const selected = new Set(latest.selected_for_removal || []);
  if (wantSelected) selected.add(path);
  else selected.delete(path);
  const updated = await api("/api/selection", {
    method: "POST",
    body: JSON.stringify({
      group_id: latest.id,
      selected: [...selected],
      scan_id: state.scanId,
    }),
  });
  markGroupTouched(latest.id);
  patchGroup(updated);
  const box = $("members");
  box.querySelectorAll(".card[data-path]").forEach((card) => {
    syncCardSelection(card, updated, card.dataset.path);
  });
  if (updated.kind === "exact") {
    syncExactSelectionState(updated);
  } else {
    updateGroupSelectionText(updated);
    updateDetailMeta(updated);
  }
  if (currentGroup()?.id === updated.id) {
    renderedGroupSnapshot = groupRenderSignature(updated);
  }
  refreshBoardRow(updated);
  refreshSidebarAndFooter(updated);
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

// The Similar tab defaults to the board (board.js); a single Similar group
// opens in the swipe deck, and the header toggle flips it to the classic card
// list (which keeps checkboxes, bulk selection, and the whole-group "Mark as
// distinct" button). "Board" in the header returns to the board.
const SIMILAR_VIEW_KEY = "dedupe.similarView";
try {
  const savedView = localStorage.getItem(SIMILAR_VIEW_KEY);
  if (["board", "grid", "swipe"].includes(savedView)) state.similarView = savedView;
} catch {
  /* private mode */
}

function updateSimilarViewToggle(g) {
  const btn = $("btnSimilarView");
  if (!btn) return;
  const show = g?.kind === "similar";
  btn.hidden = !show;
  $("btnSimilarBoard").hidden = !show;
  if (!show) return;
  const swipe = state.similarView !== "grid";
  btn.setAttribute("aria-pressed", swipe ? "false" : "true");
  btn.textContent = swipe ? "☰ List view" : "⇄ Swipe review";
  btn.title = swipe
    ? "Switch to classic card list view"
    : "List view active — click to switch to Swipe review";
}

$("btnSimilarView")?.addEventListener("click", () => {
  state.similarView = state.similarView === "grid" ? "swipe" : "grid";
  try {
    localStorage.setItem(SIMILAR_VIEW_KEY, state.similarView);
  } catch {
    /* private mode */
  }
  const g = currentGroup();
  if (g) selectGroup(g.id, { silent: true }).catch((e) => toast(e.message, "error"));
});

$("btnMarkGroupReviewed")?.addEventListener("click", async () => {
  const g = currentGroup();
  if (!g || state.scanning) return;
  const nextReviewed = !g.reviewed;
  const btn = $("btnMarkGroupReviewed");
  btn.disabled = true;
  try {
    const updated = await api("/api/group/reviewed", {
      method: "POST",
      body: JSON.stringify({
        group_id: g.id,
        reviewed: nextReviewed,
        scan_id: state.scanId,
      }),
    });
    patchGroup(updated);
    syncMarkReviewedButton(updated);
    refreshSidebarAndFooter(updated);
    toast(nextReviewed ? "Group marked reviewed" : "Group unmarked as reviewed", "ok");
  } catch (error) {
    toast(error.message || "Could not update reviewed status", "error");
  } finally {
    syncMarkReviewedButton(currentGroup());
  }
});

window.addEventListener("dedupe:work-state", () => {
  const open = currentGroup();
  syncScanningLock(open);
  const lock = Boolean(state.scanning);
  $("members").querySelectorAll(".sel-cb, .candidate-decision, .delete-candidate, .undo-delete, .copy-keep").forEach((el) => {
    if (el.classList.contains("sel-cb")) {
      const row = el.closest(".card");
      const member = open?.members?.find((m) => m.path === row?.dataset.path);
      el.disabled = Boolean(lock || member?.protected || row?.classList.contains("deleted"));
    } else {
      el.disabled = lock;
    }
  });
});

document.querySelectorAll(".member-prev").forEach((btn) => {
  btn.addEventListener("click", () => changeMemberPage(-1));
});
document.querySelectorAll(".member-next").forEach((btn) => {
  btn.addEventListener("click", () => changeMemberPage(1));
});

$("memberSort")?.addEventListener("change", (event) => {
  const current = currentGroup();
  if (current && memberSortOptions(current.kind)) {
    state.memberSortByKind[current.kind] = event.target.value;
  }
  restartMemberList();
  if (current) renderMembers(current);
});

$("memberPeople")?.addEventListener("change", (event) => {
  state.memberPeople = event.target.value;
  restartMemberList();
  const current = currentGroup();
  if (current) renderMembers(current);
});

// The Files tab's modified-time filter: re-render the member cards
// immediately, starting back on the first page.
$("memberModified")?.addEventListener("change", (event) => {
  state.memberModified = event.target.value;
  restartMemberList();
  const current = currentGroup();
  if (current) renderMembers(current);
});

const REVIEW_VIEW_KEY = "dedupe.reviewView";
const PREVIEW_SIZE_KEY = "dedupe.previewSize";
const MEDIA_DETAILS_KEY = "dedupe.showFileDetails";
try {
  if (localStorage.getItem(REVIEW_VIEW_KEY) === "focus") state.reviewView = "focus";
  const size = Number(localStorage.getItem(PREVIEW_SIZE_KEY));
  if (size >= 240 && size <= 520 && (size - 240) % 40 === 0) {
    $("previewSize").value = String(size);
    $("members").style.setProperty("--tile-size", `${size}px`);
  }
  if (localStorage.getItem(MEDIA_DETAILS_KEY) === "1") {
    $("detailBody").classList.add("show-file-details");
    $("btnMediaDetails").setAttribute("aria-pressed", "true");
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
  try { localStorage.setItem(MEDIA_DETAILS_KEY, show ? "1" : "0"); } catch { /* ignore */ }
});

export {
  selectGroup,
  renderMembers,
  restartMemberList,
  reviewCandidate,
  trashReviewCandidate,
  undoReviewCandidate,
  changeMemberPage,
  setMemberSelected,
  getRenderedMemberFilterSignature,
};
