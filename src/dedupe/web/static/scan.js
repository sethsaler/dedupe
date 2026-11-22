// Scan form submission, tabs, filters, and the session banner actions.

import { api } from "./api.js";
import { updateSelectionSummary } from "./actions.js";
import { renderBoard } from "./board.js";
import {
  loadGroups,
  renderGroupList,
  renderTabCounts,
  rememberFocusedGroup,
  resetGroupListWindow,
} from "./groups.js";
import { selectGroup } from "./members.js";
import { confirmModal } from "./modal.js";
import { groupNeedsAttention } from "./model.js";
import {
  applySettingsToForm,
  collapseScanPanel,
  expandScanPanel,
  lowResolutionMaxPixels,
  parsePathsInput,
  presetResultsTab,
  saveRecent,
  scanGoalShortLabel,
  syncCollapseButtonVisibility,
} from "./settings.js";
import { state } from "./state.js";
import { refreshStatus, renderSession, startStatusPolling } from "./status.js";
import { $, toast } from "./util.js";

// —— Scan ——
$("btnScan").addEventListener("click", startScan);
$("btnRetryAnalysis").addEventListener("click", () => {
  if (state.lastScanSettings) startScan({ ...state.lastScanSettings, new_only: false });
});
async function cancelScan() {
  const btn1 = $("btnCancelScan");
  const btn2 = $("btnCancelScanProgress");
  const prev1 = btn1.textContent;
  const prev2 = btn2.textContent;
  btn1.disabled = true;
  btn2.disabled = true;
  btn1.textContent = "Cancelling…";
  btn2.textContent = "Cancelling…";
  try {
    await api("/api/scan/cancel", {
      method: "POST",
      body: JSON.stringify({ scan_id: state.scanId }),
    });
    toast("Cancelling scan after the current work item…");
  } catch (error) {
    btn1.disabled = false;
    btn2.disabled = false;
    btn1.textContent = prev1;
    btn2.textContent = prev2;
    toast(error.message, "error");
  }
}
$("btnCancelScan").addEventListener("click", cancelScan);
$("btnCancelScanProgress").addEventListener("click", cancelScan);
$("paths").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  startScan();
});

export function collectScanSettings() {
  const workersRaw = Number($("workers").value);
  const lowResolutionBounds = {
    images: lowResolutionMaxPixels("lowResolutionImageMaxMp"),
    gifs: lowResolutionMaxPixels("lowResolutionGifMaxMp"),
    videos: lowResolutionMaxPixels("lowResolutionVideoMaxMp"),
  };
  return {
        paths: parsePathsInput($("paths").value),
        preferred_folders: $("preferredFolders").value.split("\n").map((s) => s.trim()).filter(Boolean),
        protected_folders: $("protectedFolders").value.split("\n").map((s) => s.trim()).filter(Boolean),
        video_versions: $("optVideoVersions").checked,
        exact: $("optExact").checked,
        similar: $("optSimilar").checked,
        semantic: $("optSemantic").checked,
        semantic_full_speed: $("optSemantic").checked && $("optSemanticFast").checked,
        decider: $("optSemantic").checked && $("optDecider").checked,
        semantic_threshold: Number($("semanticSetThreshold").value) / 100,
        semantic_duplicate_threshold: Number($("semanticDuplicateThreshold").value) / 100,
        find_no_humans: $("optNoHumans").checked,
        count_faces: $("optCountFaces").checked,
        find_black_borders: $("optBlackBorders").checked,
        crop_aggressiveness: $("optCropAggressiveness") ? $("optCropAggressiveness").value : "balanced",
        find_low_resolution: $("optLowResolution").checked,
        low_resolution_images: $("optLowResolutionImages").checked,
        low_resolution_gifs: $("optLowResolutionGifs").checked,
        low_resolution_videos: $("optLowResolutionVideos").checked,
        low_resolution_image_max_pixels: lowResolutionBounds.images,
        low_resolution_gif_max_pixels: lowResolutionBounds.gifs,
        low_resolution_video_max_pixels: lowResolutionBounds.videos,
        random_review_count: $("optRandomReview").checked ? 50 : 0,
        human_backend: $("humanBackend") ? $("humanBackend").value : "opencv",
        include_images: $("optImages").checked,
        include_gifs: $("optGifs").checked,
        include_videos: $("optVideos").checked,
        threshold: Number($("threshold").value),
        parallel_streams: $("optParallel").checked,
        // 0 / Auto → null so backend uses resolve_workers auto
        workers: workersRaw > 0 ? workersRaw : null,
        exclusions: $("exclusions").value.split(",").map((value) => value.trim()).filter(Boolean),
  };
}

