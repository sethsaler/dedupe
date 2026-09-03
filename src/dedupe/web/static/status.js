// Status polling + SSE stream, scan progress, session banner, diagnostics.

import { api } from "./api.js";
import { addStreamedGroup, loadGroups } from "./groups.js";
import { applyCapabilities, updateWorkersUI, workersEl } from "./settings.js";
import { state } from "./state.js";
import { $, basename, escapeHtml, formatDuration, toast } from "./util.js";

// —— Per-folder parallel scan streams ——
function renderStreams(streams, scanning) {
  const panel = $("streamProgress");
  if (!panel) return;
  if (!streams.length) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  panel.hidden = false;
  panel.innerHTML = streams
    .map((stream) => {
      const root = stream.root || "";
      const name = root.split("/").filter(Boolean).pop() || root;
      const total = stream.files_found || 0;
      const done = stream.files_processed || 0;
      let pct = total ? Math.min(100, Math.round((done / total) * 100)) : (stream.done ? 100 : 8);
      if (stream.done) pct = 100;
      const groups = stream.groups_found || 0;
      const state = stream.done ? "done" : (scanning ? "active" : "idle");
      const phase = stream.done ? "done" : (stream.phase || "");
      const detail = [phase, groups ? `${groups} group${groups === 1 ? "" : "s"}` : ""]
        .filter(Boolean)
        .join(" · ");
      return `
        <div class="stream-row ${state}" title="${escapeHtml(root)}">
          <div class="stream-head">
            <span class="stream-name">${escapeHtml(name)}</span>
            <span class="stream-detail">${escapeHtml(detail)}</span>
          </div>
          <div class="stream-bar"><div class="stream-fill" data-pct="${Math.max(pct, 4)}"></div></div>
        </div>`;
    })
    .join("");
  // The local CSP forbids style attributes, so widths are applied through CSSOM.
  panel.querySelectorAll(".stream-fill").forEach((fill) => {
    fill.style.width = `${fill.dataset.pct}%`;
  });
}

// —— Status / groups ——
function renderDiagnostics(summary) {
  const diagnostics = summary?.diagnostics;
  const panel = $("scanQuality");
  if (!diagnostics || !Object.keys(diagnostics.stages || {}).length) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const stages = Object.entries(diagnostics.stages);
  const failed = stages.reduce((total, [, stage]) => total + (stage.failed || 0), 0);
  const warnings = [...(summary.errors || [])];
  for (const [name, stage] of stages) {
    for (const warning of stage.warnings || []) warnings.push(`${name.replaceAll("_", " ")}: ${warning}`);
  }
  $("scanQualitySummary").textContent = `· ${formatDuration(diagnostics.total_duration_seconds)} · ${diagnostics.cache_hits || 0} cache hits`;
  $("scanQualityGrid").innerHTML = stages.map(([name, stage]) => {
    // Stage warnings are capped server-side; say so instead of implying the
    // shown list is complete.
    const capped = (stage.failed || 0) > (stage.warnings || []).length;
    return `
    <div class="quality-stage">
      <strong>${escapeHtml(name.replaceAll("_", " "))}</strong><span>${formatDuration(stage.duration_seconds)}</span>
      <small>${stage.attempted || 0} attempted · ${stage.succeeded || 0} succeeded · ${stage.failed || 0} failed · ${stage.skipped || 0} skipped ${escapeHtml(stage.unit || "files")}${capped ? " · first warnings shown" : ""}</small>
    </div>`;
  }).join("");
  const shown = warnings.slice(0, 20);
  const errorsTotal = Number(summary.errors_total) || (summary.errors || []).length;
  const hiddenErrors = Math.max(0, errorsTotal - (summary.errors || []).length);
  const hiddenWarnings = Math.max(0, warnings.length - shown.length);
  const hiddenNote = hiddenErrors + hiddenWarnings > 0
    ? `<li class="muted">+ ${hiddenErrors + hiddenWarnings} more warning${hiddenErrors + hiddenWarnings === 1 ? "" : "s"} not shown</li>`
    : "";
  $("scanQualityWarnings").innerHTML =
    shown.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("") + hiddenNote;
  const incomplete = failed > 0 || warnings.length > 0;
  $("scanQualityWarning").hidden = !incomplete;
  $("scanQualityWarning").textContent = incomplete
    ? `Analysis is incomplete: ${failed ? `${failed} item${failed === 1 ? "" : "s"} failed` : "scan warnings were reported"}. Review warnings before acting.`
    : "";
}

