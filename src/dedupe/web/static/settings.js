// Scan setup form: sliders, persisted settings, recent paths, native picker.

import { api } from "./api.js";
import { state } from "./state.js";
import { $, escapeHtml, toast } from "./util.js";

const RECENT_KEY = "dedupe.recentPaths";
const WORKERS_KEY = "dedupe.workers";
const SETTINGS_KEY = "dedupe.scanSettings.v1";

const thresh = $("threshold");
const threshVal = $("threshVal");
thresh.addEventListener("input", () => {
  threshVal.textContent = thresh.value;
  const preset = $("similarityPreset");
  preset.value = [...preset.options].some((option) => option.value === thresh.value) ? thresh.value : "";
});
$("similarityPreset").addEventListener("change", () => {
  // "" is the disabled "Custom (raw slider)" marker shown when the slider
  // sits between presets; choosing nothing keeps the slider value.
  if ($("similarityPreset").value === "") return;
  thresh.value = $("similarityPreset").value;
  threshVal.textContent = thresh.value;
  saveScanSettings();
});

const workersEl = $("workers");
const workersVal = $("workersVal");
const workersHint = $("workersHint");

function formatWorkersLabel(n) {
  const v = Number(n) || 0;
  if (v <= 0) return "Auto";
  return String(v);
}

function updateWorkersUI() {
  const v = Number(workersEl.value) || 0;
  workersVal.textContent = formatWorkersLabel(v);
  if (v <= 0) {
    const auto = state.autoWorkers || "auto";
    workersHint.textContent =
      state.cpuCount > 0
        ? `≈${auto} of ${state.cpuCount} cores (safe default)`
        : "parallel hashing (safe default)";
  } else if (v === 1) {
    workersHint.textContent = "serial — lighter on CPU/disk";
  } else {
    workersHint.textContent = "parallel hashing";
  }
}

workersEl.addEventListener("input", () => {
  updateWorkersUI();
  try {
    localStorage.setItem(WORKERS_KEY, workersEl.value);
  } catch {
    /* ignore */
  }
});

try {
  const saved = localStorage.getItem(WORKERS_KEY);
  if (saved !== null && saved !== "") {
    workersEl.value = String(Math.max(0, Math.min(32, Number(saved) || 0)));
  }
} catch {
  /* ignore */
}
updateWorkersUI();

// —— Options toggle ——
$("optsToggle").addEventListener("click", () => {
  const panel = $("optionsPanel");
  const open = panel.hidden;
  panel.hidden = !open;
  $("optsToggle").setAttribute("aria-expanded", open ? "true" : "false");
});

// —— Scan panel collapse ——
// The setup form folds into a slim bar once a scan starts (or when results
// are already loaded) so the results below get the screen; the bar re-opens
// the form on click and shows the scanned paths while collapsed.
$("scanCollapse").addEventListener("click", () => {
  const collapsed = $("scanPanel").classList.toggle("collapsed");
  $("scanCollapse").setAttribute("aria-expanded", collapsed ? "false" : "true");
});

function collapseScanPanel(pathsText = "") {
  if (pathsText) $("scanCollapsePaths").textContent = pathsText;
  $("scanPanel").classList.add("collapsed");
  $("scanCollapse").setAttribute("aria-expanded", "false");
}

const settingIds = [
  "optExact",
  "optSimilar",
  "optNoHumans",
  "optCountFaces",
  "humanBackend",
  "optLowResolution",
  "optLowResolutionImages",
  "optLowResolutionGifs",
  "optLowResolutionVideos",
  "lowResolutionImageMaxMp",
  "lowResolutionGifMaxMp",
  "lowResolutionVideoMaxMp",
  "optRandomReview",
  "optImages",
  "optGifs",
  "optVideos",
  "threshold",
  "exclusions",
];

function saveScanSettings() {
  const settings = {};
  for (const id of settingIds) {
    const element = $(id);
    settings[id] = element.type === "checkbox" ? element.checked : element.value;
  }
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    /* ignore */
  }
}

function restoreScanSettings() {
  try {
    const settings = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    for (const id of settingIds) {
      if (!(id in settings)) continue;
      const element = $(id);
      if (element.type === "checkbox") element.checked = !!settings[id];
      else element.value = settings[id];
    }
    threshVal.textContent = thresh.value;
  } catch {
    /* ignore */
  }
}