function kindSupportedBySettings(kind, settings) {
  switch (kind) {
    case "all":
    case "all_files":
      return true;
    case "exact":
      return Boolean(settings.exact);
    case "similar":
      return Boolean(settings.similar || settings.video_versions || settings.semantic);
    case "sets":
      return Boolean(settings.semantic);
    case "crop":
      return Boolean(settings.find_black_borders);
    case "faces":
      return Boolean(settings.count_faces);
    case "no_humans":
      return Boolean(settings.find_no_humans);
    case "low_resolution":
      return Boolean(settings.find_low_resolution);
    case "random_review":
      return Number(settings.random_review_count || 0) > 0;
    default:
      return true;
  }
}

function targetTabForScan(settings, explicitOverrides = false) {
  const presetTab = !explicitOverrides ? presetResultsTab() : null;
  if (presetTab) return presetTab;
  if (kindSupportedBySettings(state.kind, settings)) return state.kind;
  const duplicatesEnabled = Boolean(
    settings.exact || settings.similar || settings.semantic || settings.video_versions,
  );
  if (!duplicatesEnabled) {
    const enabledReviews = [
      ["crop", Boolean(settings.find_black_borders)],
      ["faces", Boolean(settings.count_faces)],
      ["no_humans", Boolean(settings.find_no_humans)],
      ["low_resolution", Boolean(settings.find_low_resolution)],
      ["random_review", Number(settings.random_review_count || 0) > 0],
    ].filter(([, active]) => active);
    if (enabledReviews.length === 1) return enabledReviews[0][0];
  }
  return "all";
}

