(() => {
  const $ = (id) => document.getElementById(id);

  const RECENT_KEY = "dedupe.recentPaths";
  const QUAR_KEY = "dedupe.quarantineDir";
  const WORKERS_KEY = "dedupe.workers";
  const SETTINGS_KEY = "dedupe.scanSettings.v1";
  const PLAYBACK_RATE_KEY = "dedupe.videoPlaybackRate";
  const MEMBER_PAGE_SIZE = 50;
  const GROUP_FETCH_PAGE = 250;
  const GROUP_RENDER_CHUNK = 60;
  const CSRF_TOKEN =
    document.querySelector('meta[name="dedupe-token"]')?.getAttribute("content") || "";

  const state = {
    kind: "all",
    groups: [],
    allGroups: [],
    currentId: null,
    pollTimer: null,
    memberFocus: 0,
    memberPage: 0,
    lightboxItems: [],
    lightboxIndex: 0,
    scanning: false,
    acting: false,
    cpuCount: 0,
    autoWorkers: 0,
    groupsVersion: -1, // tracks streaming updates mid-scan
    scanId: null,
    reviewSession: null,
    groupListLimit: GROUP_RENDER_CHUNK, // how many sidebar rows are in the DOM
    groupsLoadToken: 0,
    groupsTotal: 0,
    reviewingCandidate: false,
  };

  // —— Batched rendering ——
  const pendingRender = { groupList: false, selection: false, members: null };
  let renderHandle = null;

  function flushRenders() {
    renderHandle = null;
    const members = pendingRender.members;
    const list = pendingRender.groupList;
    const selection = pendingRender.selection;
    pendingRender.members = null;
    pendingRender.groupList = false;
    pendingRender.selection = false;
    if (members) renderMembers(members);
    if (list) renderGroupList();
    if (selection) updateSelectionSummary();
  }

  function scheduleRender({ groupList = false, selection = false, members = null } = {}) {
    if (groupList) pendingRender.groupList = true;
    if (selection) pendingRender.selection = true;
    if (members) pendingRender.members = members;
    if (renderHandle !== null) return;
    renderHandle =
      typeof requestAnimationFrame === "function"
        ? requestAnimationFrame(flushRenders)
        : setTimeout(flushRenders, 0);
  }

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

  // —— Toast ——
  function toast(msg, kind = "") {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast" + (kind ? ` ${kind}` : "");
    el.hidden = false;
    // force reflow for transition
    void el.offsetWidth;
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => {
        el.hidden = true;
      }, 220);
    }, 3400);
  }

  function formatBytes(n) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = Number(n) || 0;
    for (const u of units) {
      if (size < 1024 || u === units[units.length - 1]) {
        return u === "B" ? `${size} ${u}` : `${size.toFixed(1)} ${u}`;
      }
      size /= 1024;
    }
    return `${n} B`;
  }

  function formatMtime(seconds) {
    if (seconds == null || Number.isNaN(Number(seconds))) return "—";
    const date = new Date(Number(seconds) * 1000);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function syncMemberPagination(pageCount, summaryText) {
    const bars = [
      $("memberPagination"),
      $("memberPaginationBottom"),
    ].filter(Boolean);
    for (const bar of bars) {
      bar.hidden = pageCount <= 1;
      const prev = bar.querySelector(".member-prev");
      const next = bar.querySelector(".member-next");
      const summary = bar.querySelector(".member-page-summary");
      if (prev) prev.disabled = state.memberPage === 0;
      if (next) next.disabled = state.memberPage >= pageCount - 1;
      if (summary) summary.textContent = summaryText;
    }
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60);
    const remainder = total % 60;
    return `${minutes}m ${remainder}s`;
  }

  function basename(p) {
    return (p || "").split(/[/\\]/).pop() || p;
  }

  function setPreviewAspectRatio(preview, width, height) {
    const displayWidth = Number(width);
    const displayHeight = Number(height);
    if (!preview || !Number.isFinite(displayWidth) || !Number.isFinite(displayHeight)
      || displayWidth <= 0 || displayHeight <= 0) return;
    const aspectRatio = displayWidth / displayHeight;
    preview.style.setProperty("--preview-aspect-ratio", String(aspectRatio));
    preview.style.setProperty("--preview-max-width", `${58 * aspectRatio}vh`);
  }

  function isDecisionReview(g) {
    return g?.kind === "low_resolution" || g?.kind === "random_review";
  }

  function isIndependentReview(g) {
    return g?.policy === "independent_candidates";
  }

  function isPagedIndependentReview(g) {
    return g?.kind === "no_humans" || g?.kind === "faces";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function hexDistance(left, right) {
    if (!left || !right || left.length !== right.length || !/^[0-9a-f]+$/i.test(left + right)) return null;
    let distance = 0;
    for (let i = 0; i < left.length; i += 1) {
      let bits = parseInt(left[i], 16) ^ parseInt(right[i], 16);
      while (bits) { distance += bits & 1; bits >>>= 1; }
    }
    return distance;
  }

  function similarityExplanation(member, keeper) {
    const parts = [];
    const phash = hexDistance(member.phash, keeper.phash);
    const dhash = hexDistance(member.dhash, keeper.dhash);
    if (phash !== null) parts.push(`pHash distance ${phash}`);
    if (dhash !== null) parts.push(`dHash distance ${dhash}`);
    if (member.tile_phashes && keeper.tile_phashes) parts.push("tile fingerprints compared");
    if (member.video_fingerprint && keeper.video_fingerprint) parts.push("video fingerprints compared");
    return parts.join(" · ");
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: {
        "Content-Type": "application/json",
        "X-Dedupe-Token": CSRF_TOKEN,
        ...(opts.headers || {}),
      },
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const error = new Error(data.error || res.statusText);
      error.status = res.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  const settingIds = [
    "optExact",
    "optSimilar",
    "optNoHumans",
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

  // restore quarantine
  try {
    const q = localStorage.getItem(QUAR_KEY);
    if (q) $("quarantineDir").value = q;
  } catch {
    /* ignore */
  }
  $("quarantineDir").addEventListener("change", () => {
    try {
      localStorage.setItem(QUAR_KEY, $("quarantineDir").value.trim());
    } catch {
      /* ignore */
    }
  });

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

  // —— Confirm modal ——
  function formatCountdown(seconds) {
    const total = Math.max(0, Math.round(seconds));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  /**
   * Resolves true (confirmed), false (cancelled), or "expired" when a preview
   * token ran out while the sheet was open. Callers must re-preview on "expired";
   * an execute is never attempted on a lapsed token.
   */
  function confirmModal({
    title,
    body,
    confirmLabel = "Confirm",
    danger = true,
    validitySeconds = null,
  }) {
    return new Promise((resolve) => {
      $("modalTitle").textContent = title;
      $("modalBody").innerHTML = body;
      const btn = $("modalConfirm");
      btn.textContent = confirmLabel;
      btn.className = danger ? "btn danger" : "btn primary";
      const validity = $("modalValidity");
      let ticker = null;
      $("modalBackdrop").hidden = false;

      const cleanup = (ok) => {
        if (ticker !== null) clearInterval(ticker);
        ticker = null;
        $("modalBackdrop").hidden = true;
        btn.removeEventListener("click", onOk);
        $("modalCancel").removeEventListener("click", onCancel);
        document.removeEventListener("keydown", onKey);
        resolve(ok);
      };
      const onOk = () => cleanup(true);
      const onCancel = () => cleanup(false);
      const onKey = (e) => {
        if (e.key === "Escape") cleanup(false);
        if (e.key === "Enter") cleanup(true);
      };
      btn.addEventListener("click", onOk);
      $("modalCancel").addEventListener("click", onCancel);
      document.addEventListener("keydown", onKey);

      if (validitySeconds > 0) {
        const expiresAt = Date.now() + validitySeconds * 1000;
        const tick = () => {
          const left = (expiresAt - Date.now()) / 1000;
          if (left <= 0) {
            cleanup("expired");
            return;
          }
          validity.textContent = `Verified against the current selection · preview valid for ${formatCountdown(left)}`;
          validity.classList.toggle("expiring", left <= 60);
        };
        validity.hidden = false;
        tick();
        ticker = setInterval(tick, 1000);
      } else {
        validity.hidden = true;
        validity.textContent = "";
        validity.classList.remove("expiring");
      }
    });
  }

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
    $("scanQualityGrid").innerHTML = stages.map(([name, stage]) => `
      <div class="quality-stage">
        <strong>${escapeHtml(name.replaceAll("_", " "))}</strong><span>${formatDuration(stage.duration_seconds)}</span>
        <small>${stage.attempted || 0} attempted · ${stage.succeeded || 0} succeeded · ${stage.failed || 0} failed · ${stage.skipped || 0} skipped ${escapeHtml(stage.unit || "files")}</small>
      </div>`).join("");
    $("scanQualityWarnings").innerHTML = warnings.slice(0, 20).map((warning) => `<li>${escapeHtml(warning)}</li>`).join("");
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

  function renderSession(metadata, resumed = false) {
    state.reviewSession = metadata || null;
    const available = !!metadata?.available;
    const corrupt = !!metadata?.corrupt;
    $("sessionStatus").hidden = !available && !corrupt;
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

  async function refreshStatus() {
    const s = await api("/api/status");
    const statusEl = $("scanStatus");
    const wrap = $("progressWrap");
    const fill = $("progressFill");
    const msg = $("progressMsg");
    const top = $("topStats");
    const scanCompleted = state.scanning && !s.scanning && s.progress?.done;

    state.scanning = !!s.scanning;
    state.acting = !!s.acting;
    state.scanId = s.scan_id || state.scanId;
    renderSession(s.review_session, s.progress?.message === "Resumed saved review");
    document.querySelectorAll("#actionBar button, #actionBar input").forEach((element) => {
      element.disabled = state.scanning || state.acting;
    });

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
      fill.style.width = `${p.done ? 100 : Math.max(pct, 5)}%`;
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
      if (s.scanning || s.summary.group_count > 0 || !s.scanning) {
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
    // also load once when scan finishes.
    const version = Number(s.groups_version);
    const versionChanged =
      Number.isFinite(version) && version !== state.groupsVersion;
    if (s.has_result && (versionChanged || (!s.scanning && s.progress?.done))) {
      if (versionChanged) state.groupsVersion = version;
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
    return s;
  }

  async function fetchAllGroups(token) {
    // Pages are only consistent within one groups_version; mid-scan the server
    // re-sorts as groups stream in, so restart when the version moves under us.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const collected = [];
      let offset = 0;
      let total = 0;
      let version = null;
      let stale = false;
      for (;;) {
        const page = await api(`/api/groups?kind=all&offset=${offset}&limit=${GROUP_FETCH_PAGE}`);
        if (token !== state.groupsLoadToken) return null;
        if (version === null) version = page.groups_version;
        else if (page.groups_version !== version) {
          stale = true;
          break;
        }
        const batch = page.groups || [];
        collected.push(...batch);
        total = Number.isFinite(Number(page.total)) ? Number(page.total) : collected.length;
        offset += batch.length;
        if (collected.length === batch.length && collected.length < total) {
          // Paint the first page immediately so streamed groups appear without delay.
          state.allGroups = collected;
          renderGroupList();
        }
        if (!batch.length || collected.length >= total) break;
      }
      if (!stale) {
        state.groupsTotal = total;
        return collected;
      }
    }
    const fallback = await api(`/api/groups?kind=all`);
    if (token !== state.groupsLoadToken) return null;
    state.groupsTotal = Number(fallback.total) || (fallback.groups || []).length;
    return fallback.groups || [];
  }

  async function loadGroups({ preserveSelection = false } = {}) {
    const token = ++state.groupsLoadToken;
    const all = await fetchAllGroups(token);
    if (all === null) return;
    state.allGroups = all;
    applyResultControls();

    const exact = state.allGroups.filter((g) => g.kind === "exact").length;
    const similar = state.allGroups.filter((g) => g.kind === "similar").length;
    const lowResolution = state.allGroups
      .filter((g) => g.kind === "low_resolution")
      .reduce((count, g) => count + (g.member_count || 0), 0);
    const randomReview = state.allGroups
      .filter((g) => g.kind === "random_review")
      .reduce((count, g) => count + (g.member_count || 0), 0);
    const noHumans = state.allGroups
      .filter((g) => g.kind === "no_humans")
      .reduce((count, g) => count + (g.member_count || 0), 0);
    const faces = state.allGroups
      .filter((g) => g.kind === "faces")
      .reduce((count, g) => count + (g.member_count || 0), 0);
    $("countAll").textContent = state.allGroups.length;
    $("countExact").textContent = exact;
    $("countSimilar").textContent = similar;
    $("countLowResolution").textContent = lowResolution;
    $("countRandomReview").textContent = randomReview;
    $("countNoHumans").textContent = noHumans;
    $("countFaces").textContent = faces;

    if (state.groups.length) {
      $("emptyState").hidden = true;
      $("results").hidden = false;
    }

    scheduleRender({ groupList: true, selection: true });
    if (state.currentId) {
      const still = state.groups.find((g) => g.id === state.currentId);
      if (still) {
        // Mid-scan: keep list fresh but don't thrash an open detail view
        // (member set for a group is fixed once published).
        if (!preserveSelection) {
          await selectGroup(state.currentId, { silent: true });
        }
      } else {
        state.currentId = null;
        $("detailBody").hidden = true;
        $("detailEmpty").hidden = false;
      }
    } else if (state.groups.length && !$("results").hidden) {
      // Auto-select first when nothing selected (including first group mid-scan)
      if (!$("detailEmpty").hidden) {
        await selectGroup(state.groups[0].id, { silent: true });
      }
    }
  }

  function groupSelectedCount(g) {
    return (g.selected_for_removal || []).length;
  }

  function groupComplete(g) {
    if (isIndependentReview(g)) return (g.reviewed_paths || []).length >= (g.member_count || 0);
    return (g.selected_for_removal || []).length >= Math.max(0, (g.member_count || 0) - 1);
  }

  function groupNeedsAttention(g) {
    return (g.members || []).some((member) => member.error) || (g.deleted_paths || []).length > 0 || !groupComplete(g);
  }

  function numericFilter(id) {
    const raw = ($(id).value || "").trim();
    if (!raw) return null;
    const value = Number(raw);
    return Number.isFinite(value) && value >= 0 ? value : null;
  }

  function pathMatcher() {
    const raw = ($("filterPathPattern").value || "").trim();
    if (!raw) return null;
    if (!/[*?]/.test(raw)) {
      const needle = raw.toLowerCase();
      return (path) => path.toLowerCase().includes(needle);
    }
    const expression = raw
      .replace(/[.+^${}()|[\]\\]/g, "\\$&")
      .replace(/\*/g, ".*")
      .replace(/\?/g, ".");
    let pattern;
    try {
      pattern = new RegExp(expression, "i");
    } catch {
      return null;
    }
    return (path) => pattern.test(path);
  }

  function advancedFilters() {
    const minMb = numericFilter("filterMinMb");
    const maxMb = numericFilter("filterMaxMb");
    const filters = {
      minSize: minMb == null ? null : minMb * 1024 * 1024,
      maxSize: maxMb == null ? null : maxMb * 1024 * 1024,
      minWidth: numericFilter("filterMinWidth"),
      minHeight: numericFilter("filterMinHeight"),
      matchPath: pathMatcher(),
      faces: $("filterFaces").value === "any" ? null : $("filterFaces").value,
    };
    filters.active =
      filters.minSize != null ||
      filters.maxSize != null ||
      filters.minWidth != null ||
      filters.minHeight != null ||
      !!filters.matchPath ||
      filters.faces != null;
    return filters;
  }

  function memberMatchesFilters(member, filters) {
    if (filters.minSize != null && (member.size || 0) < filters.minSize) return false;
    if (filters.maxSize != null && (member.size || 0) > filters.maxSize) return false;
    if (filters.minWidth != null && (member.width || 0) < filters.minWidth) return false;
    if (filters.minHeight != null && (member.height || 0) < filters.minHeight) return false;
    if (filters.matchPath && !filters.matchPath(member.path)) return false;
    if (filters.faces) {
      // Files without a trusted face count never match: "No faces" must mean
      // the counter actually ran and found zero, not "never analyzed".
      if (member.face_count == null) return false;
      if (filters.faces === "has" && member.face_count < 1) return false;
      if (filters.faces === "none" && member.face_count !== 0) return false;
    }
    return true;
  }

  function applyResultControls() {
    const query = ($("resultSearch").value || "").trim().toLowerCase();
    const selection = $("selectionFilter").value;
    const filters = advancedFilters();
    $("advancedFilterFlag").hidden = !filters.active;
    let groups = state.allGroups.filter((g) => state.kind === "all" || g.kind === state.kind);
    groups = groups.filter((g) => {
      const selected = groupSelectedCount(g) > 0;
      if (query && !(g.members || []).some((member) => member.path.toLowerCase().includes(query))) return false;
      if (selection === "selected" && !selected) return false;
      if (selection === "unselected" && selected) return false;
      if ($("issuesOnly").checked && !groupNeedsAttention(g)) return false;
      if ($("hideCompleted").checked && groupComplete(g)) return false;
      // Advanced filters keep a group when any one of its files qualifies.
      if (filters.active && !(g.members || []).some((member) => memberMatchesFilters(member, filters))) {
        return false;
      }
      return true;
    });
    const sort = $("resultSort").value;
    groups.sort((a, b) => {
      if (sort === "size") return (b.member_count || 0) - (a.member_count || 0);
      if (sort === "date") return Math.max(...(b.members || []).map((m) => m.mtime || 0), 0) - Math.max(...(a.members || []).map((m) => m.mtime || 0), 0);
      if (sort === "media") return String(a.media_type).localeCompare(String(b.media_type));
      return (b.reclaimable_bytes || 0) - (a.reclaimable_bytes || 0);
    });
    state.groups = groups;
    $("filteredCount").textContent = `${groups.length} of ${state.allGroups.length} groups shown`;
  }

  function groupItemHtml(g) {
    const active = g.id === state.currentId ? "active" : "";
    const sel = groupSelectedCount(g);
    const badgeLabel = {
      no_humans: "non-human",
      low_resolution: "low-res",
      random_review: "random",
      faces: "faces",
    }[g.kind] || g.kind;
    const reviewed = (g.reviewed_paths || []).length;
    const groupSummary = isIndependentReview(g)
      ? `${reviewed}/${g.member_count} reviewed${sel ? ` · ${sel} delete` : ""}`
      : `${formatBytes(g.reclaimable_bytes)} reclaimable`;
    // Status is never colour-only: the glyph and its label carry the same meaning.
    const attention = groupNeedsAttention(g);
    const stateLabel = attention ? "Needs review" : "Reviewed";
    const stateGlyph = attention ? "●" : "✔";
    return `
          <button class="group-item ${active} ${attention ? "attention" : "done"}" data-id="${g.id}" type="button" role="option" aria-selected="${active ? "true" : "false"}">
            <div class="g-top">
              <span>${g.member_count} files${isIndependentReview(g) ? "" : ` · ${escapeHtml(g.media_type)}`}</span>
              <span class="badge ${g.kind}">${badgeLabel}</span>
            </div>
            <div class="g-state"><span class="g-state-glyph" aria-hidden="true">${stateGlyph}</span>${stateLabel}</div>
            <div class="g-sub">
              <span>${groupSummary}</span>
              ${sel ? `<span class="sel-mark">${sel} selected</span>` : ""}
            </div>
          </button>
        `;
  }

  function groupMoreHtml() {
    const remaining = Math.max(0, state.groups.length - state.groupListLimit);
    if (!remaining) return "";
    return `
          <div class="group-more-wrap">
            <button class="btn ghost group-more" type="button">Show ${Math.min(remaining, GROUP_RENDER_CHUNK)} more (${remaining} hidden)</button>
          </div>`;
  }

  function wireGroupList() {
    const list = $("groupList");
    if (list.dataset.wired === "1") return list;
    list.dataset.wired = "1";
    list.addEventListener("click", (event) => {
      if (event.target.closest(".group-more")) {
        growGroupList();
        return;
      }
      const btn = event.target.closest(".group-item[data-id]");
      if (btn) selectGroup(btn.dataset.id);
    });
    list.addEventListener("scroll", () => {
      if (list.scrollTop + list.clientHeight >= list.scrollHeight - 240) growGroupList();
    });
    return list;
  }

  function resetGroupListWindow() {
    state.groupListLimit = GROUP_RENDER_CHUNK;
  }

  function growGroupList() {
    if (state.groupListLimit >= state.groups.length) return;
    const list = wireGroupList();
    const from = state.groupListLimit;
    const to = Math.min(state.groups.length, from + GROUP_RENDER_CHUNK);
    state.groupListLimit = to;
    const html = state.groups.slice(from, to).map(groupItemHtml).join("");
    const more = list.querySelector(".group-more-wrap");
    if (more) more.insertAdjacentHTML("beforebegin", html);
    else list.insertAdjacentHTML("beforeend", html);
    if (more) more.outerHTML = groupMoreHtml();
  }

  function renderGroupList() {
    applyResultControls();
    const list = wireGroupList();
    if (!state.groups.length) {
      list.innerHTML = `<div class="group-empty">No groups in this filter.</div>`;
      return;
    }
    // Only the visited window lives in the DOM; the rest streams in on scroll.
    state.groupListLimit = Math.max(
      GROUP_RENDER_CHUNK,
      Math.min(state.groupListLimit, state.groups.length),
    );
    const scrollTop = list.scrollTop;
    list.innerHTML =
      state.groups.slice(0, state.groupListLimit).map(groupItemHtml).join("") + groupMoreHtml();
    list.scrollTop = Math.min(scrollTop, Math.max(0, list.scrollHeight - list.clientHeight));
  }

  function updateGroupListItem(g) {
    const node = $("groupList").querySelector(`.group-item[data-id="${g.id}"]`);
    if (!node) return false;
    const holder = document.createElement("div");
    holder.innerHTML = groupItemHtml(g).trim();
    const fresh = holder.firstElementChild;
    if (!fresh) return false;
    node.className = fresh.className;
    node.setAttribute("aria-selected", fresh.getAttribute("aria-selected"));
    node.innerHTML = fresh.innerHTML;
    return true;
  }

  function selectionFiltersActive() {
    return (
      $("selectionFilter").value !== "all" ||
      $("issuesOnly").checked ||
      $("hideCompleted").checked
    );
  }

  function markGroupListActive(id) {
    const list = $("groupList");
    list.querySelectorAll(".group-item.active").forEach((node) => {
      node.classList.remove("active");
      node.setAttribute("aria-selected", "false");
    });
    const node = list.querySelector(`.group-item[data-id="${id}"]`);
    if (node) {
      node.classList.add("active");
      node.setAttribute("aria-selected", "true");
    }
    return node;
  }

  function ensureGroupVisible(id) {
    const index = state.groups.findIndex((g) => g.id === id);
    if (index < 0) return;
    if (index < state.groupListLimit) return;
    state.groupListLimit = Math.min(
      state.groups.length,
      Math.ceil((index + 1) / GROUP_RENDER_CHUNK) * GROUP_RENDER_CHUNK,
    );
    renderGroupList();
  }

  function updateDetailMeta(g) {
    if (g.kind === "no_humans") {
      const reviewed = new Set(g.reviewed_paths || []);
      const selected = new Set(g.selected_for_removal || []);
      $("detailMeta").textContent =
        `${reviewed.size} of ${g.member_count} reviewed · ${selected.size} selected for removal · detector output is not a guarantee`;
      return;
    }
    if (g.kind === "faces") {
      const reviewed = new Set(g.reviewed_paths || []);
      const selected = new Set(g.selected_for_removal || []);
      $("detailMeta").textContent =
        `${reviewed.size} of ${g.member_count} reviewed · ${selected.size} selected for removal · face counts are heuristic, not a guarantee`;
      return;
    }
    if (isDecisionReview(g)) {
      const reviewed = new Set(g.reviewed_paths || []);
      const selected = new Set(g.selected_for_removal || []);
      const remaining = Math.max(0, g.member_count - reviewed.size);
      $("detailMeta").textContent =
        `${reviewed.size} reviewed · ${selected.size} marked Delete · ${remaining} remaining · decisions are staged until you confirm a file action`;
      return;
    }

    const keeper = (g.members || []).find((member) => member.path === g.suggested_keep);
    const keeperWhy = keeper
      ? ` Suggested keeper: ${basename(keeper.path)} (${keeper.width && keeper.height ? `${keeper.width}×${keeper.height}, ` : ""}${formatBytes(keeper.size)}), ranked by resolution, size, date, and path.`
      : "";
    $("detailMeta").textContent =
      `${formatBytes(g.reclaimable_bytes)} reclaimable · every member was directly verified against the suggested keeper.${keeperWhy}`;
  }

  async function selectGroup(id, { silent = false } = {}) {
    state.currentId = id;
    state.memberFocus = 0;
    state.memberPage = 0;
    ensureGroupVisible(id);
    markGroupListActive(id);
    const g = await api(`/api/groups/${id}`);
    if (isDecisionReview(g)) {
      const reviewed = new Set(g.reviewed_paths || []);
      const firstUnreviewed = (g.members || []).findIndex((member) => !reviewed.has(member.path));
      state.memberFocus = firstUnreviewed >= 0 ? firstUnreviewed : 0;
    }
    const idx = state.groups.findIndex((group) => group.id === g.id);
    if (idx >= 0) state.groups[idx] = g;
    const allIdx = state.allGroups.findIndex((group) => group.id === g.id);
    if (allIdx >= 0) state.allGroups[allIdx] = g;
    updateGroupListItem(g);
    markGroupListActive(id);
    scheduleRender({ selection: true });
    $("detailEmpty").hidden = true;
    $("detailBody").hidden = false;
    const kindLabel = {
      no_humans: "Non-Human · no person detected",
      low_resolution: "Low resolution · under 1 megapixel",
      random_review: "Random review · fresh sample",
      faces: "Faces · OpenCV face counts",
    }[g.kind] || g.kind;
    $("detailTitle").textContent = isIndependentReview(g)
      ? `${kindLabel} · ${g.member_count} files`
      : `${kindLabel} · ${g.media_type} · ${g.member_count} files`;
    const deletedPaths = new Set(g.deleted_paths || []);
    $("btnMarkRemainingHuman").hidden =
      g.kind !== "no_humans" || !(g.members || []).some((member) => !deletedPaths.has(member.path));
    $("btnMarkDistinct").hidden = g.kind !== "similar";
    $("nonHumanBanner").hidden = g.kind !== "no_humans";
    $("candidateReviewBanner").hidden = !isDecisionReview(g);
    if (isDecisionReview(g)) {
      $("candidateReviewTitle").textContent = g.kind === "low_resolution"
        ? "Low-resolution deletion suggestions"
        : `${g.member_count}-file library check-in`;
      $("candidateReviewDescription").textContent = g.kind === "low_resolution"
        ? "These files are below 1 megapixel. Decide one at a time; nothing moves until final confirmation."
        : "A fresh random sample from this scan. Use the arrow keys to decide quickly.";
    }
    document.querySelector(".selection-toolbar").hidden = isIndependentReview(g);
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
    // keep list item in view
    const active = document.querySelector(`.group-item[data-id="${id}"]`);
    if (active && !silent) active.scrollIntoView({ block: "nearest" });
  }

  function renderMembers(g) {
    const box = $("members");
    const selected = new Set(g.selected_for_removal || []);
    const reviewedPaths = new Set(g.reviewed_paths || []);
    const deletedPaths = new Set(g.deleted_paths || []);
    const allMembers = g.members || [];
    const decisionReview = isDecisionReview(g);
    const pageCount = decisionReview
      ? Math.max(1, allMembers.length)
      : isPagedIndependentReview(g)
        ? Math.max(1, Math.ceil(allMembers.length / MEMBER_PAGE_SIZE))
        : 1;
    state.memberPage = Math.max(0, Math.min(pageCount - 1, state.memberPage));
    if (decisionReview) {
      state.memberFocus = Math.max(0, Math.min(allMembers.length - 1, state.memberFocus));
      state.memberPage = state.memberFocus;
    }
    const pageStart = decisionReview ? state.memberFocus : state.memberPage * MEMBER_PAGE_SIZE;
    const members = decisionReview
      ? allMembers.slice(state.memberFocus, state.memberFocus + 1)
      : isPagedIndependentReview(g)
      ? allMembers.slice(pageStart, pageStart + MEMBER_PAGE_SIZE)
      : allMembers;
    const summaryText = allMembers.length
      ? decisionReview
        ? `${pageStart + 1} of ${allMembers.length}`
        : `${pageStart + 1}–${Math.min(pageStart + members.length, allMembers.length)} of ${allMembers.length}`
      : "0 results";
    syncMemberPagination(pageCount, summaryText);
    state.lightboxItems = members
      .filter((member) => !deletedPaths.has(member.path))
      .map((member) => ({ path: member.path, mediaType: member.media_type, keeper: g.suggested_keep, kind: g.kind }));
    updateDetailMeta(g);
    const reviewedCount = allMembers.filter((member) => reviewedPaths.has(member.path)).length;
    $("groupSelectionSummary").textContent = isIndependentReview(g)
      ? `${selected.size} selected · ${reviewedCount} of ${allMembers.length} reviewed`
      : `${selected.size} of ${allMembers.length} selected for removal`;

    box.innerHTML = members
      .map((m, i) => {
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
        const thumb = `/api/thumbnail?path=${encodeURIComponent(m.path)}`;
        const memberIndex = decisionReview ? state.memberFocus : i;
        const focused = decisionReview || i === state.memberFocus ? "focused" : "";
        const lightboxIndex = state.lightboxItems.findIndex((item) => item.path === m.path);
        const fileName = basename(m.path);
        const badge = isSel
          ? `<span class="thumb-badge remove">Remove</span>`
          : isKeep
            ? `<span class="thumb-badge keep">Keep</span>`
            : "";
        const keeper = allMembers.find((candidate) => candidate.path === g.suggested_keep);
        const distanceParts = g.kind === "similar" && keeper ? similarityExplanation(m, keeper) : "";
        const evidence = g.kind === "exact"
          ? "Byte-identical SHA-256 match"
          : g.kind === "similar"
            ? `Perceptual match to suggested keeper${distanceParts ? ` · ${distanceParts}` : ""} (explanation only, not a probability)`
            : g.kind === "low_resolution"
              ? `${dims} · ${((m.width || 0) * (m.height || 0) / 1_000_000).toFixed(2)} megapixels · below the 1 MP review threshold`
              : g.kind === "random_review"
                ? "Randomly selected from this scan for a quick keep-or-delete check"
                : g.kind === "faces"
                  ? `OpenCV face detection found ${m.face_count} face${m.face_count === 1 ? "" : "s"} (heuristic, not a guarantee)`
                  : `OpenCV person detection analyzed ${m.human_frames_analyzed || 0} frame(s); no person detected — likely non-human`;
        const selectionTitle = isSel
          ? (isPagedIndependentReview(g) ? "Reviewed · selected" : "Selected for removal")
          : (isPagedIndependentReview(g) && reviewed ? "Reviewed · not selected" : "Not selected");
        const selectionHint = isSel
          ? "Click to keep this file"
          : (isPagedIndependentReview(g) ? "Click to review and remove" : "Click to remove this file");
        const mediaPreview = m.media_type === "video"
          ? `<video class="hover-video" poster="${thumb}" data-src="/api/media?path=${encodeURIComponent(m.path)}" muted loop playsinline preload="none"></video>`
          : `<img class="thumb-image ${m.media_type === "gif" ? "hover-gif" : ""}" src="${thumb}" ${m.media_type === "gif" ? `data-thumbnail="${thumb}" data-src="/api/media?path=${encodeURIComponent(m.path)}"` : ""} alt="Preview of ${escapeHtml(fileName)}" loading="lazy" />`;
        const preview = deleted
          ? `<div class="thumb-wrap deleted-preview"${previewDimensions}><div class="thumb-fallback">Moved to Trash — undo available</div></div>`
          : `<button class="thumb-wrap" data-path="${escapeHtml(m.path)}" data-index="${lightboxIndex}"${previewDimensions} type="button" aria-label="Open preview for ${escapeHtml(fileName)}">
              ${badge}
              ${mediaPreview}
              ${["video", "gif"].includes(m.media_type) ? '<span class="video-preview-badge" aria-hidden="true">▶ Hover to play</span>' : ""}
            </button>`;
        const actions = decisionReview
          ? `<div class="candidate-actions" role="group" aria-label="Keep or delete ${escapeHtml(fileName)}">
                <button class="candidate-decision candidate-delete" data-path="${escapeHtml(m.path)}" type="button"><kbd>←</kbd><span><strong>Delete</strong><small>Stage for removal</small></span></button>
                <button class="candidate-decision candidate-keep" data-path="${escapeHtml(m.path)}" type="button"><span><strong>Keep</strong><small>Leave untouched</small></span><kbd>→</kbd></button>
              </div>`
          : isPagedIndependentReview(g)
          ? `<button class="btn ${deleted ? "ghost undo-delete" : "danger delete-candidate"}" data-path="${escapeHtml(m.path)}" type="button">${deleted ? "Undo" : "Delete"}</button>`
          : `<label class="selection-control">
                  <input type="checkbox" class="sel-cb" data-path="${escapeHtml(m.path)}" ${isSel ? "checked" : ""} />
                  <span class="selection-copy">
                    <strong>${selectionTitle}</strong>
                    <small>${selectionHint}</small>
                  </span>
                </label>
                <button class="linkish reveal" data-path="${escapeHtml(m.path)}" type="button">Reveal</button>`;
        return `
          <article class="card ${decisionReview ? "decision-card" : ""} ${isKeep ? "keep" : ""} ${isSel ? "selected" : ""} ${deleted ? "deleted" : ""} ${focused}" data-path="${escapeHtml(m.path)}" data-index="${memberIndex}">
            ${preview}
            <div class="card-body">
              <div class="name" title="${escapeHtml(m.path)}">${escapeHtml(fileName)}</div>
              <div class="path" title="${escapeHtml(m.path)}">${escapeHtml(m.path)}</div>
              <div class="card-meta">
                <span>${formatBytes(m.size)}</span>
                <span>${dims}</span>
                <span title="Modified">${escapeHtml(formatMtime(m.mtime))}</span>
                ${m.face_count != null ? `<span class="face-count ${m.face_count > 1 ? "multi" : ""}" title="Faces detected by OpenCV (heuristic)">${m.face_count === 0 ? "No faces" : `${m.face_count} face${m.face_count === 1 ? "" : "s"}`}</span>` : ""}
              </div>
              <div class="evidence">${escapeHtml(evidence)}</div>
              <div class="card-actions">
                ${actions}
              </div>
            </div>
          </article>
        `;
      })
      .join("");

    box.querySelectorAll(".thumb-wrap").forEach((preview) => {
      setPreviewAspectRatio(
        preview,
        preview.dataset.previewWidth,
        preview.dataset.previewHeight,
      );
    });

    box.querySelectorAll(".thumb-image").forEach((image) => {
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

    box.querySelectorAll(".hover-video").forEach((video) => {
      const wrap = video.closest(".thumb-wrap");
      video.addEventListener("loadedmetadata", () => {
        setPreviewAspectRatio(wrap, video.videoWidth, video.videoHeight);
      });
      wrap.addEventListener("pointerenter", () => {
        video.muted = true;
        if (!video.src) video.src = video.dataset.src;
        video.play().catch(() => {
          /* The static poster remains when the browser cannot play this codec. */
        });
      });
      wrap.addEventListener("pointerleave", () => {
        video.pause();
        if (video.readyState > 0) video.currentTime = 0;
      });
    });

    box.querySelectorAll(".hover-gif").forEach((image) => {
      const wrap = image.closest(".thumb-wrap");
      wrap.addEventListener("pointerenter", () => {
        image.src = image.dataset.src;
      });
      wrap.addEventListener("pointerleave", () => {
        image.src = image.dataset.thumbnail;
      });
    });

    box.querySelectorAll(".sel-cb").forEach((cb) => {
      cb.addEventListener("change", async () => {
        const changedPath = cb.dataset.path;
        const checks = [...box.querySelectorAll(".sel-cb")];
        const selectedPaths = checks.filter((c) => c.checked).map((c) => c.dataset.path);
        try {
          const updated = await api("/api/selection", {
            method: "POST",
            body: JSON.stringify({
              group_id: g.id,
              selected: selectedPaths,
              scan_id: state.scanId,
            }),
          });
          const idx = state.groups.findIndex((x) => x.id === g.id);
          if (idx >= 0) state.groups[idx] = updated;
          const aidx = state.allGroups.findIndex((x) => x.id === g.id);
          if (aidx >= 0) state.allGroups[aidx] = updated;
          renderMembers(updated);
          const replacement = [...box.querySelectorAll(".sel-cb")]
            .find((input) => input.dataset.path === changedPath);
          if (replacement) replacement.focus();
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

    box.querySelectorAll(".candidate-delete").forEach((btn) => {
      btn.addEventListener("click", () => reviewCandidate(g, btn.dataset.path, true));
    });
    box.querySelectorAll(".candidate-keep").forEach((btn) => {
      btn.addEventListener("click", () => reviewCandidate(g, btn.dataset.path, false));
    });

    box.querySelectorAll(".reveal").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          await api(`/api/reveal?path=${encodeURIComponent(btn.dataset.path)}&open=1`);
        } catch (err) {
          toast(err.message, "error");
        }
      });
    });

    async function updateDeletedCandidate(path, endpoint, previewToken = null) {
      try {
        const updated = await api(endpoint, {
          method: "POST",
          body: JSON.stringify({ group_id: g.id, path, scan_id: state.scanId,
            dry_run: endpoint.endsWith("delete") ? !previewToken : undefined,
            preview_token: previewToken }),
        });
        const idx = state.groups.findIndex((candidate) => candidate.id === g.id);
        if (idx >= 0) state.groups[idx] = updated;
        const allIdx = state.allGroups.findIndex((candidate) => candidate.id === g.id);
        if (allIdx >= 0) state.allGroups[allIdx] = updated;
        renderMembers(updated);
        scheduleRender({ groupList: true, selection: true });
        toast(endpoint.endsWith("undo") ? "Image restored" : "Moved to Trash", "ok");
      } catch (err) {
        toast(err.message, "error");
      }
    }

    box.querySelectorAll(".delete-candidate").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const member = allMembers.find((item) => item.path === btn.dataset.path);
        try {
          const preview = await api("/api/review-candidate/delete", { method: "POST", body: JSON.stringify({
            group_id: g.id, path: btn.dataset.path, scan_id: state.scanId, dry_run: true,
          }) });
          if (!preview.success_count) return toast("This file is not safely eligible for deletion", "error");
          const heuristicWarning = g.kind === "faces"
            ? "Face counting is heuristic and may miscount."
            : "Non-Human detection is heuristic and may miss people.";
          const ok = await confirmModal({ title: "Move this file to Trash?", danger: true,
            confirmLabel: "Move to Trash", body: `<div class="review-sheet"><p><strong>${escapeHtml(basename(member.path))} · ${formatBytes(member.size)}</strong></p><p class="heuristic-warning"><strong>Review carefully:</strong> ${heuristicWarning}</p></div>` });
          if (ok) await updateDeletedCandidate(btn.dataset.path, "/api/review-candidate/delete", preview.preview_token);
        } catch (err) { toast(err.message, "error"); }
      });
    });

    box.querySelectorAll(".undo-delete").forEach((btn) => {
      btn.addEventListener("click", () => {
        updateDeletedCandidate(btn.dataset.path, "/api/review-candidate/undo");
      });
    });

    box.querySelectorAll("button.thumb-wrap").forEach((el) => {
      el.addEventListener("click", () => {
        const i = Number(el.dataset.index);
        state.memberFocus = Number(el.closest(".card")?.dataset.index || 0);
        openLightbox(i);
      });
    });

    box.querySelectorAll(".card").forEach((card) => {
      card.addEventListener("click", (e) => {
        if (e.target.closest("input, button, label, a")) return;
        state.memberFocus = Number(card.dataset.index);
        box.querySelectorAll(".card").forEach((c) => c.classList.remove("focused"));
        card.classList.add("focused");
      });
    });
  }

  async function reviewCandidate(group, path, remove) {
    if (!isDecisionReview(group) || state.reviewingCandidate) return;
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
    }
  }

  function changeMemberPage(delta) {
    const current = state.allGroups.find((group) => group.id === state.currentId)
      || state.groups.find((group) => group.id === state.currentId);
    if (isDecisionReview(current)) {
      const nextIndex = Math.max(
        0,
        Math.min((current.members || []).length - 1, state.memberFocus + delta),
      );
      if (nextIndex === state.memberFocus) return;
      state.memberFocus = nextIndex;
      state.memberPage = nextIndex;
      renderMembers(current);
      $("memberPagination").scrollIntoView({ block: "start", behavior: "smooth" });
      return;
    }
    if (!current || current.kind !== "no_humans") return;
    const pageCount = Math.max(1, Math.ceil((current.members || []).length / MEMBER_PAGE_SIZE));
    const nextPage = Math.max(0, Math.min(pageCount - 1, state.memberPage + delta));
    if (nextPage === state.memberPage) return;
    state.memberPage = nextPage;
    state.memberFocus = 0;
    renderMembers(current);
    // Jump to the top pager so the next page of results is immediately visible.
    const topPager = $("memberPagination");
    if (topPager) topPager.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  document.querySelectorAll(".member-prev").forEach((btn) => {
    btn.addEventListener("click", () => changeMemberPage(-1));
  });
  document.querySelectorAll(".member-next").forEach((btn) => {
    btn.addEventListener("click", () => changeMemberPage(1));
  });

  function currentScope() {
    const el = $("actionScope");
    return el ? el.value : "all";
  }

  function scopeLabelFor(scope) {
    return (
      {
        all: "All selected categories",
        duplicates: "Exact + Similar",
        review_suggestions: "Low-res + Random review",
        exact: "Exact",
        similar: "Similar",
        low_resolution: "Low resolution",
        random_review: "Random review",
        no_humans: "Non-Human",
        faces: "Faces",
      }[scope] ||
      "All selected categories"
    );
  }

  function effectiveSelection(scope = currentScope()) {
    const source = state.allGroups.length ? state.allGroups : state.groups;
    const inScope = (g) =>
      scope === "all" ||
      g.kind === scope ||
      (scope === "duplicates" && (g.kind === "exact" || g.kind === "similar")) ||
      (scope === "review_suggestions" && ["low_resolution", "random_review"].includes(g.kind));
    const protectedPaths = new Set();
    for (const g of source) {
      if (!isIndependentReview(g)) continue;
      const selectedPaths = new Set(g.selected_for_removal || []);
      for (const path of g.reviewed_paths || []) {
        if (!selectedPaths.has(path)) protectedPaths.add(path);
      }
    }
    const selected = new Map();
    for (const g of source) {
      if (!inScope(g)) continue;
      const sel = new Set(g.selected_for_removal || []);
      const reviewed = new Set(g.reviewed_paths || []);
      for (const m of g.members || []) {
        if (
          !protectedPaths.has(m.path)
          && sel.has(m.path)
          && (!isIndependentReview(g) || reviewed.has(m.path))
        ) {
          selected.set(m.path, m);
        }
      }
    }
    for (const g of source) {
      if (!inScope(g)) continue;
      if (isIndependentReview(g) || !(g.members || []).length) continue;
      if (g.members.every((m) => selected.has(m.path))) {
        selected.delete(g.suggested_keep || g.members[0].path);
      }
    }
    return [...selected.values()];
  }

  function duplicateSelectionCounts() {
    const combined = new Set(effectiveSelection("duplicates").map((member) => member.path));
    const exact = new Set(
      effectiveSelection("exact")
        .map((member) => member.path)
        .filter((path) => combined.has(path)),
    );
    const similar = effectiveSelection("similar")
      .filter((member) => combined.has(member.path) && !exact.has(member.path))
      .length;
    return { exact: exact.size, similar, uniqueTotal: combined.size };
  }

  function updateSelectionSummary() {
    const scope = currentScope();
    const selected = effectiveSelection(scope);
    const count = selected.length;
    const bytes = selected.reduce((total, member) => total + (member.size || 0), 0);
    const prefix = scope === "all" ? "" : `${scopeLabelFor(scope)} · `;
    const duplicateCounts = scope === "duplicates" ? duplicateSelectionCounts() : null;
    const breakdown = duplicateCounts
      ? ` (${duplicateCounts.exact} Exact + ${duplicateCounts.similar} Similar)`
      : "";
    $("selectionSummary").textContent = `${prefix}${count} unique file${count === 1 ? "" : "s"} selected${breakdown} · ${formatBytes(bytes)}`;
  }

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

  function lowResolutionMaxPixels(id) {
    const megapixels = Number($(id).value);
    if (!Number.isFinite(megapixels) || megapixels <= 0) return null;
    return Math.round(megapixels * 1_000_000);
  }

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
        `<div class="group-empty">Scanning — matches will appear here as they are found…</div>`;
      $("countAll").textContent = "0";
      $("countExact").textContent = "0";
      $("countSimilar").textContent = "0";
      $("countLowResolution").textContent = "0";
      $("countRandomReview").textContent = "0";
      $("countNoHumans").textContent = "0";
      state.groups = [];
      state.allGroups = [];
      state.currentId = null;
      state.groupsVersion = -1;
      state.groupsTotal = 0;
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
          human_backend: "opencv",
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
      if (state.pollTimer) clearInterval(state.pollTimer);
      state.pollTimer = setInterval(async () => {
        try {
          await refreshStatus();
        } catch {
          /* ignore transient */
        }
      }, 350);
    } catch (e) {
      toast(e.message, "error");
    } finally {
      $("btnCancelScan").disabled = false;
    }
  }

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", async () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      state.kind = tab.dataset.kind;
      resetGroupListWindow();
      await loadGroups();
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
  // Filters render synchronously so the list matches the inputs on the next tick.
  [...LIVE_FILTER_IDS, "resultSort", "selectionFilter", "filterFaces", "issuesOnly", "hideCompleted"].forEach((id) => {
    $(id).addEventListener(LIVE_FILTER_IDS.includes(id) ? "input" : "change", () => {
      resetGroupListWindow();
      renderGroupList();
    });
  });
  $("btnClearFilters").addEventListener("click", () => {
    for (const id of LIVE_FILTER_IDS.filter((value) => value !== "resultSearch")) $(id).value = "";
    $("filterFaces").value = "any";
    resetGroupListWindow();
    renderGroupList();
  });
  $("btnNextReview").addEventListener("click", () => {
    const candidates = state.groups.filter(groupNeedsAttention);
    if (!candidates.length) return toast("No unreviewed results need attention", "ok");
    const current = candidates.findIndex((group) => group.id === state.currentId);
    selectGroup(candidates[(current + 1) % candidates.length].id);
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

  async function applyRuleToCurrentGroup(rule, successMessage) {
    if (!state.currentId) return toast("Select a group first");
    try {
      const g = await api("/api/smart-select", {
        method: "POST",
        body: JSON.stringify({
          rule,
          group_id: state.currentId,
          scan_id: state.scanId,
        }),
      });
      const idx = state.groups.findIndex((x) => x.id === g.id);
      if (idx >= 0) state.groups[idx] = g;
      const aidx = state.allGroups.findIndex((x) => x.id === g.id);
      if (aidx >= 0) state.allGroups[aidx] = g;
      renderMembers(g);
      if (selectionFiltersActive() || !updateGroupListItem(g)) {
        scheduleRender({ groupList: true });
      } else {
        applyResultControls();
      }
      scheduleRender({ selection: true });
      toast(successMessage, "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  }

  $("btnSelectSuggested").addEventListener("click", async () => {
    const current = state.allGroups.find((group) => group.id === state.currentId)
      || state.groups.find((group) => group.id === state.currentId);
    const rule = current?.kind === "no_humans" || current?.kind === "faces"
      ? "select_candidates"
      : "automatic";
    const message = current?.kind === "no_humans"
      ? "Reviewed non-human candidates selected"
      : current?.kind === "faces"
        ? "Reviewed face candidates selected"
        : "Suggested selection applied";
    await applyRuleToCurrentGroup(rule, message);
  });

  $("btnClearGroup").addEventListener("click", async () => {
    await applyRuleToCurrentGroup("deselect_all", "Group selection cleared");
  });

  $("btnSmartGroup").addEventListener("click", async () => {
    await applyRuleToCurrentGroup($("smartRule").value, "Selection rule applied to group");
  });

  $("btnSmartAll").addEventListener("click", async () => {
    try {
      await api("/api/smart-select", {
        method: "POST",
        body: JSON.stringify({ rule: $("smartRule").value, scan_id: state.scanId }),
      });
      await loadGroups();
      toast("Smart select applied to all groups", "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  });

  // —— Bulk selection over the filtered view ——
  async function runBulkSelection(operation, criteria = null, label = "") {
    const groupIds = state.groups.map((group) => group.id);
    if (!groupIds.length) return toast("No groups are shown in this filter");
    const independent = state.groups.filter(isIndependentReview).length;
    if (independent && operation !== "select_none") {
      const ok = await confirmModal({
        title: "Include independent review candidates?",
        confirmLabel: "Apply to all shown groups",
        danger: false,
        body: `<div class="review-sheet"><p><strong>${independent} independent review group${independent === 1 ? "" : "s"}</strong> ${independent === 1 ? "is" : "are"} in this view.</p><p>A bulk selection also marks those files reviewed, and every candidate can be selected. Nothing is deleted until you run and confirm an action.</p></div>`,
      });
      if (ok !== true) return;
    }
    try {
      const result = await api("/api/selection/bulk", {
        method: "POST",
        body: JSON.stringify({
          operation,
          group_ids: groupIds,
          criteria: criteria || {},
          scan_id: state.scanId,
        }),
      });
      await loadGroups({ preserveSelection: true });
      if (state.currentId) await selectGroup(state.currentId, { silent: true });
      scheduleRender({ groupList: true, selection: true });
      const noun = result.changed_count === 1 ? "group" : "groups";
      toast(
        `${label || operation}: ${result.changed_count} ${noun} updated · ${result.selected_count} files selected`,
        "ok",
      );
    } catch (e) {
      toast(e.message, "error");
    }
  }

  $("btnBulkAll").addEventListener("click", () => runBulkSelection("select_all", null, "Select all"));
  $("btnBulkNone").addEventListener("click", () => runBulkSelection("select_none", null, "Select none"));
  $("btnBulkInvert").addEventListener("click", () => runBulkSelection("invert", null, "Invert"));

  function syncBulkValueRow() {
    const rule = $("bulkCriteria").value;
    const needsValue = rule !== "smaller_than_keeper";
    $("bulkValueRow").hidden = !needsValue;
    $("bulkValue").placeholder = rule === "path_contains"
      ? "text or /folder/"
      : rule === "min_faces"
        ? "faces (e.g. 2)"
        : "MB";
  }
  $("bulkCriteria").addEventListener("change", syncBulkValueRow);
  syncBulkValueRow();

  $("btnBulkCriteria").addEventListener("click", () => {
    const rule = $("bulkCriteria").value;
    const raw = ($("bulkValue").value || "").trim();
    const criteria = {};
    if (rule === "smaller_than_keeper") {
      criteria.smaller_than_keeper = true;
    } else if (rule === "path_contains") {
      if (!raw) return toast("Enter the text a path must contain");
      criteria.path_contains = raw;
    } else if (rule === "min_faces") {
      const faces = Number(raw);
      if (!Number.isInteger(faces) || faces < 1) return toast("Enter a face count of 1 or more");
      criteria.min_faces = faces;
    } else {
      const megabytes = Number(raw);
      if (!Number.isFinite(megabytes) || megabytes < 0) return toast("Enter a size in MB");
      criteria[rule] = Math.round(megabytes * 1024 * 1024);
    }
    runBulkSelection("criteria", criteria, "Rule applied");
  });

  $("btnMarkRemainingHuman").addEventListener("click", async () => {
    const remaining = state.allGroups
      .filter((group) => group.kind === "no_humans")
      .reduce((count, group) => {
        const deleted = new Set(group.deleted_paths || []);
        return count + (group.members || []).filter((member) => !deleted.has(member.path)).length;
      }, 0);
    if (!remaining) return;
    const noun = remaining === 1 ? "file" : "files";
    if (!window.confirm(
      `Mark ${remaining} remaining ${noun} as containing humans? They will not appear in future Non-Human scans unless the files change.`,
    )) return;
    try {
      const result = await api("/api/non-human/mark-remaining-human", {
        method: "POST",
        body: JSON.stringify({ scan_id: state.scanId }),
      });
      await loadGroups();
      toast(`${result.marked_count} ${noun} marked as human`, "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  });

  $("btnMarkDistinct").addEventListener("click", async () => {
    const current = state.allGroups.find((group) => group.id === state.currentId);
    if (!current || current.kind !== "similar") return;
    if (!window.confirm(
      `Mark these ${current.member_count} files as distinct? This group will stay hidden in future scans unless one of the files changes.`,
    )) return;
    try {
      await api("/api/similar/mark-distinct", {
        method: "POST",
        body: JSON.stringify({ group_id: current.id, scan_id: state.scanId }),
      });
      await loadGroups();
      toast("Similar files marked as distinct", "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  });

  // —— Actions ——
  const MAX_PREVIEW_REFRESHES = 2;

  function previewNoticeHtml(notice) {
    return notice ? `<p class="preview-notice">${escapeHtml(notice)}</p>` : "";
  }

  async function runAction(action, dryRun, options = {}) {
    const { attempt = 0, notice = "" } = options;
    let previewToken = null;
    let previewValidity = null;
    const quarantine_dir = $("quarantineDir").value.trim() || null;
    if (action === "quarantine" && !dryRun && !quarantine_dir) {
      toast("Set a quarantine folder first");
      $("quarantineDir").focus();
      return;
    }

    // selection check (scoped to the chosen category)
    const scope = currentScope();
    const scopeLabel = scope === "all" ? "" : `${scopeLabelFor(scope)} `;
    const count = effectiveSelection(scope).length;
    if (action !== "isolate" && count === 0 && !dryRun) {
      toast(`No ${scopeLabel}files selected for removal`);
      return;
    }

    if (!dryRun) {
      let preview;
      try {
        preview = await api("/api/action", {
          method: "POST",
          body: JSON.stringify({
            action,
            dry_run: true,
            quarantine_dir,
            scan_id: state.scanId,
            kinds: scope,
            ...(action === "isolate" ? { isolate_mode: "copy", isolate_kinds: scope } : {}),
          }),
        });
        previewToken = preview.preview_token;
        previewValidity = Number(preview.preview_expires_in) || null;
      } catch (error) {
        toast(`Could not verify selection: ${error.message}`, "error");
        return;
      }

      if (action !== "isolate" && preview.success_count === 0) {
        toast(`No verified ${scopeLabel}files are eligible for this action`);
        await loadGroups();
        return;
      }

      const eligibleCount = preview.success_count;
      const verifiedCount = action === "isolate" ? count : eligibleCount;
      const counts = preview.selection_counts || {};
      const selectedMembers = effectiveSelection(scope);
      const totalBytes = selectedMembers.reduce((sum, member) => sum + (member.size || 0), 0);
      const facesBreakdown = counts.faces ? ` · ${counts.faces} Faces` : "";
      const duplicateBreakdown = `${counts.exact || 0} Exact · ${counts.similar || 0} Similar · ${counts.low_resolution || 0} Low-res · ${counts.random_review || 0} Random · ${counts.no_humans || 0} Non-Human${facesBreakdown}`;
      const skippedWarning = (action !== "isolate" && preview.fail_count)
        ? `<p><strong>${eligibleCount} eligible</strong> · ${preview.fail_count} skipped (stale/unavailable)</p>`
        : "";
      const reviewQuarantineCount = preview.review_quarantine_count || 0;
      const reviewQuarantineNote = reviewQuarantineCount
        ? `<p><strong>${reviewQuarantineCount} Low-res/Random review file${reviewQuarantineCount === 1 ? "" : "s"}</strong> will move to <code>${escapeHtml(preview.review_quarantine_dir)}</code> instead of system Trash.</p>`
        : "";
      const labels = {
        trash: `Move selected ${scopeLabel}files to Trash?`,
        quarantine: `Move selected ${scopeLabel}files to quarantine?`,
        isolate: `Copy ${scope === "all" ? "all groups" : `${scopeLabelFor(scope)} groups`} into a _Dedupe Review folder inside the scan root?`,
      };
      const bodies = {
        trash: `<div class="review-sheet">${previewNoticeHtml(notice)}<p><strong>${verifiedCount} unique files · ${formatBytes(totalBytes)}</strong></p>${skippedWarning}<p>${duplicateBreakdown}</p>${reviewQuarantineNote}<p>At least one file is always kept in every duplicate group. Other selected files go to system Trash and can be restored there.</p>${(counts.similar || counts.no_humans || counts.faces) ? '<p class="heuristic-warning"><strong>Review carefully:</strong> Similar matching, Non-Human detection, and face counting are heuristic, not guarantees.</p>' : ""}</div>`,
        quarantine: `<div class="review-sheet">${previewNoticeHtml(notice)}<p><strong>${verifiedCount} unique files · ${formatBytes(totalBytes)}</strong></p>${skippedWarning}<p>${duplicateBreakdown}</p><p>At least one file is always kept in every duplicate group. Files move to <code>${escapeHtml(quarantine_dir)}</code>; undo is a manual move back.</p>${(counts.similar || counts.no_humans || counts.faces) ? '<p class="heuristic-warning"><strong>Review carefully:</strong> Similar matching, Non-Human detection, and face counting are heuristic, not guarantees.</p>' : ""}</div>`,
        isolate: `<div class="review-sheet">${previewNoticeHtml(notice)}<p><strong>Non-destructive review copy</strong></p><p>${scope === "all" ? "Every source" : `Every ${scopeLabelFor(scope)} source`} will be revalidated and copied into a timestamped _Dedupe Review folder. Originals stay put.</p></div>`,
      };
      const ok = await confirmModal({
        title: labels[action] || "Confirm",
        body: bodies[action] || "",
        confirmLabel: action === "trash" ? "Move to Trash" : action === "quarantine" ? "Quarantine" : "Isolate",
        danger: action === "trash",
        validitySeconds: previewToken ? previewValidity : null,
      });
      if (ok === "expired") {
        // The token lapsed while the sheet was open: re-verify, then re-confirm.
        if (attempt >= MAX_PREVIEW_REFRESHES) {
          toast("Preview keeps expiring — try again when you are ready to confirm", "error");
          return;
        }
        toast("Preview expired — re-checking the current selection…");
        return runAction(action, dryRun, {
          attempt: attempt + 1,
          notice: "The previous preview expired. These numbers were just re-verified — confirm them again.",
        });
      }
      if (!ok) return;
    }

    try {
      const payload = {
        action,
        dry_run: dryRun,
        quarantine_dir,
        scan_id: state.scanId,
        kinds: scope,
        preview_token: previewToken,
      };
      if (action === "isolate") {
        payload.isolate_mode = "copy";
        payload.isolate_kinds = scope;
      }
      const res = await api("/api/action", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const mode = dryRun ? "Preview" : "Done";
      let msg = (!dryRun && res.fail_count > 0)
        ? `Done: ${res.success_count} ok, ${res.fail_count} skipped`
        : `${mode}: ${res.success_count} ok, ${res.fail_count} failed`;
      if (res.review_root) msg += ` · ${res.review_root}`;
      if (res.log_path) msg += ` · receipt saved`;
      if (res.fail_count) {
        const failed = res.items?.find((item) => item.error);
        if (failed) {
          msg += ` · ${basename(failed.path)}: ${failed.error}`;
        }
      }
      toast(msg, res.fail_count ? "" : "ok");
      if (!dryRun) {
        await loadGroups();
        await refreshStatus();
        if (action === "isolate" && res.review_root) {
          try {
            await api(`/api/reveal?path=${encodeURIComponent(res.review_root)}&open=1`);
          } catch {
            /* optional */
          }
        }
      } else if (res.items?.length) {
        const sample = res.items
          .slice(0, 3)
          .map((i) => basename(i.path))
          .join(", ");
        if (sample) toast(`${msg} — e.g. ${sample}`);
      }
    } catch (e) {
      const stale = e.data?.preview_stale
        || /preview expired|selection changed|fresh preview/i.test(e.message);
      if (stale && attempt < MAX_PREVIEW_REFRESHES) {
        // Never execute on a stale token: re-run the dry run and re-confirm.
        toast(`${e.message} — re-checking now…`);
        return runAction(action, dryRun, {
          attempt: attempt + 1,
          notice: `${e.message.charAt(0).toUpperCase()}${e.message.slice(1)}. These numbers were just re-verified — confirm them again.`,
        });
      }
      toast(e.message, "error");
    }
  }

  $("btnDryTrash").addEventListener("click", () => runAction("trash", true));
  $("btnTrash").addEventListener("click", () => runAction("trash", false));
  $("btnDryQuarantine").addEventListener("click", () => runAction("quarantine", true));
  $("btnQuarantine").addEventListener("click", () => runAction("quarantine", false));
  $("btnDryIsolate").addEventListener("click", () => runAction("isolate", true));
  $("btnIsolate").addEventListener("click", () => runAction("isolate", false));
  $("actionScope").addEventListener("change", updateSelectionSummary);

  // —— Lightbox ——
  function openLightbox(index) {
    if (!state.lightboxItems.length) return;
    state.lightboxIndex = Math.max(0, Math.min(index, state.lightboxItems.length - 1));
    updateLightbox();
    $("lightbox").hidden = false;
  }

  function closeLightbox() {
    $("lbVideo").pause();
    $("lbVideo").removeAttribute("src");
    $("lbVideo").load();
    $("lightbox").hidden = true;
  }

  function updateLightbox() {
    const item = state.lightboxItems[state.lightboxIndex];
    if (!item) return;
    const image = $("lbImage");
    const video = $("lbVideo");
    const isVideo = item.mediaType === "video";
    const canCompare = !isVideo && item.kind === "similar" && item.keeper && item.keeper !== item.path;

    video.pause();
    video.hidden = !isVideo;
    $("lbVideoTools").hidden = !isVideo;
    $("lbCompareTools").hidden = !canCompare;
    $("lbImageStack").hidden = isVideo;
    image.hidden = isVideo;
    if (isVideo) {
      image.removeAttribute("src");
      video.src = `/api/media?path=${encodeURIComponent(item.path)}`;
      video.playbackRate = Number($("lbSpeed").value);
    } else {
      video.removeAttribute("src");
      video.load();
      image.src = `/api/thumbnail?path=${encodeURIComponent(item.path)}&full=1`;
      const keeperImage = $("lbKeeperImage");
      keeperImage.hidden = !canCompare;
      if (canCompare) {
        keeperImage.src = `/api/thumbnail?path=${encodeURIComponent(item.keeper)}&full=1`;
        keeperImage.style.opacity = String(Number($("lbOpacity").value) / 100);
      } else keeperImage.removeAttribute("src");
    }
    $("lbMeta").textContent = item.path;
    $("lbPrev").disabled = state.lightboxIndex <= 0;
    $("lbNext").disabled = state.lightboxIndex >= state.lightboxItems.length - 1;
  }

  try {
    const savedRate = Number(localStorage.getItem(PLAYBACK_RATE_KEY));
    if ([0.5, 1, 1.5, 2, 3, 4].includes(savedRate)) $("lbSpeed").value = String(savedRate);
  } catch {
    /* ignore */
  }
  $("lbSpeed").addEventListener("change", () => {
    const rate = Number($("lbSpeed").value);
    $("lbVideo").playbackRate = rate;
    try {
      localStorage.setItem(PLAYBACK_RATE_KEY, String(rate));
    } catch {
      /* ignore */
    }
  });
  $("lbVideo").addEventListener("loadedmetadata", () => {
    $("lbVideo").playbackRate = Number($("lbSpeed").value);
  });
  $("lbOpacity").addEventListener("input", () => {
    $("lbKeeperImage").style.opacity = String(Number($("lbOpacity").value) / 100);
  });
  const showKeeper = (show) => {
    $("lbKeeperImage").style.opacity = show ? "1" : "0";
    $("lbFlicker").setAttribute("aria-pressed", show ? "true" : "false");
  };
  ["pointerdown", "keydown"].forEach((eventName) => $("lbFlicker").addEventListener(eventName, (event) => {
    if (eventName === "keydown" && ![" ", "Enter"].includes(event.key)) return;
    showKeeper(true);
  }));
  ["pointerup", "pointerleave", "keyup", "blur"].forEach((eventName) => $("lbFlicker").addEventListener(eventName, () => showKeeper(false)));

  $("lbClose").addEventListener("click", closeLightbox);
  $("lbPrev").addEventListener("click", () => {
    if (state.lightboxIndex > 0) {
      state.lightboxIndex -= 1;
      updateLightbox();
    }
  });
  $("lbNext").addEventListener("click", () => {
    if (state.lightboxIndex < state.lightboxItems.length - 1) {
      state.lightboxIndex += 1;
      updateLightbox();
    }
  });
  $("lightbox").addEventListener("click", (e) => {
    if (e.target === $("lightbox")) closeLightbox();
  });

  // —— Help ——
  function openHelp() {
    $("helpBackdrop").hidden = false;
  }
  function closeHelp() {
    $("helpBackdrop").hidden = true;
  }
  $("helpClose").addEventListener("click", closeHelp);
  $("helpBackdrop").addEventListener("click", (e) => {
    if (e.target === $("helpBackdrop")) closeHelp();
  });

  // —— Keyboard ——
  document.addEventListener("keydown", async (e) => {
    const tag = (e.target && e.target.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target?.isContentEditable;

    if (e.key === "Escape") {
      if (!$("lightbox").hidden) {
        closeLightbox();
        e.preventDefault();
        return;
      }
      if (!$("helpBackdrop").hidden) {
        closeHelp();
        e.preventDefault();
        return;
      }
      if (!$("modalBackdrop").hidden) return; // handled by modal
    }

    if (!$("modalBackdrop").hidden || !$("helpBackdrop").hidden) return;

    if (!typing && (e.key === "?" || (e.shiftKey && e.key === "/"))) {
      openHelp();
      e.preventDefault();
      return;
    }

    if (!$("lightbox").hidden) {
      if (typing || e.target === $("lbVideo")) return;
      if (e.key === "ArrowLeft") {
        $("lbPrev").click();
        e.preventDefault();
      } else if (e.key === "ArrowRight") {
        $("lbNext").click();
        e.preventDefault();
      }
      return;
    }

    if (typing || $("results").hidden) return;

    if (e.metaKey || e.ctrlKey || e.altKey) return;

    if (e.key === "j" || e.key === "ArrowDown") {
      navGroup(1);
      e.preventDefault();
    } else if (e.key === "k" || e.key === "ArrowUp") {
      navGroup(-1);
      e.preventDefault();
    } else if (e.key === "]") {
      navAttention(1);
      e.preventDefault();
    } else if (e.key === "[") {
      navAttention(-1);
      e.preventDefault();
    } else if (e.key === "u" && state.currentId) {
      $("btnSelectSuggested").click();
      e.preventDefault();
    } else if (e.key === "s" && state.currentId) {
      $("btnSmartGroup").click();
      e.preventDefault();
    } else if (e.key === "a") {
      if ($("actionBar").hidden) return;
      $("btnTrash").click();
      e.preventDefault();
    } else if (e.key === "Enter" && state.currentId) {
      openLightbox(state.memberFocus || 0);
      e.preventDefault();
    } else if (e.key === " " && state.currentId) {
      const cards = [...document.querySelectorAll("#members .card")];
      const card = cards[state.memberFocus] || cards[0];
      if (card) {
        const cb = card.querySelector(".sel-cb");
        if (cb) {
          cb.checked = !cb.checked;
          cb.dispatchEvent(new Event("change"));
        }
      }
      e.preventDefault();
    } else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && state.currentId) {
      const current = state.allGroups.find((group) => group.id === state.currentId)
        || state.groups.find((group) => group.id === state.currentId);
      if (isDecisionReview(current)) {
        const member = (current.members || [])[state.memberFocus];
        if (member) await reviewCandidate(current, member.path, e.key === "ArrowLeft");
        e.preventDefault();
        return;
      }
      const cards = document.querySelectorAll("#members .card");
      if (!cards.length) return;
      if (e.key === "ArrowRight") state.memberFocus = Math.min(cards.length - 1, state.memberFocus + 1);
      else state.memberFocus = Math.max(0, state.memberFocus - 1);
      cards.forEach((c) => c.classList.remove("focused"));
      cards[state.memberFocus].classList.add("focused");
      cards[state.memberFocus].scrollIntoView({ block: "nearest" });
      e.preventDefault();
    }
  });

  function navGroup(delta) {
    if (!state.groups.length) return;
    let idx = state.groups.findIndex((g) => g.id === state.currentId);
    if (idx < 0) idx = delta > 0 ? -1 : 0;
    idx = Math.max(0, Math.min(state.groups.length - 1, idx + delta));
    selectGroup(state.groups[idx].id);
  }

  function navAttention(delta) {
    const candidates = state.groups.filter(groupNeedsAttention);
    if (!candidates.length) return toast("No shown groups need attention", "ok");
    const current = candidates.findIndex((group) => group.id === state.currentId);
    const next = current < 0
      ? (delta > 0 ? 0 : candidates.length - 1)
      : (current + delta + candidates.length) % candidates.length;
    selectGroup(candidates[next].id);
  }

  // —— Init ——
  renderRecent();
  refreshStatus().catch(() => {});

  // Shut down the server when the tab is closed so the Terminal/.command window closes too.
  // sendBeacon cannot carry the X-Dedupe-Token header, so use fetch with keepalive,
  // which survives page teardown and passes the CSRF check.
  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    fetch("/api/shutdown", {
      method: "POST",
      keepalive: true,
      headers: {
        "Content-Type": "application/json",
        "X-Dedupe-Token": CSRF_TOKEN,
      },
      body: "{}",
    }).catch(() => {});
  });
})();
