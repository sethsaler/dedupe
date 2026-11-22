// Scan setup form: sliders, persisted settings, recent paths, native picker.

import { api } from "./api.js";
import { state } from "./state.js";
import { syncRangeInput } from "./theme.js";
import { $, basename, escapeHtml, toast } from "./util.js";

const RECENT_KEY = "dedupe.recentPaths";
const WORKERS_KEY = "dedupe.workers";
const SETTINGS_KEY = "dedupe.scanSettings.v1";

/** Parse scan paths without splitting single folder names that contain commas. */
function parsePathsInput(raw) {
  const text = String(raw || "").trim();
  if (!text) return [];
  const seen = new Set();
  const parsed = [];
  for (const part of text.split(/\r?\n|,\s*(?=[/~]|[A-Za-z]:[\\/])/)) {
    const cleaned = part.trim();
    if (cleaned && !seen.has(cleaned)) {
      seen.add(cleaned);
      parsed.push(cleaned);
    }
  }
  return parsed;
}

function syncThresholdUI() {
  threshVal.textContent = thresh.value;
  const preset = $("similarityPreset");
  if (preset) {
    preset.value = [...preset.options].some((option) => option.value === thresh.value) ? thresh.value : "";
  }
  syncRangeInput(thresh);
}

const thresh = $("threshold");
const threshVal = $("threshVal");
thresh.addEventListener("input", syncThresholdUI);
$("similarityPreset").addEventListener("change", () => {
  // "" is the disabled "Custom (raw slider)" marker shown when the slider
  // sits between presets; choosing nothing keeps the slider value.
  if ($("similarityPreset").value === "") return;
  thresh.value = $("similarityPreset").value;
  syncThresholdUI();
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
  syncRangeInput(workersEl);
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

// Scan settings are always visible; there is no Options disclosure.

// —— Scan panel collapse ——
// The setup form folds into a slim bar once a scan starts (or when results
// are already loaded) so the results below get the screen; the bar re-opens
// the form on click and shows the active goal + scanned paths while collapsed.
function syncCollapseButtonVisibility() {
  const btn = $("btnCollapseScan");
  if (!btn) return;
  btn.hidden = $("results").hidden || state.scanning;
}

function collapseScanPanel(pathsText = "", goalLabel = "") {
  if (pathsText) $("scanCollapsePaths").textContent = pathsText;
  if (goalLabel) {
    $("scanCollapseGoal").textContent = goalLabel;
    $("scanCollapseGoal").hidden = false;
  }
  $("scanPanel").classList.add("collapsed");
  $("scanCollapse").setAttribute("aria-expanded", "false");
  syncCollapseButtonVisibility();
}

function expandScanPanel({ focusId = null } = {}) {
  $("scanPanel").classList.remove("collapsed");
  $("scanCollapse").setAttribute("aria-expanded", "true");
  const main = $("scanMain");
  if (main) main.scrollTop = 0;
  syncCollapseButtonVisibility();
  if (focusId) $(focusId)?.focus();
}

$("scanCollapse").addEventListener("click", () => {
  if ($("scanPanel").classList.contains("collapsed")) expandScanPanel();
  else collapseScanPanel();
});

$("btnCollapseScan")?.addEventListener("click", () => {
  collapseScanPanel();
  $("scanCollapse").focus();
});

const settingIds = [
  "preferredFolders",
  "protectedFolders",
  "optVideoVersions",
  "optParallel",
  "optExact",
  "optSimilar",
  "optSemantic",
  "optSemanticFast",
  "optDecider",
  "optNoHumans",
  "optCountFaces",
  "optBlackBorders",
  "optCropAggressiveness",
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
  "semanticDuplicateThreshold",
  "semanticSetThreshold",
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

function syncSemanticLabels() {
  $("semanticDuplicateVal").textContent = `${Number($("semanticDuplicateThreshold").value).toFixed(1).replace(/\.0$/, "")}%`;
  $("semanticSetVal").textContent = `${$("semanticSetThreshold").value}%`;
  syncRangeInput($("semanticDuplicateThreshold"));
  syncRangeInput($("semanticSetThreshold"));
}
$("semanticDuplicateThreshold").addEventListener("input", syncSemanticLabels);
$("semanticSetThreshold").addEventListener("input", syncSemanticLabels);

function restoreScanSettings() {
  try {
    const settings = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    for (const id of settingIds) {
      if (!(id in settings)) continue;
      const element = $(id);
      if (element.type === "checkbox") element.checked = !!settings[id];
      else element.value = settings[id];
    }
    syncThresholdUI();
    syncSemanticLabels();
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

function syncCropLevelVisibility() {
  const field = $("cropLevelField");
  if (field) field.hidden = !$("optBlackBorders").checked;
}
$("optBlackBorders").addEventListener("change", syncCropLevelVisibility);
syncCropLevelVisibility();

function syncSemanticVisibility() {
  $("semanticFields").hidden = !$("optSemantic").checked;
}
$("optSemantic").addEventListener("change", syncSemanticVisibility);
syncSemanticVisibility();

function syncLowResolutionVisibility() {
  const field = $("lowResFields");
  if (field) field.hidden = !$("optLowResolution").checked;
}
$("optLowResolution").addEventListener("change", syncLowResolutionVisibility);
syncLowResolutionVisibility();

function syncSimilarityVisibility() {
  const field = $("similarityFields");
  if (field) field.hidden = !$("optSimilar").checked;
}
$("optSimilar").addEventListener("change", syncSimilarityVisibility);
syncSimilarityVisibility();

// —— Scan goals ——
// Each goal is a starting point: it sets which detectors run and which tab
// the results open on. Review goals skip duplicate hashing entirely, so a
// crop or people pass over a large library is not slowed by it. Changing any
// option afterwards turns the goal into "Custom settings".
const GOAL_DETECTORS = [
  "optExact",
  "optSimilar",
  "optSemantic",
  "optVideoVersions",
  "optDecider",
  "optNoHumans",
  "optCountFaces",
  "optBlackBorders",
  "optLowResolution",
  "optRandomReview",
  "optParallel",
];
const DUPLICATES_HINT =
  "Find exact copies and similar versions, including resized or lightly edited media, across all folders.";
const DETECTOR_LABELS = {
  optExact: "Exact",
  optSimilar: "Similar",
  optSemantic: "Sets & look-alikes",
  optVideoVersions: "Video clips & versions",
  optDecider: "Decider verification",
  optNoHumans: "Non-Human",
  optCountFaces: "Faces",
  optBlackBorders: "Crops",
  optLowResolution: "Low resolution",
  optRandomReview: "Random 50",
};
const SCAN_PRESETS = {
  cleanup: { on: ["optExact", "optSimilar"], tab: "all", badge: "Duplicates", hint: DUPLICATES_HINT },
  deep: {
    on: ["optExact", "optSimilar", "optSemantic", "optVideoVersions"],
    optional: ["optSemantic", "optVideoVersions"],
    tab: "all",
    badge: "Edited copies",
    hint: "Includes edited-copy discovery with DigUp; each look-alike needs review.",
  },
  crop: {
    on: ["optBlackBorders"],
    tab: "crop",
    badge: "Crops",
    hint: "Find images and MP4/MOV videos framed by black bars, images framed in white or another flat colour, and screenshots with interface at the edges (status bar, app header, navigation bar, reply field), and suggest a crop for each. Videos are re-encoded when cropped. Duplicates are not checked.",
  },
  faces: {
    on: ["optCountFaces"],
    tab: "faces",
    badge: "Faces",
    hint: "Find images with faces, group shots first. Sort and filter them by how many faces appear.",
  },
  no_humans: {
    on: ["optNoHumans"],
    tab: "no_humans",
    badge: "Non-Human",
    hint: "Find photos and videos with no person in them, for review one at a time.",
  },
  low_resolution: {
    on: ["optLowResolution"],
    tab: "low_resolution",
    badge: "Low-res",
    hint: "Find media below the low-resolution bounds in Scan settings below (1 MP by default).",
  },
  random_review: {
    on: ["optRandomReview"],
    tab: "random_review",
    badge: "Random 50",
    hint: "Pick 50 files at random for a quick spot check of the library.",
  },
  checkup: {
    on: ["optExact", "optSimilar", "optNoHumans", "optCountFaces", "optBlackBorders", "optLowResolution"],
    optional: ["optNoHumans", "optCountFaces"],
    tab: "all",
    badge: "Full checkup",
    hint: "Duplicates, crops, faces, photos without people, and low-resolution media in one pass. Slower.",
  },
};

function syncCustomPresetHint() {
  const active = Object.entries(DETECTOR_LABELS)
    .filter(([id]) => $(id)?.checked)
    .map(([, label]) => label);
  const separate = Boolean($("optParallel")?.checked);
  if (!active.length) {
    $("presetHint").textContent = separate
      ? "Only folder browsing (Files tab) is enabled, scanning folders separately — pick a Scan goal or check detectors below."
      : "Only folder browsing (Files tab) is enabled — pick a Scan goal or check detectors below.";
    return;
  }
  $("presetHint").textContent = `Custom settings: ${active.join(", ")}${separate ? " (separate folders)" : ""}.`;
}

function settingsDetectorFlag(settings, detectorId) {
  switch (detectorId) {
    case "optExact": return Boolean(settings.exact);
    case "optSimilar": return Boolean(settings.similar);
    case "optSemantic": return Boolean(settings.semantic);
    case "optVideoVersions": return Boolean(settings.video_versions);
    case "optDecider": return Boolean(settings.decider);
    case "optNoHumans": return Boolean(settings.find_no_humans ?? settings.no_humans);
    case "optCountFaces": return Boolean(settings.count_faces);
    case "optBlackBorders": return Boolean(settings.find_black_borders);
    case "optLowResolution": return Boolean(settings.find_low_resolution ?? settings.low_resolution);
    case "optRandomReview": return Number(settings.random_review_count || 0) > 0 || Boolean(settings.random_review);
    case "optParallel": return Boolean(settings.parallel_streams);
    default: return false;
  }
}

function currentDetectorSettings() {
  return {
    exact: $("optExact")?.checked,
    similar: $("optSimilar")?.checked,
    semantic: $("optSemantic")?.checked,
    video_versions: $("optVideoVersions")?.checked,
    decider: $("optDecider")?.checked,
    find_no_humans: $("optNoHumans")?.checked,
    count_faces: $("optCountFaces")?.checked,
    find_black_borders: $("optBlackBorders")?.checked,
    find_low_resolution: $("optLowResolution")?.checked,
    random_review_count: $("optRandomReview")?.checked ? 50 : 0,
    parallel_streams: $("optParallel")?.checked,
  };
}

function inferScanPreset(settings) {
  if (!settings) return "custom";
  for (const [preset, spec] of Object.entries(SCAN_PRESETS)) {
    const on = new Set(spec.on);
    const matches = GOAL_DETECTORS.every((id) => {
      const expected = on.has(id) && !Boolean($(id)?.disabled);
      return settingsDetectorFlag(settings, id) === expected;
    });
    if (matches) return preset;
  }
  return "custom";
}

function scanGoalShortLabel(settings = null) {
  const preset = settings ? inferScanPreset(settings) : $("scanPreset").value;
  return SCAN_PRESETS[preset]?.badge || "Custom";
}

// Inputs a goal cannot run without, and why (from the dependency gates).
function goalBlocker(preset) {
  const spec = SCAN_PRESETS[preset];
  if (!spec) return null;
  const optional = new Set(spec.optional || []);
  const missing = spec.on.find((id) => !optional.has(id) && $(id).disabled);
  if (!missing) return null;
  const wrap = $(`${missing}Wrap`);
  return (wrap && wrap.title) || $(missing).title || "A required detector is not installed";
}

function presetResultsTab() {
  return SCAN_PRESETS[$("scanPreset").value]?.tab || null;
}

function applyScanPreset(preset) {
  if (preset === "custom") {
    syncCustomPresetHint();
    return;
  }
  const spec = SCAN_PRESETS[preset];
  if (!spec) return;
  const blocker = goalBlocker(preset);
  if (blocker) {
    $("presetHint").textContent = `${blocker}.`;
    return;
  }
  const on = new Set(spec.on);
  for (const id of GOAL_DETECTORS) $(id).checked = on.has(id) && !$(id).disabled;
  if (preset === "deep") { $("semanticDuplicateThreshold").value = "94"; syncSemanticLabels(); }
  const skipped = (spec.optional || []).filter((id) => $(id).disabled);
  $("presetHint").textContent = preset === "deep" && $("optSemantic").disabled
    ? "Edited-copy search needs the optional DigUp helper. Standard duplicate detection is ready."
    : skipped.length
      ? `${spec.hint} Some checks are unavailable here: run dedupe doctor for setup.`
      : spec.hint;
  syncSemanticVisibility();
  syncHumanBackendVisibility();
  syncCropLevelVisibility();
  syncLowResolutionVisibility();
  syncSimilarityVisibility();
  syncCrossFolderHint();
  syncSetupConflictHint();
  saveScanSettings();
}

// Goals whose detectors are missing are listed but disabled, with the reason.
function syncPresetAvailability() {
  const select = $("scanPreset");
  for (const option of select.options) {
    const blocker = goalBlocker(option.value);
    option.disabled = Boolean(blocker);
    option.title = blocker || "";
  }
  if (select.selectedOptions[0]?.disabled) {
    select.value = "cleanup";
    applyScanPreset("cleanup");
  }
}

$("scanPreset").addEventListener("change", () => applyScanPreset($("scanPreset").value));
// Every launch starts with the fast duplicate pass, even after a Sets or custom
// scan. Keep saved keeper preferences, exclusions, media types, and thresholds.
// Extra analysis remains available explicitly for subsequent scans in this page.
$("scanPreset").value = "cleanup";
applyScanPreset("cleanup");
for (const id of GOAL_DETECTORS) {
  $(id).addEventListener("change", () => {
    const inferred = inferScanPreset(currentDetectorSettings());
    $("scanPreset").value = inferred;
    if (inferred === "custom") syncCustomPresetHint();
    else $("presetHint").textContent = SCAN_PRESETS[inferred]?.hint || "";
  });
}

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
    });
  });
}