restoreScanSettings();
for (const id of settingIds) {
  $(id).addEventListener($(id).type === "range" ? "input" : "change", saveScanSettings);
}

function syncHumanBackendVisibility() {
  const field = $("humanBackendField");
  if (field) field.hidden = !$("optNoHumans").checked;
}
$("optNoHumans").addEventListener("change", syncHumanBackendVisibility);
syncHumanBackendVisibility();

// —— Recent paths ——
function loadRecent() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveRecent(path) {
  if (!path) return;
  const list = loadRecent().filter((p) => p !== path);
  list.unshift(path);
  localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 6)));
  renderRecent();
}

function renderRecent() {
  const box = $("recentPaths");
  const list = loadRecent();
  if (!list.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = list
    .map(
      (p) =>
        `<button type="button" class="recent-chip" data-path="${escapeHtml(p)}" title="${escapeHtml(p)}">${escapeHtml(p)}</button>`
    )
    .join("");
  box.querySelectorAll(".recent-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      // Append like the native picker does; replacing wiped typed paths.
      appendPickedPaths([btn.dataset.path]);
      syncCrossFolderHint();
    });
  });
}

// —— Native folder/file pick (the local server can return absolute paths) ——
function appendPickedPaths(paths) {
  const input = $("paths");
  const existing = input.value
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  for (const path of paths) {
    if (path && !existing.includes(path)) existing.push(path);
  }
  input.value = existing.join(", ");
  input.focus();
}

async function pickPaths(kind, button) {
  const originalLabel = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Opening…";
  try {
    const data = await api("/api/pick-folder", {
      method: "POST",
      body: JSON.stringify({ kind }),
    });
    const paths = data.paths || (data.path ? [data.path] : []);
    if (paths.length) {
      appendPickedPaths(paths);
      toast(`${paths.length} ${kind === "folder" ? "folder" : "file"}${paths.length === 1 ? "" : "s"} added`, "ok");
    } else if (data.cancelled) {
      /* user cancelled */
    } else {
      toast(data.message || "Paste a local path instead");
    }
  } catch (e) {
    toast(e.message || "Could not open the native picker — paste a path instead", "error");
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = originalLabel;
  }
}

$("btnPickFolder").addEventListener("click", (event) => {
  pickPaths("folder", event.currentTarget);
});
$("btnPickFiles").addEventListener("click", (event) => {
  pickPaths("files", event.currentTarget);
});