export async function startScan(overrides = {}) {
  if (overrides instanceof Event) overrides = {};
  const hasOverrides = Object.keys(overrides).length > 0;
  const settings = { ...collectScanSettings(), job_id: state.selectedJobId || null, ...overrides };
  const paths = Array.isArray(settings.paths)
    ? settings.paths.map((s) => String(s).trim()).filter(Boolean)
    : parsePathsInput(settings.paths);
  if (!paths.length) {
    toast("Enter at least one folder path");
    $("paths").focus();
    return;
  }
  settings.paths = paths;
  const lowResolutionBounds = {
    images: lowResolutionMaxPixels("lowResolutionImageMaxMp"),
    gifs: lowResolutionMaxPixels("lowResolutionGifMaxMp"),
    videos: lowResolutionMaxPixels("lowResolutionVideoMaxMp"),
  };
  const invalidLowResolutionBound = $("optLowResolution").checked && [
    ["optLowResolutionImages", "images", "Images"],
    ["optLowResolutionGifs", "gifs", "GIFs"],
    ["optLowResolutionVideos", "videos", "Videos"],
  ].find(([toggle, key]) => $(toggle).checked && lowResolutionBounds[key] == null);
  if (invalidLowResolutionBound) {
    toast(`Set a positive megapixel bound for ${invalidLowResolutionBound[2]}`);
    return;
  }
  const scanBtn = $("btnScan");
  scanBtn.disabled = true;
  const tabBeforeScan = state.kind;
  const previous = {
    groups: state.groups,
    allGroups: state.allGroups,
    currentId: state.currentId,
    groupsVersion: state.groupsVersion,
    loadedGroupsVersion: state.loadedGroupsVersion,
    lastScanSettings: state.lastScanSettings,
    emptyHidden: $("emptyState").hidden,
    resultsHidden: $("results").hidden,
    actionBarHidden: $("actionBar").hidden,
    detailBodyHidden: $("detailBody").hidden,
    detailEmptyHidden: $("detailEmpty").hidden,
    progressHidden: $("progressWrap").hidden,
    progressMsg: $("progressMsg").textContent,
    scanStatus: $("scanStatus").textContent,
    scanStatusError: $("scanStatus").classList.contains("error"),
  };
  state.groups = [];
  state.allGroups = [];
  state.currentId = null;
  state.groupsVersion = -1;
  state.loadedGroupsVersion = -1;
  state.touchedGroups.clear();
  state.swipeAnchors.clear();
  state.swipeDeckOrder.clear();
  state.swipeUndo.clear();
  state.trashedInPlace.clear();
  state.showDeleted = false;
  state.boardLimit = 30;
  state.boardFocus = { id: null, index: 0 };
  state.memberPage = 0;
  state.memberFocus = 0;
  rememberFocusedGroup(null);
  resetGroupListWindow();
  $("btnCancelScan").disabled = false;
  $("btnCancelScan").hidden = false;
  $("btnCancelScan").textContent = "Cancel";
  $("btnCancelScanProgress").disabled = false;
  $("btnCancelScanProgress").hidden = false;
  $("btnCancelScanProgress").textContent = "Cancel scan";
  $("progressWrap").hidden = false;
  $("progressBar").removeAttribute("value");
  $("progressMsg").textContent = settings.new_only
    ? "Checking new arrivals against baseline…"
    : "Starting…";
  $("scanStatus").textContent = "Scanning…";
  $("scanStatus").classList.remove("error");
  $("scanQuality").hidden = true;
  $("scanQuality").open = false;
  delete $("scanQuality").dataset.autoOpened;
  $("exclusionsCheckResult").hidden = true;
  $("streamProgress").hidden = true;
  $("streamProgress").innerHTML = "";
  $("btnCollapseScan").hidden = true;
  $("emptyState").hidden = true;
  $("results").hidden = false;
  $("actionBar").hidden = true;
  $("detailBody").hidden = true;
  $("detailEmpty").hidden = false;
  $("groupList").innerHTML =
    `<div class="group-empty">Scanning — matches will appear here as they are found…</div>`;
  $("groupMore").innerHTML = "";
  $("countAll").textContent = "0";
  $("countExact").textContent = "0";
  $("countSimilar").textContent = "0";
  $("countSets").textContent = "0";
  $("countLowResolution").textContent = "0";
  $("countRandomReview").textContent = "0";
  $("countNoHumans").textContent = "0";
  $("countFaces").textContent = "0";
  $("countCrop").textContent = "0";
  $("countAllFiles").textContent = "0";
  state.pendingScanPriorId = state.scanId;
  state.pendingScanStart = true;
  try {
    const started = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({
        ...settings,
      }),
    });
    paths.forEach(saveRecent);
    state.lastScanSettings = settings;
    const startedScanId = started.scan_id || null;
    const alreadyCompleted = Boolean(startedScanId && state.completedScanId === startedScanId);
    state.scanId = startedScanId || state.scanId;
    if (!alreadyCompleted) {
      state.scanning = true;
    }
    // Fold the setup form into its slim bar so the streaming results get
    // the screen; the bar shows the active goal + scanned paths and re-opens on click.
    collapseScanPanel(paths.join(", "), scanGoalShortLabel(hasOverrides ? settings : null));
    syncCollapseButtonVisibility();
    // Route to the goal's results tab (or recover from an inactive category)
    // rather than leaving the user stranded on the previous scan's tab.
    if (!alreadyCompleted || state.kind === tabBeforeScan) {
      const nextKind = targetTabForScan(settings, hasOverrides);
      const goalTab = tabButtons.find((tab) => tab.dataset.kind === nextKind);
      if (goalTab && !goalTab.classList.contains("active")) {
        activateTab(goalTab);
      } else {
        renderBoard();
      }
    }
    // SSE status/group events drive the UI from here; polling is the
    // fallback and stands down as soon as the stream delivers.
    if (!alreadyCompleted) {
      startStatusPolling();
    }
  } catch (e) {
    if (!state.scanning) {
      state.groups = previous.groups;
      state.allGroups = previous.allGroups;
      state.currentId = previous.currentId;
      state.groupsVersion = previous.groupsVersion;
      state.loadedGroupsVersion = previous.loadedGroupsVersion;
      state.lastScanSettings = previous.lastScanSettings;
      $("emptyState").hidden = previous.emptyHidden;
      $("results").hidden = previous.resultsHidden;
      $("actionBar").hidden = previous.actionBarHidden;
      $("detailBody").hidden = previous.detailBodyHidden;
      $("detailEmpty").hidden = previous.detailEmptyHidden;
      $("progressWrap").hidden = previous.progressHidden;
      $("progressMsg").textContent = previous.progressMsg;
      $("btnCancelScan").hidden = true;
      $("btnCancelScan").disabled = false;
      $("btnCancelScan").textContent = "Cancel";
      $("btnCancelScanProgress").hidden = true;
      $("btnCancelScanProgress").disabled = false;
      $("btnCancelScanProgress").textContent = "Cancel scan";
      scanBtn.disabled = false;
      scanBtn.querySelector(".btn-label").textContent = "Scan";
      renderTabCounts();
      renderGroupList();
      updateSelectionSummary();
    }
    $("scanStatus").textContent = "Error";
    $("scanStatus").classList.add("error");
    syncCollapseButtonVisibility();
    toast(e.message, "error");
  } finally {
    state.pendingScanStart = false;
  }
}

