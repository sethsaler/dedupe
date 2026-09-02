// Scan form submission, tabs, filters, and the session banner actions.

import { api } from "./api.js";
import { updateSelectionSummary } from "./actions.js";
import { loadGroups, renderGroupList, resetGroupListWindow } from "./groups.js";
import { selectGroup } from "./members.js";
import { confirmModal } from "./modal.js";
import { groupNeedsAttention } from "./model.js";
import { lowResolutionMaxPixels, saveRecent } from "./settings.js";
import { state } from "./state.js";
import { refreshStatus, renderSession, startStatusPolling } from "./status.js";
import { $, toast } from "./util.js";

// —— Scan ——
$("btnScan").addEventListener("click", startScan);
$("btnCancelScan").addEventListener("click", async () => {
  try {
    await api("/api/scan/cancel", {
      method: "POST",
      body: JSON.stringify({ scan_id: state.scanId }),
    });
    $("btnCancelScan").disabled = true;
    toast("Cancelling scan after the current work item…");
  } catch (error) {
    toast(error.message, "error");
  }
});
$("paths").addEventListener("keydown", (e) => {
  if (e.key === "Enter") startScan();
});

async function startScan() {
  const raw = $("paths").value.trim();
  if (!raw) {
    toast("Enter at least one folder path");
    $("paths").focus();
    return;
  }
  const paths = raw.split(",").map((s) => s.trim()).filter(Boolean);
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
  paths.forEach(saveRecent);
  try {
    $("progressWrap").hidden = false;
    $("progressFill").style.width = "5%";
    $("progressMsg").textContent = "Starting…";
    $("emptyState").hidden = true;
    $("results").hidden = false;
    $("actionBar").hidden = true;
    $("detailBody").hidden = true;
    $("detailEmpty").hidden = false;
      $("groupList").innerHTML =
        `<div class="group-empty" role="presentation">Scanning — matches will appear here as they are found…</div>`;
      $("groupMore").innerHTML = "";
    $("countAll").textContent = "0";
    $("countExact").textContent = "0";
    $("countSimilar").textContent = "0";
    $("countLowResolution").textContent = "0";
    $("countRandomReview").textContent = "0";
    $("countNoHumans").textContent = "0";
    $("countFaces").textContent = "0";
    state.groups = [];
    state.allGroups = [];
    state.currentId = null;
    state.groupsVersion = -1;
    resetGroupListWindow();
    const workersRaw = Number($("workers").value);
    const started = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({
        paths,
        exact: $("optExact").checked,
        similar: $("optSimilar").checked,
        find_no_humans: $("optNoHumans").checked,
        count_faces: $("optCountFaces").checked,
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
      }),
    });
    state.scanId = started.scan_id || state.scanId;
    state.scanning = true;
    // SSE status/group events drive the UI from here; polling is the
    // fallback and stands down as soon as the stream delivers.
    startStatusPolling();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("btnCancelScan").disabled = false;
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", async () => {
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.remove("active");
      t.setAttribute("aria-selected", "false");
    });
    tab.classList.add("active");
    tab.setAttribute("aria-selected", "true");
    state.kind = tab.dataset.kind;
    updateSelectionSummary();
    resetGroupListWindow();
    try {
      await loadGroups();
    } catch (e) {
      toast(e.message || String(e), "error");
    }
  });
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
  for (const id of LIVE_FILTER_IDS) $(id).value = "";
  $("selectionFilter").value = "all";
  $("filterFaces").value = "any";
  $("issuesOnly").checked = false;
  $("hideCompleted").checked = false;
  resetGroupListWindow();
  renderGroupList();
});
$("btnNextReview").addEventListener("click", () => {
  const candidates = state.groups.filter(groupNeedsAttention);
  if (!candidates.length) return toast("No unreviewed results need attention", "ok");
  const current = candidates.findIndex((group) => group.id === state.currentId);
  selectGroup(candidates[(current + 1) % candidates.length].id)
    .catch((e) => toast(e.message || String(e), "error"));
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
    renderSession(null); renderGroupList(); await refreshStatus();
    toast("Saved review discarded", "ok");
  } catch (error) { toast(error.message, "error"); }
});

export { startScan };