// —— Path chips & native folder/file pick ——
function syncPathChips() {
  const box = $("pathChips");
  const input = $("paths");
  if (!box || !input) return;
  const paths = parsePathsInput(input.value);
  if (!paths.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = paths
    .map(
      (p) =>
        `<span class="path-chip" title="${escapeHtml(p)}">`
        + `<button type="button" class="path-chip-label" data-focus-path="${escapeHtml(p)}">${escapeHtml(basename(p) || p)}</button>`
        + `<button type="button" class="path-chip-remove" data-remove-path="${escapeHtml(p)}" aria-label="Remove ${escapeHtml(p)}">✕</button>`
        + `</span>`,
    )
    .join("");
}

$("pathChips")?.addEventListener("click", (event) => {
  const removeBtn = event.target.closest("[data-remove-path]");
  if (removeBtn) {
    const target = removeBtn.dataset.removePath;
    const remaining = parsePathsInput($("paths").value).filter((p) => p !== target);
    $("paths").value = remaining.join(", ");
    syncPathChips();
    syncCrossFolderHint();
    syncSetupConflictHint();
    $("paths").dispatchEvent(new Event("input", { bubbles: true }));
    $("paths").focus();
    return;
  }
  if (event.target.closest("[data-focus-path]")) {
    $("paths").focus();
  }
});

function appendPickedPaths(paths, targetEl = $("paths"), { multiline = false } = {}) {
  if (!targetEl) return;
  const existing = multiline
    ? targetEl.value.split("\n").map((value) => value.trim()).filter(Boolean)
    : parsePathsInput(targetEl.value);
  for (const path of paths) {
    const cleaned = String(path || "").trim();
    if (cleaned && !existing.includes(cleaned)) existing.push(cleaned);
  }
  targetEl.value = existing.join(multiline ? "\n" : ", ");
  if (targetEl === $("paths")) {
    syncPathChips();
    syncCrossFolderHint();
  }
  syncSetupConflictHint();
  targetEl.dispatchEvent(new Event(multiline ? "change" : "input", { bubbles: true }));
  targetEl.focus();
}

async function pickPaths(kind, button, targetEl = $("paths"), { multiline = false } = {}) {
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
      appendPickedPaths(paths, targetEl, { multiline });
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
$("btnPickProtected")?.addEventListener("click", (event) => {
  pickPaths("folder", event.currentTarget, $("protectedFolders"), { multiline: true });
});
$("btnPickPreferred")?.addEventListener("click", (event) => {
  pickPaths("folder", event.currentTarget, $("preferredFolders"), { multiline: true });
});

function decodeFileUrl(raw) {
  let path = raw.replace(/^file:\/\/(?:localhost)?/i, "");
  try {
    path = decodeURIComponent(path);
  } catch {
    /* keep raw percent sequences if malformed */
  }
  if (/^\/[A-Za-z]:\//.test(path)) path = path.slice(1);
  return path.trim();
}

function unquoteShellLine(line) {
  const trimmed = line.trim();
  if (!trimmed) return [];
  const multiQuoted = trimmed.match(/^(?:'[^']+'|"[^"]+")(?:\s+(?:'[^']+'|"[^"]+"))+$/);
  if (multiQuoted) {
    return [...trimmed.matchAll(/'([^']+)'|"([^"]+)"/g)].map((m) => (m[1] ?? m[2]).trim()).filter(Boolean);
  }
  if ((trimmed.startsWith("'") && trimmed.endsWith("'")) || (trimmed.startsWith('"') && trimmed.endsWith('"'))) {
    const inner = trimmed.slice(1, -1).trim();
    return inner ? [inner] : [];
  }
  // Unescape POSIX backslash-escaped spaces when not a Windows drive path.
  const normalized = /^[A-Za-z]:\\/.test(trimmed) ? trimmed : trimmed.replace(/\\ /g, " ");
  return parsePathsInput(normalized);
}

function parseDroppedPaths(dataTransfer) {
  const uriList = dataTransfer.getData("text/uri-list") || "";
  const plain = dataTransfer.getData("text/plain") || "";
  const raw = uriList.trim() ? uriList : plain;
  if (!raw.trim()) return [];
  const seen = new Set();
  const out = [];
  const isUriList = Boolean(uriList.trim());
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || (isUriList && trimmed.startsWith("#"))) continue;
    const candidates = /^file:\/\//i.test(trimmed)
      ? [decodeFileUrl(trimmed)]
      : unquoteShellLine(trimmed);
    for (const candidate of candidates) {
      if (candidate && !seen.has(candidate)) {
        seen.add(candidate);
        out.push(candidate);
      }
    }
  }
  return out;
}

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
  const dropped = parseDroppedPaths(e.dataTransfer);
  if (dropped.length) {
    appendPickedPaths(dropped);
  } else if (e.dataTransfer.files?.length) {
    // Browsers never expose a dropped item's absolute path; say so instead of
    // silently ignoring the drop.
    toast("The browser can't read a dropped folder's location — use Folders… to pick it", "error");
  }
});