function prunedReasonSummary(metadata) {
  const reasons = metadata.pruned_reasons || {};
  const labels = metadata.pruned_reason_labels || {};
  return Object.entries(reasons)
    .map(([reason, count]) => `${count} ${labels[reason] || reason}`)
    .join(" · ");
}

function renderPrunedDetail(metadata) {
  const panel = $("sessionPruned");
  const samples = metadata?.pruned_samples || [];
  const total = Number(metadata?.pruned_files) || 0;
  panel.hidden = !total;
  if (!total) {
    $("sessionPrunedList").innerHTML = "";
    $("sessionPrunedMore").hidden = true;
    return;
  }
  const summary = prunedReasonSummary(metadata);
  $("sessionPrunedSummary").textContent = summary
    ? `What was dropped? (${summary})`
    : "What was dropped?";
  $("sessionPrunedList").innerHTML = samples
    .map(
      (sample) =>
        `<li><span class="pruned-path" title="${escapeHtml(sample.path)}">${escapeHtml(basename(sample.path))}</span><span class="muted small">${escapeHtml(sample.detail || sample.reason)}</span></li>`,
    )
    .join("");
  const hidden = total - samples.length;
  $("sessionPrunedMore").hidden = hidden <= 0;
  $("sessionPrunedMore").textContent = hidden > 0
    ? `…and ${hidden} more file${hidden === 1 ? "" : "s"} not listed.`
    : "";
}

// Identity of the banner the user dismissed; a changed session re-shows it.
function sessionBannerKey(metadata) {
  if (!metadata) return "";
  return [
    metadata.available,
    metadata.corrupt,
    metadata.saved_at,
    metadata.pruned_files,
    metadata.error,
  ].join("|");
}

function renderSession(metadata, resumed = false) {
  state.reviewSession = metadata || null;
  const available = !!metadata?.available;
  const corrupt = !!metadata?.corrupt;
  const dismissed = sessionBannerKey(metadata) === state.dismissedSessionKey
    && state.dismissedSessionKey !== "";
  $("sessionStatus").hidden = (!available && !corrupt) || dismissed;
  if (dismissed) return;
  $("sessionStatus").classList.toggle("warn", corrupt);
  if (!available && !corrupt) return;
  if (corrupt) {
    $("sessionFlag").textContent = "!";
    $("sessionStatusText").textContent =
      `The saved review could not be read (${metadata.error || "unreadable file"}). Scan again, or discard it to start clean.`;
    $("btnDiscardSession").textContent = "Discard unreadable review";
    renderPrunedDetail(null);
    return;
  }
  $("sessionFlag").textContent = "✔";
  $("btnDiscardSession").textContent = "Discard saved review";
  const when = metadata.saved_at ? new Date(metadata.saved_at).toLocaleString() : "an earlier session";
  const pruned = metadata.pruned_files ? ` · ${metadata.pruned_files} stale file${metadata.pruned_files === 1 ? "" : "s"} pruned` : "";
  $("sessionStatusText").textContent = `${resumed ? "Resumed" : "Saved"} review from ${when}${pruned}`;
  renderPrunedDetail(metadata);
}

$("btnDismissSession").addEventListener("click", () => {
  state.dismissedSessionKey = sessionBannerKey(state.reviewSession);
  $("sessionStatus").hidden = true;
});

