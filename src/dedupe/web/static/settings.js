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
      $("paths").value = btn.dataset.path;
      $("paths").focus();
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
  }
});

function lowResolutionMaxPixels(id) {
  const megapixels = Number($(id).value);
  if (!Number.isFinite(megapixels) || megapixels <= 0) return null;
  return Math.round(megapixels * 1_000_000);
}

export { updateWorkersUI, workersEl, saveRecent, renderRecent, lowResolutionMaxPixels };