function normalizeFolderCompare(p) {
  const trimmed = String(p || "").trim();
  return trimmed.length > 1 ? trimmed.replace(/[\\/]+$/, "") : trimmed;
}

// —— Cross-folder duplicate & setup conflict hints ——
// Opt-in independent streams never match files across
// folders; say so next to the path field, where the user actually looks.
function syncCrossFolderHint() {
  const hint = $("crossFolderHint");
  if (!hint) return;
  const pathCount = parsePathsInput($("paths")?.value).length;
  hint.hidden = !(pathCount > 1 && $("optParallel").checked);
}

function syncSetupConflictHint() {
  const hint = $("setupConflictHint");
  if (!hint) return;
  const scanPaths = parsePathsInput($("paths")?.value);
  const protectedPaths = ($("protectedFolders")?.value || "")
    .split("\n")
    .map((value) => value.trim())
    .filter(Boolean);
  const messages = [];
  if (protectedPaths.length > 0 && $("optParallel")?.checked) {
    messages.push(
      "Protected libraries require comparing folders together — turn off “Scan folders separately” before scanning.",
    );
  }
  if (scanPaths.length > 0 && protectedPaths.length > 0) {
    const protectedSet = new Set(protectedPaths.map(normalizeFolderCompare));
    const overlap = scanPaths.filter((p) => protectedSet.has(normalizeFolderCompare(p)));
    if (overlap.length > 0) {
      messages.push(
        `Listed in both Scan folders and Protected library (${overlap.map((p) => basename(p) || p).join(", ")}): protected folders stay read-only.`,
      );
    }
  }
  hint.hidden = messages.length === 0;
  hint.textContent = messages.join(" ");
}

