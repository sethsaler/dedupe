import { api } from "./api.js";
import { groupNeedsAttention } from "./model.js";
import { confirmModal } from "./modal.js";
import { collectScanSettings, startScan, switchTab } from "./scan.js";
import {
  applySettingsToForm,
  collapseScanPanel,
  scanGoalShortLabel,
} from "./settings.js";
import { state } from "./state.js";
import { refreshStatus } from "./status.js";
import { $, escapeHtml, toast } from "./util.js";

let jobs = [];
let lastCompletedScan = null;

function normalizedSettingValue(value) {
  if (Array.isArray(value)) return value.map((item) => String(item).trim());
  if (value === undefined) return null;
  return value;
}

function areJobSettingsDirty(savedSettings) {
  if (!savedSettings || typeof savedSettings !== "object") return false;
  const current = collectScanSettings();
  for (const [key, savedRaw] of Object.entries(savedSettings)) {
    if (key === "job_id" || key === "new_only" || key === "scan_id" || key === "retry") continue;
    const saved = normalizedSettingValue(savedRaw);
    const cur = normalizedSettingValue(current[key]);
    if (Array.isArray(saved) || Array.isArray(cur)) {
      const a = Array.isArray(saved) ? saved : [];
      const b = Array.isArray(cur) ? cur : [];
      if (a.length !== b.length || a.some((val, idx) => val !== b[idx])) return true;
    } else if (cur !== saved) {
      return true;
    }
  }
  return false;
}

function chosenJob() { return jobs.find((job) => job.id === $("cleanupJob").value); }

function syncJobButtons(busy = state.scanning || state.acting) {
  const job = chosenJob();
  $("btnSaveJob").disabled = busy;
  $("btnResumeJob").disabled = busy || !job?.last_scan_at;
  $("btnNewArrivals").disabled = busy || !job?.last_scan_at;
  $("btnDeleteJob").disabled = busy || !job;
}

function updateJobHint() {
  const hint = $("jobHint");
  if (!hint) return;
  const job = chosenJob();
  if (!job) {
    hint.textContent = "Save folders and settings together. Each job keeps its own review progress.";
    return;
  }
  if (areJobSettingsDirty(job.settings)) {
    hint.textContent =
      `Unsaved changes for "${job.name}" — click Save job or Scan to overwrite (or choose "New cleanup" to keep it unchanged).`;
    return;
  }
  hint.textContent = job.last_scan_at
    ? `Last scanned ${new Date(job.last_scan_at).toLocaleString()}. Resume your review or check new arrivals.`
    : `Saved job "${job.name}" — run Scan to build its baseline and review progress.`;
}

function restoreSettings(settings) {
  applySettingsToForm(settings);
  updateJobHint();
}

async function refreshJobs(selected = $("cleanupJob").value) {
  const data = await api("/api/jobs");
  jobs = data.jobs;
  $("cleanupJob").innerHTML = '<option value="">New cleanup</option>' + jobs.map((job) =>
    `<option value="${job.id}">${escapeHtml(job.name)}</option>`).join("");
  $("cleanupJob").value = jobs.some((job) => job.id === selected) ? selected : "";
  const job = chosenJob();
  state.selectedJobId = job?.id || null;
  syncJobButtons();
  updateJobHint();
}

$("cleanupJob").addEventListener("change", () => {
  const job = chosenJob();
  state.selectedJobId = job?.id || null;
  $("cleanupJobName").value = job?.name || "";
  if (job) restoreSettings(job.settings);
  else updateJobHint();
  syncJobButtons();
});

const onJobFormChange = (event) => {
  const id = event.target?.id;
  if (id === "cleanupJob" || id === "cleanupJobName") return;
  updateJobHint();
};
$("scanMain")?.addEventListener("input", onJobFormChange);
$("scanMain")?.addEventListener("change", onJobFormChange);

$("btnSaveJob").addEventListener("click", async () => {
  try {
    const settings = collectScanSettings();
    if (!settings.paths.length) throw new Error("Enter a cleanup folder first");
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify({
      id: state.selectedJobId, name: $("cleanupJobName").value, settings,
    }) });
    await refreshJobs(job.id);
    toast(`Saved ${job.name}`, "ok");
  } catch (error) { toast(error.message, "error"); }
});

$("btnResumeJob").addEventListener("click", async () => {
  const job = chosenJob();
  if (!job) return;
  const button = $("btnResumeJob");
  const originalLabel = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Resuming…";
  try {
    const resumed = await api("/api/review-session/resume", {
      method: "POST",
      body: JSON.stringify({ job_id: job.id }),
    });
    state.scanId = resumed.scan_id || state.scanId;
    const status = await refreshStatus();
    const settings = status?.scan_settings && Object.keys(status.scan_settings).length
      ? status.scan_settings
      : job.settings;
    applySettingsToForm(settings);
    updateJobHint();
    collapseScanPanel(
      (settings?.paths || job.settings?.paths || []).join(", "),
      scanGoalShortLabel(settings),
    );
    if (state.allGroups.length > 0 || status?.summary?.group_count > 0) {
      $("emptyState").hidden = true;
      $("results").hidden = false;
      if (state.groups.length === 0 && state.allGroups.length > 0) {
        await switchTab("all");
      }
    }
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.removeAttribute("aria-busy");
    button.textContent = originalLabel;
    syncJobButtons();
  }
});

$("btnNewArrivals").addEventListener("click", async () => {
  const job = chosenJob();
  if (!job) return;
  const hasOpenUnfinished = state.allGroups.some(groupNeedsAttention);
  const hasUnloadedSaved = Boolean(job.last_scan_at && state.lastScanSettings?.job_id !== job.id);
  if (hasOpenUnfinished || hasUnloadedSaved) {
    const ok = await confirmModal({
      title: `Check new arrivals for ${job.name}?`,
      body: "<p>Checking new arrivals scans only for new or changed files against the job's baseline. If 0 new files are found, the saved review snapshot for this job is cleared — resume your review first if you still have unfinished items.</p>",
      confirmLabel: "Check new arrivals",
    });
    if (!ok) return;
  }
  startScan({ ...job.settings, job_id: job.id, new_only: true });
});

$("btnDeleteJob").addEventListener("click", async () => {
  const job = chosenJob();
  if (!job) return;
  const ok = await confirmModal({ title: `Delete ${job.name}?`,
    body: "<p>This removes the saved job and its review progress. Your media files stay where they are.</p>",
    confirmLabel: "Delete job" });
  if (!ok) return;
  try {
    await api("/api/jobs/delete", { method: "POST", body: JSON.stringify({ id: job.id }) });
    await refreshJobs("");
    $("cleanupJobName").value = "";
  } catch (error) { toast(error.message, "error"); }
});

refreshJobs().catch((error) => toast(error.message, "error"));

window.addEventListener("dedupe:work-state", (event) => {
  const { busy, scanId, done } = event.detail;
  syncJobButtons(busy);
  if (!busy && done && scanId !== lastCompletedScan) {
    lastCompletedScan = scanId;
    refreshJobs().catch((error) => toast(error.message, "error"));
  }
});