const tabButtons = [...document.querySelectorAll(".tab")];

export function switchTab(kind) {
  const tab = tabButtons.find((item) => item.dataset.kind === kind);
  if (tab) return activateTab(tab);
  return Promise.resolve();
}

async function activateTab(tab) {
  tabButtons.forEach((t) => {
    const active = t === tab;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", active ? "true" : "false");
    // Roving tabindex: only the active tab is in the Tab order; arrows move.
    t.tabIndex = active ? 0 : -1;
  });
  state.kind = tab.dataset.kind;
  resetGroupListWindow();
  // Switch layouts now (the Similar board or the sidebar) rather than after
  // any refetch.
  renderGroupList();
  updateSelectionSummary();
  try {
    tab.scrollIntoView({ block: "nearest", inline: "nearest", container: "nearest" });
  } catch {
    tab.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  if (state.allGroups.length > 0 && state.groupsVersion >= 0) {
    if (state.currentId && !state.groups.some((g) => g.id === state.currentId)) {
      $("members").querySelectorAll("video").forEach((video) => video.pause());
      state.currentId = null;
      $("detailBody").hidden = true;
      $("detailEmpty").hidden = false;
    }
    if (!state.currentId && state.groups.length && !$("results").hidden && !$("detailEmpty").hidden) {
      try {
        await selectGroup(state.groups[0].id, { silent: true });
      } catch (e) {
        toast(e.message || String(e), "error");
      }
    }
    return;
  }
  try {
    await loadGroups();
  } catch (e) {
    toast(e.message || String(e), "error");
  }
}

tabButtons.forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab));
});

// WAI tablist: ←/→/Home/End move between tabs and activate on move.
document.querySelector(".filter-tabs").addEventListener("keydown", (e) => {
  const current = tabButtons.indexOf(document.activeElement);
  if (current < 0) return;
  let next = null;
  if (e.key === "ArrowRight") next = (current + 1) % tabButtons.length;
  else if (e.key === "ArrowLeft") next = (current - 1 + tabButtons.length) % tabButtons.length;
  else if (e.key === "Home") next = 0;
  else if (e.key === "End") next = tabButtons.length - 1;
  if (next === null) return;
  e.preventDefault();
  e.stopPropagation(); // keep the global arrow handlers (cards/groups) out of this
  tabButtons[next].focus();
  activateTab(tabButtons[next]);
});