// Drag path text onto input
const pathWrap = document.querySelector(".path-input-wrap");
["dragenter", "dragover"].forEach((ev) => {
  pathWrap.addEventListener(ev, (e) => {
    e.preventDefault();
    pathWrap.classList.add("drag-over");
  });
});
["dragleave", "drop"].forEach((ev) => {
  pathWrap.addEventListener(ev, (e) => {
    e.preventDefault();
    pathWrap.classList.remove("drag-over");
  });
});
pathWrap.addEventListener("drop", (e) => {
  const text =
    e.dataTransfer.getData("text/plain") ||
    e.dataTransfer.getData("text/uri-list") ||
    "";
  if (text.trim()) {
    const cleaned = text.trim().replace(/^file:\/\//, "");
    const cur = $("paths").value.trim();
    $("paths").value = cur ? `${cur}, ${cleaned}` : cleaned;
    syncCrossFolderHint();
  } else if (e.dataTransfer.files?.length) {
    // Browsers never expose a dropped item's absolute path; say so instead of
    // silently ignoring the drop.
    toast("The browser can't read a dropped folder's location — use Folders… to pick it", "error");
  }
});

// —— Cross-folder duplicate hint ——
// Parallel streams (the default for several paths) never match files across
// folders; say so next to the path field, where the user actually looks.
function syncCrossFolderHint() {
  const hint = $("crossFolderHint");
  if (!hint) return;
  const pathCount = $("paths")
    .value.split(",")
    .map((value) => value.trim())
    .filter(Boolean).length;
  hint.hidden = !(pathCount > 1 && $("optParallel").checked);
}
$("paths").addEventListener("input", syncCrossFolderHint);
$("optParallel").addEventListener("change", syncCrossFolderHint);
syncCrossFolderHint();

// —— Dependency-gated options ——
// The server probes optional dependencies once at startup; options that need a
// missing one are disabled with the reason, instead of failing at scan time.
function applyCapabilities(caps) {
  if (!caps) return;
  const opencvReady = Boolean(caps.opencv && caps.yunet_model);
  const noHumansReady = opencvReady || Boolean(caps.photon);
  const gate = (inputId, wrapId, ready, reason) => {
    const input = $(inputId);
    const wrap = wrapId ? $(wrapId) : null;
    if (!input) return;
    input.disabled = !ready;
    if (!ready) {
      input.checked = false;
      (wrap || input).title = reason;
    } else {
      (wrap || input).removeAttribute("title");
    }
  };
  gate(
    "optNoHumans",
    "optNoHumansWrap",
    noHumansReady,
    "Non-Human detection needs OpenCV or Photon — run `dedupe doctor` for setup",
  );
  gate(
    "optCountFaces",
    "optCountFacesWrap",
    Boolean(caps.opencv),
    "Face counting needs OpenCV — run `dedupe doctor` for setup",
  );
  const backend = $("humanBackend");
  if (backend) {
    const optionReady = {
      opencv: opencvReady,
      photon: Boolean(caps.photon),
      ensemble: opencvReady && Boolean(caps.photon),
    };
    const optionReason = {
      opencv: "needs OpenCV and the bundled YuNet model",
      photon: "needs the Moondream SDK (pip install -e '.[photon]')",
      ensemble: "needs both OpenCV and the Moondream SDK",
    };
    for (const option of backend.options) {
      option.disabled = !optionReady[option.value];
      option.title = option.disabled ? optionReason[option.value] : "";
    }
    if (backend.selectedOptions[0]?.disabled) {
      const firstReady = [...backend.options].find((option) => !option.disabled);
      if (firstReady) backend.value = firstReady.value;
    }
  }
  const hint = $("depHint");
  if (hint) {
    const missing = [];
    if (!opencvReady) missing.push("OpenCV");
    if (!caps.photon) missing.push("Photon");
    if (!caps.ffmpeg) missing.push("ffmpeg (video similarity and thumbnails limited)");
    hint.hidden = !missing.length;
    hint.textContent = missing.length
      ? `Not installed: ${missing.join(" · ")} — some options are disabled. Run dedupe doctor for details.`
      : "";
  }
  syncHumanBackendVisibility();
}

// —— Exclusion check ——
$("btnCheckExclusions").addEventListener("click", async () => {
  const result = $("exclusionsCheckResult");
  const paths = $("paths").value.split(",").map((value) => value.trim()).filter(Boolean);
  const exclusions = $("exclusions").value.split(",").map((value) => value.trim()).filter(Boolean);
  if (!paths.length) {
    toast("Enter the scan folders first, then check exclusions");
    $("paths").focus();
    return;
  }
  if (!exclusions.length) {
    result.hidden = false;
    result.textContent = "No exclusion globs to check.";
    return;
  }
  result.hidden = false;
  result.textContent = "Checking…";
  try {
    const data = await api("/api/scan/check-exclusions", {
      method: "POST",
      body: JSON.stringify({ paths, exclusions }),
    });
    const parts = (data.patterns || []).map((entry) => {
      const noun = entry.matches === 1 ? "match" : "matches";
      return entry.matches > 0
        ? `✓ ${entry.pattern} — ${entry.matches}${data.truncated ? "+" : ""} ${noun}`
        : `⚠ ${entry.pattern} — matches nothing (typo?)`;
    });
    result.textContent = data.truncated
      ? `${parts.join("  ·  ")}  ·  stopped early (large tree) — counts are lower bounds`
      : parts.join("  ·  ");
  } catch (error) {
    result.hidden = true;
    toast(error.message || "Could not check exclusions", "error");
  }
});

function lowResolutionMaxPixels(id) {
  const megapixels = Number($(id).value);
  if (!Number.isFinite(megapixels) || megapixels <= 0) return null;
  return Math.round(megapixels * 1_000_000);
}

export { updateWorkersUI, workersEl, saveRecent, renderRecent, lowResolutionMaxPixels, collapseScanPanel, applyCapabilities, syncCrossFolderHint };
