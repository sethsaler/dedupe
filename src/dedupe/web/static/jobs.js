import { api } from "./api.js";
import { collectScanSettings, startScan } from "./scan.js";
import { loadGroups } from "./groups.js";
import { confirmModal } from "./modal.js";
import { updateWorkersUI } from "./settings.js";
import { state } from "./state.js";
import { refreshStatus } from "./status.js";
import { $, escapeHtml, toast } from "./util.js";

let jobs = [];
let lastCompletedScan = null;
const fields = {
  exact: "optExact", similar: "optSimilar", semantic: "optSemantic", decider: "optDecider",
  video_versions: "optVideoVersions", find_no_humans: "optNoHumans", count_faces: "optCountFaces",
  find_black_borders: "optBlackBorders",
  find_low_resolution: "optLowResolution", low_resolution_images: "optLowResolutionImages",
  low_resolution_gifs: "optLowResolutionGifs", low_resolution_videos: "optLowResolutionVideos",
  human_backend: "humanBackend", include_images: "optImages", include_gifs: "optGifs",
  include_videos: "optVideos", threshold: "threshold", parallel_streams: "optParallel", workers: "workers",
};

function restoreSettings(settings) {
  for (const [key, id] of Object.entries(fields)) {
    if (!(key in settings)) continue;
    const element = $(id);
    if (element.type === "checkbox") element.checked = !!settings[key];
    else element.value = settings[key] ?? 0;
    element.dispatchEvent(new Event("change"));
  }
  $("paths").value = (settings.paths || []).join(", ");
  $("preferredFolders").value = (settings.preferred_folders || []).join("\n");
  $("protectedFolders").value = (settings.protected_folders || []).join("\n");
  $("exclusions").value = (settings.exclusions || []).join(", ");
  $("optRandomReview").checked = (settings.random_review_count || 0) > 0;
  for (const [key, id, multiplier] of [
    ["semantic_threshold", "semanticSetThreshold", 100],
    ["semantic_duplicate_threshold", "semanticDuplicateThreshold", 100],
    ["low_resolution_image_max_pixels", "lowResolutionImageMaxMp", 0.000001],
    ["low_resolution_gif_max_pixels", "lowResolutionGifMaxMp", 0.000001],
    ["low_resolution_video_max_pixels", "lowResolutionVideoMaxMp", 0.000001],
  ]) {
    if (settings[key] != null) $(id).value = settings[key] * multiplier;
    $(id).dispatchEvent(new Event("input"));
  }
  $("threshold").dispatchEvent(new Event("input"));
  $("scanPreset").value = "custom";
  $("semanticFields").hidden = !$("optSemantic").checked;
  updateWorkersUI();
}

function chosenJob() { return jobs.find((job) => job.id === $("cleanupJob").value); }

async function refreshJobs(selected = $("cleanupJob").value) {
  const data = await api("/api/jobs");
  jobs = data.jobs;
  $("cleanupJob").innerHTML = '<option value="">New cleanup</option>' + jobs.map((job) =>
    `<option value="${job.id}">${escapeHtml(job.name)}</option>`).join("");
  $("cleanupJob").value = selected;
  state.selectedJobId = chosenJob()?.id || null;
  $("btnResumeJob").disabled = !chosenJob()?.last_scan_at;
  $("btnNewArrivals").disabled = !chosenJob()?.last_scan_at;
  $("btnDeleteJob").disabled = !chosenJob();
}

$("cleanupJob").addEventListener("change", () => {
  const job = chosenJob();
  state.selectedJobId = job?.id || null;
  $("cleanupJobName").value = job?.name || "";
  if (job) restoreSettings(job.settings);
  $("btnResumeJob").disabled = !job?.last_scan_at;
  $("btnNewArrivals").disabled = !job?.last_scan_at;
  $("btnDeleteJob").disabled = !job;
  $("jobHint").textContent = job?.last_scan_at
    ? `Last scanned ${new Date(job.last_scan_at).toLocaleString()}. Resume your review or check new arrivals.`
    : "Save folders and settings together. Each job keeps its own review progress.";
});

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
  try {
    await api("/api/review-session/resume", { method: "POST", body: JSON.stringify({ job_id: chosenJob().id }) });
    await refreshStatus();
    await loadGroups();
  } catch (error) { toast(error.message, "error"); }
});

$("btnNewArrivals").addEventListener("click", () => {
  const job = chosenJob();
  if (job) startScan({ ...job.settings, job_id: job.id, new_only: true });
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

$("savedJobs").addEventListener("toggle", () => {
  if ($("savedJobs").open) refreshJobs().catch((error) => toast(error.message, "error"));
});
refreshJobs().catch((error) => toast(error.message, "error"));

window.addEventListener("dedupe:work-state", (event) => {
  const { busy, scanId, done } = event.detail;
  $("btnSaveJob").disabled = busy;
  $("btnResumeJob").disabled = busy || !chosenJob()?.last_scan_at;
  $("btnNewArrivals").disabled = busy || !chosenJob()?.last_scan_at;
  $("btnDeleteJob").disabled = busy || !chosenJob();
  if (!busy && done && scanId !== lastCompletedScan) {
    lastCompletedScan = scanId;
    refreshJobs().catch((error) => toast(error.message, "error"));
  }
});