const LIVE_FILTER_IDS = [
  "resultSearch",
  "filterMinMb",
  "filterMaxMb",
  "filterMinWidth",
  "filterMinHeight",
  "filterPathPattern",
];
// Text inputs re-filter on a short debounce (a keystroke re-sorts the whole
// group array); selects and checkboxes apply immediately.
let filterDebounce = null;
function applyFiltersSoon() {
  clearTimeout(filterDebounce);
  filterDebounce = setTimeout(() => {
    resetGroupListWindow();
    renderGroupList();
  }, 150);
}
[...LIVE_FILTER_IDS, "resultSort", "selectionFilter", "filterFaces", "issuesOnly", "hideCompleted"].forEach((id) => {
  const isText = LIVE_FILTER_IDS.includes(id);
  $(id).addEventListener(isText ? "input" : "change", () => {
    if (isText) applyFiltersSoon();
    else {
      resetGroupListWindow();
      renderGroupList();
    }
  });
});
$("btnClearFilters").addEventListener("click", () => {
  clearTimeout(filterDebounce);
  for (const id of LIVE_FILTER_IDS) $(id).value = "";
  $("selectionFilter").value = "all";
  $("filterFaces").value = "any";
  $("issuesOnly").checked = false;
  $("hideCompleted").checked = false;
  resetGroupListWindow();
  renderGroupList();
});
$("groupList").addEventListener("click", (event) => {
  const action = event.target.closest("[data-empty-action]")?.dataset.emptyAction;
  if (action === "clear") {
    $("btnClearFilters").click();
    $("resultSearch").focus();
  } else if (action === "all") {
    const tab = tabButtons.find((tab) => tab.dataset.kind === "all");
    tab.focus();
    activateTab(tab);
  } else if (action === "setup") {
    expandScanPanel({ focusId: "scanPreset" });
  }
});
$("btnNextReview").addEventListener("click", () => {
  const candidates = state.groups.filter(groupNeedsAttention);
  if (!candidates.length) return toast("No unreviewed results need attention", "ok");
  const current = candidates.findIndex((group) => group.id === state.currentId);
  const target = candidates[(current + 1) % candidates.length];
  // A single-group view (one Files folder, Faces, Non-Human) is its own only
  // candidate: re-selecting it would refetch the same group and reset the
  // member page to the first cards, so stay put and say so instead.
  if (target.id === state.currentId) {
    return toast("Already showing the only group that needs attention", "ok");
  }
  selectGroup(target.id)
    .catch((e) => toast(e.message || String(e), "error"));
});

// Rescanning the saved review's folders is the only way to bring pruned
// files back; the banner offers it directly.
$("btnRescanSession").addEventListener("click", () => {
  const roots = state.reviewSession?.roots || [];
  if (!roots.length) return toast("No saved folders to rescan", "error");
  const saved = state.reviewSession.scan_settings || {};
  const hydrated = {
    ...saved,
    paths: saved.paths?.length ? saved.paths : roots,
    protected_folders: state.reviewSession.protected_folders || saved.protected_folders || [],
  };
  applySettingsToForm(hydrated);
  $("scanPanel").classList.remove("collapsed");
  $("scanCollapse").setAttribute("aria-expanded", "true");
  startScan({
    ...hydrated,
    job_id: state.reviewSession.job_id || null,
    new_only: false,
  });
});

$("btnResumeSession").addEventListener("click", async () => {
  const button = $("btnResumeSession");
  const originalLabel = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Resuming…";
  try {
    const resumed = await api("/api/review-session/resume", { method: "POST", body: "{}" });
    state.scanId = resumed.scan_id || state.scanId;
    const status = await refreshStatus();
    const saved = status?.scan_settings || state.lastScanSettings || {};
    const hydrated = {
      ...saved,
      paths: saved.paths?.length ? saved.paths : (state.reviewSession?.roots || []),
      protected_folders: saved.protected_folders || state.reviewSession?.protected_folders || [],
    };
    if (hydrated.paths?.length || Object.keys(saved).length) {
      applySettingsToForm(hydrated);
    }
    const roots = (hydrated.paths || []).join(", ");
    collapseScanPanel(roots, scanGoalShortLabel(state.lastScanSettings || hydrated));
    toast("Saved review resumed", "ok");
  } catch (error) {
    toast(error.message || String(error), "error");
    refreshStatus().catch(() => {});
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = originalLabel;
  }
});

$("btnDiscardSession").addEventListener("click", async () => {
  const ok = await confirmModal({
    title: "Discard saved review?",
    body: "<p>This removes the durable review state and its selections. Your media files are not changed.</p>",
    confirmLabel: "Discard saved review",
  });
  if (!ok) return;
  try {
    await api("/api/review-session", { method: "DELETE" });
    state.groups = []; state.allGroups = []; state.currentId = null;
    state.touchedGroups.clear();
    rememberFocusedGroup(null);
    $("progressWrap").hidden = true;
    $("scanQuality").hidden = true;
    $("scanCollapsePaths").textContent = "";
    $("scanCollapseGoal").hidden = true;
    renderSession(null); renderGroupList(); await refreshStatus();
    expandScanPanel();
    toast("Saved review discarded", "ok");
  } catch (error) { toast(error.message, "error"); }
});