function onPathsFieldChanged() {
  syncPathChips();
  syncCrossFolderHint();
  syncSetupConflictHint();
}
$("paths").addEventListener("input", onPathsFieldChanged);
$("paths").addEventListener("change", onPathsFieldChanged);
$("protectedFolders")?.addEventListener("input", syncSetupConflictHint);
$("protectedFolders")?.addEventListener("change", syncSetupConflictHint);
$("optParallel").addEventListener("change", () => {
  syncCrossFolderHint();
  syncSetupConflictHint();
});
syncPathChips();
syncCrossFolderHint();
syncSetupConflictHint();

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
    const target = wrap || input;
    if (target.dataset.defaultTitle === undefined) {
      target.dataset.defaultTitle = target.getAttribute("title") || "";
    }
    input.disabled = !ready;
    if (!ready) {
      input.checked = false;
      target.title = reason;
    } else if (target.dataset.defaultTitle) {
      target.title = target.dataset.defaultTitle;
    } else {
      target.removeAttribute("title");
    }
  };
  gate("optVideoVersions", "optVideoVersionsWrap", Boolean(caps.ffmpeg && caps.ffprobe),
    "Video version detection needs ffmpeg and ffprobe");
  gate(
    "optNoHumans",
    "optNoHumansWrap",
    noHumansReady,
    "Non-Human detection needs OpenCV or Photon — run `dedupe doctor` for setup",
  );
  gate(
    "optSemantic",
    "optSemanticWrap",
    Boolean(caps.semantic),
    "Look-alikes need DigUp installed (github.com/ARahim3/DigUp) — it runs EmbeddingGemma 2",
  );
  gate(
    "optDecider",
    "optDeciderWrap",
    Boolean(caps.decider),
    "Decider verification needs Decider.app running — run `dedupe doctor` to check",
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
  syncPresetAvailability();
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
  syncSemanticVisibility();
  syncCropLevelVisibility();
  syncLowResolutionVisibility();
  syncSimilarityVisibility();
}