async function refreshStatus(payload = null, { handleGroups = true } = {}) {
  // With no payload this is the polling path; SSE status events pass theirs
  // in and stream groups separately (group/reset events) instead of refetching.
  const s = payload || (await api("/api/status"));
  const statusEl = $("scanStatus");
  const wrap = $("progressWrap");
  const fill = $("progressFill");
  const msg = $("progressMsg");
  const top = $("topStats");
  const scanCompleted = state.scanning && !s.scanning && s.progress?.done;

  // A new scan ends in-app undo for files trashed from the earlier review;
  // say so once instead of letting the restore control silently vanish.
  if (s.scanning && !state.trashUndoClearedNotified && (s.trash_undo_cleared || 0) > 0) {
    state.trashUndoClearedNotified = true;
    const count = s.trash_undo_cleared;
    toast(
      `${count} file${count === 1 ? "" : "s"} trashed earlier can still be restored from the Trash — the new scan ends in-app undo for ${count === 1 ? "it" : "them"}`,
    );
  }
  if (!s.scanning && !(s.trash_undo_cleared > 0)) state.trashUndoClearedNotified = false;

  state.scanning = !!s.scanning;
  state.acting = !!s.acting;
  state.scanId = s.scan_id || state.scanId;
  renderSession(s.review_session, s.progress?.message === "Resumed saved review");
  document.querySelectorAll("#actionBar button, #actionBar input").forEach((element) => {
    element.disabled = state.scanning || state.acting;
  });

  // Gate dependency-bound scan options (OpenCV/Photon/ffmpeg) once known.
  if (s.capabilities && s.capabilities !== state.capabilities) {
    state.capabilities = s.capabilities;
    applyCapabilities(s.capabilities);
  }

  // A durable keep-decisions write failure must not pass silently: surface it
  // once per distinct error instead of swallowing it server-side.
  if (s.keep_decisions_error && s.keep_decisions_error !== state.keepDecisionsError) {
    toast(
      `Could not remember Keep decisions (${s.keep_decisions_error}) — these files may resurface in future scans`,
      "error",
    );
  }
  state.keepDecisionsError = s.keep_decisions_error || null;

  // Configure workers slider from server CPU info (once)
  if (s.system) {
    const cpu = Number(s.system.cpu_count) || 0;
    const auto = Number(s.system.auto_workers) || 0;
    const maxW = Number(s.system.max_workers) || Math.max(cpu, 16);
    if (cpu && cpu !== state.cpuCount) {
      state.cpuCount = cpu;
      state.autoWorkers = auto;
      workersEl.max = String(Math.max(8, Math.min(32, maxW)));
      updateWorkersUI();
    }
  }

  if (s.scanning) {
    wrap.hidden = false;
    statusEl.textContent = "Scanning…";
    statusEl.classList.remove("error");
    $("btnScan").disabled = true;
    $("btnCancelScan").hidden = false;
    $("btnScan").querySelector(".btn-label").textContent = "Scanning…";
    const p = s.progress || {};
    const total = p.files_found || 0;
    const done = p.files_processed || 0;
    const groupsSoFar = p.groups_found || 0;
    // Soft progress while hashing; bump near-complete as groups stream in
    let pct = total ? Math.min(95, Math.round((done / total) * 100)) : 12;
    if (groupsSoFar > 0) pct = Math.max(pct, Math.min(98, 20 + groupsSoFar * 3));
    const shownPct = p.done ? 100 : Math.max(pct, 5);
    fill.style.width = `${shownPct}%`;
    $("progressBar").setAttribute("aria-valuenow", String(shownPct));
    const baseMsg = p.message || p.phase || "";
    const parts = [
      total > 0 ? `${done}/${total}` : "",
      baseMsg,
      groupsSoFar > 1 || groupsSoFar === 1
        ? `${groupsSoFar} group${groupsSoFar === 1 ? "" : "s"} so far`
        : "",
      p.eta_seconds > 0 ? `about ${formatDuration(p.eta_seconds)} left` : "",
    ];
    msg.textContent = parts.filter(Boolean).join(" · ");
  } else {
    $("btnScan").disabled = false;
    $("btnCancelScan").hidden = true;
    $("btnScan").querySelector(".btn-label").textContent = "Scan";
    if (s.progress?.done) {
      fill.style.width = "100%";
      $("progressBar").setAttribute("aria-valuenow", "100");
      msg.textContent = s.progress.message || "Done";
      statusEl.textContent = s.error ? `Error` : "Ready";
      if (s.error) statusEl.classList.add("error");
      else statusEl.classList.remove("error");
    }
  }

  renderStreams(s.streams || [], !!s.scanning);

  if (s.summary) {
    renderDiagnostics(s.summary);
    const scanningNote = s.scanning ? " · live" : "";
    top.innerHTML = `
      <span class="stat-chip"><span class="dot"></span><strong>${s.summary.group_count}</strong> groups${scanningNote}</span>
      <span class="stat-chip">${s.summary.exact_groups} exact · ${s.summary.similar_groups} similar · ${s.summary.low_resolution_files || 0} low-res · ${s.summary.random_review_files || 0} random · ${s.summary.no_human_files || 0} non-human</span>
      <span class="stat-chip reclaim"><span class="dot"></span><strong>${s.summary.reclaimable_human}</strong> reclaimable</span>
      ${s.summary.errors?.length ? `<span class="stat-chip muted-chip">${s.summary.errors.length} warning${s.summary.errors.length === 1 ? "" : "s"}</span>` : ""}
    `;
    // Show results as soon as we have a result shell (even 0 groups) while scanning,
    // or whenever summary is present after scan.
    if (s.scanning || s.summary.group_count > 0) {
      $("emptyState").hidden = true;
      $("results").hidden = false;
      // Actions only when scan finished (groups still changing mid-scan)
      $("actionBar").hidden = !!s.scanning;
    }
    $("countAll").textContent = s.summary.group_count;
    $("countExact").textContent = s.summary.exact_groups;
    $("countSimilar").textContent = s.summary.similar_groups;
    $("countLowResolution").textContent = s.summary.low_resolution_files || 0;
    $("countRandomReview").textContent = s.summary.random_review_files || 0;
    $("countNoHumans").textContent = s.summary.no_human_files || 0;
    $("countFaces").textContent = s.summary.faces_files || 0;
  } else {
    $("scanQuality").hidden = true;
    top.innerHTML = s.error
      ? `<span class="stat-chip muted-chip">Error: ${escapeHtml(s.error)}</span>`
      : `<span class="stat-chip muted-chip">No scan yet</span>`;
    if (!s.scanning) {
      $("emptyState").hidden = false;
      $("results").hidden = true;
      $("actionBar").hidden = true;
    }
  }

  // Stream groups while scanning whenever the server version advances;
  // also load once when scan finishes. (SSE clients skip this — group and
  // reset events carry the changes.)
  const version = Number(s.groups_version);
  const versionChanged =
    Number.isFinite(version) && version !== state.groupsVersion;
  if (versionChanged) state.groupsVersion = version;
  if (handleGroups && s.has_result && (versionChanged || (!s.scanning && s.progress?.done))) {
    await loadGroups({ preserveSelection: s.scanning });
  }
  if (!s.scanning && state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  if (scanCompleted) {
    if (s.error) toast(s.error, "error");
    else toast(s.progress.message || "Scan complete", "ok");
  }
  // A resumed session whose files all pruned away loads as a baffling empty
  // review; explain it once and point at a fresh scan.
  if (
    !state.emptyResumeNotified
    && !s.scanning
    && s.review_session?.available
    && s.summary
    && Number(s.summary.group_count) === 0
    && s.progress?.message === "Resumed saved review"
  ) {
    state.emptyResumeNotified = true;
    toast(
      "The saved review's files all changed or moved — nothing is left to review. Scan again for a fresh look.",
    );
  }
  return s;
}

// —— Server-sent events (with a 350 ms polling fallback) ——
function startStatusPolling() {
  if (state.pollTimer) return;
  state.pollFailures = 0;
  state.pollTimer = setInterval(async () => {
    try {
      await refreshStatus();
      state.pollFailures = 0;
    } catch {
      state.pollFailures += 1;
      if (state.pollFailures >= 5) {
        // The server is gone; polling on just looks like a hung scan.
        stopStatusPolling();
        toast(
          "Lost connection to the Dedupe server — restart it and reload this page.",
          "error",
          { duration: 3600000 },
        );
      }
    }
  }, 350);
}

function stopStatusPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function openEventStream() {
  if (typeof EventSource !== "function") return; // polling covers old browsers
  const source = new EventSource("/api/events");
  state.eventSource = source;
  source.onopen = () => {
    state.eventFailures = 0;
    // The stream is healthy again; the fallback can stand down.
    state.pollFailures = 0;
    stopStatusPolling();
  };
  source.addEventListener("status", (event) => {
    state.eventFailures = 0;
    stopStatusPolling();
    let s;
    try {
      s = JSON.parse(event.data);
    } catch {
      return;
    }
    refreshStatus(s, { handleGroups: false })
      .catch((e) => toast(e.message || String(e), "error"));
  });
  source.addEventListener("group", (event) => {
    try {
      addStreamedGroup(JSON.parse(event.data));
    } catch {
      /* malformed event; the next reset resyncs */
    }
  });
  source.addEventListener("reset", () => {
    state.groupsVersion = -1;
    loadGroups().catch((e) => toast(e.message || String(e), "error"));
  });
  source.onerror = () => {
    // EventSource retries on its own; poll as a fallback so progress keeps
    // moving (and the dead-server toast still fires) while it is down.
    state.eventFailures += 1;
    if (state.eventFailures >= 3) startStatusPolling();
  };
}

export { refreshStatus, renderSession, startStatusPolling, openEventStream };