function setCheckboxFromSetting(id, value) {
  const el = $(id);
  if (!el) return;
  el.checked = !el.disabled && Boolean(value);
}

function formatMegapixels(pixelsValue, mpValue) {
  if (mpValue != null && Number.isFinite(Number(mpValue))) return String(mpValue);
  if (pixelsValue == null || !Number.isFinite(Number(pixelsValue))) return null;
  return String(Number((Number(pixelsValue) / 1_000_000).toFixed(4)));
}

/** Hydrate the scan setup form from a saved or active scan_settings object. */
function applySettingsToForm(settings) {
  if (!settings || typeof settings !== "object") return;
  if ("paths" in settings) {
    const paths = Array.isArray(settings.paths)
      ? settings.paths.map((s) => String(s).trim()).filter(Boolean)
      : parsePathsInput(settings.paths);
    if ($("paths")) $("paths").value = paths.join(", ");
    syncPathChips();
  }
  if ("protected_folders" in settings && $("protectedFolders")) {
    $("protectedFolders").value = Array.isArray(settings.protected_folders)
      ? settings.protected_folders.join("\n")
      : (settings.protected_folders || "");
  }
  if ("preferred_folders" in settings && $("preferredFolders")) {
    $("preferredFolders").value = Array.isArray(settings.preferred_folders)
      ? settings.preferred_folders.join("\n")
      : (settings.preferred_folders || "");
  }
  const boolMappings = [
    ["exact", "optExact"],
    ["similar", "optSimilar"],
    ["video_versions", "optVideoVersions"],
    ["semantic", "optSemantic"],
    ["decider", "optDecider"],
    ["count_faces", "optCountFaces"],
    ["find_black_borders", "optBlackBorders"],
    ["low_resolution_images", "optLowResolutionImages"],
    ["low_resolution_gifs", "optLowResolutionGifs"],
    ["low_resolution_videos", "optLowResolutionVideos"],
    ["include_images", "optImages"],
    ["include_gifs", "optGifs"],
    ["include_videos", "optVideos"],
    ["parallel_streams", "optParallel"],
  ];
  for (const [key, id] of boolMappings) {
    if (key in settings) setCheckboxFromSetting(id, settings[key]);
  }
  if ("semantic_full_speed" in settings || "semantic_fast" in settings) {
    setCheckboxFromSetting("optSemanticFast", settings.semantic_full_speed ?? settings.semantic_fast);
  }
  if ("find_no_humans" in settings || "no_humans" in settings) {
    setCheckboxFromSetting("optNoHumans", settings.find_no_humans ?? settings.no_humans);
  }
  if ("find_low_resolution" in settings || "low_resolution" in settings) {
    setCheckboxFromSetting("optLowResolution", settings.find_low_resolution ?? settings.low_resolution);
  }
  if ("random_review_count" in settings || "random_review" in settings) {
    const active = settings.random_review_count != null
      ? Number(settings.random_review_count) > 0
      : Boolean(settings.random_review);
    setCheckboxFromSetting("optRandomReview", active);
  }
  if ("human_backend" in settings && $("humanBackend")) {
    const val = String(settings.human_backend || "opencv");
    const opt = [...$("humanBackend").options].find((o) => o.value === val && !o.disabled);
    if (opt) $("humanBackend").value = val;
  }
  if ("crop_aggressiveness" in settings && $("optCropAggressiveness")) {
    $("optCropAggressiveness").value = String(settings.crop_aggressiveness || "balanced");
  }
  if (settings.semantic_duplicate_threshold != null && $("semanticDuplicateThreshold")) {
    const raw = Number(settings.semantic_duplicate_threshold);
    if (Number.isFinite(raw)) {
      $("semanticDuplicateThreshold").value = String(raw <= 1 ? raw * 100 : raw);
    }
  }
  const semSetRaw = settings.semantic_threshold ?? settings.semantic_set_threshold;
  if (semSetRaw != null && $("semanticSetThreshold")) {
    const raw = Number(semSetRaw);
    if (Number.isFinite(raw)) {
      $("semanticSetThreshold").value = String(raw <= 1 ? raw * 100 : raw);
    }
  }
  for (const [pixelsKey, mpKey, id] of [
    ["low_resolution_image_max_pixels", "low_resolution_image_max_mp", "lowResolutionImageMaxMp"],
    ["low_resolution_gif_max_pixels", "low_resolution_gif_max_mp", "lowResolutionGifMaxMp"],
    ["low_resolution_video_max_pixels", "low_resolution_video_max_mp", "lowResolutionVideoMaxMp"],
  ]) {
    const formatted = formatMegapixels(settings[pixelsKey], settings[mpKey]);
    if (formatted !== null && $(id)) $(id).value = formatted;
  }
  const simThresh = settings.threshold ?? settings.similarity_threshold;
  if (simThresh != null && Number.isFinite(Number(simThresh))) {
    thresh.value = String(simThresh);
  }
  if ("workers" in settings && workersEl) {
    const maxW = Number(workersEl.max || 32);
    workersEl.value = settings.workers == null
      ? "0"
      : String(Math.max(0, Math.min(maxW, Number(settings.workers) || 0)));
  }
  if ("exclusions" in settings && $("exclusions")) {
    $("exclusions").value = Array.isArray(settings.exclusions)
      ? settings.exclusions.join(", ")
      : (settings.exclusions || "");
  }
  syncThresholdUI();
  syncSemanticLabels();
  updateWorkersUI();
  syncSemanticVisibility();
  syncHumanBackendVisibility();
  syncCropLevelVisibility();
  syncLowResolutionVisibility();
  syncSimilarityVisibility();
  syncCrossFolderHint();
  syncSetupConflictHint();
  const inferredPreset = inferScanPreset(settings);
  $("scanPreset").value = inferredPreset;
  if (inferredPreset === "custom") syncCustomPresetHint();
  else $("presetHint").textContent = SCAN_PRESETS[inferredPreset]?.hint || "";
}

// —— Exclusion check ——
$("btnCheckExclusions").addEventListener("click", async () => {
  const result = $("exclusionsCheckResult");
  const paths = parsePathsInput($("paths").value);
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

export {
  SCAN_PRESETS,
  parsePathsInput,
  applySettingsToForm,
  updateWorkersUI,
  workersEl,
  saveRecent,
  renderRecent,
  lowResolutionMaxPixels,
  collapseScanPanel,
  expandScanPanel,
  applyCapabilities,
  syncCrossFolderHint,
  syncSetupConflictHint,
  syncCollapseButtonVisibility,
  syncCustomPresetHint,
  inferScanPreset,
  scanGoalShortLabel,
  presetResultsTab,
};
