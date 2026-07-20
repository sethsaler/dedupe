(() => {
  const $ = (id) => document.getElementById(id);

  const RECENT_KEY = "dedupe.recentPaths";
  const QUAR_KEY = "dedupe.quarantineDir";
  const WORKERS_KEY = "dedupe.workers";
  const SETTINGS_KEY = "dedupe.scanSettings.v1";
  const PLAYBACK_RATE_KEY = "dedupe.videoPlaybackRate";
  const MEMBER_PAGE_SIZE = 50;
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
  };

  const thresh = $("threshold");
  const threshVal = $("threshVal");
  thresh.addEventListener("input", () => {
    threshVal.textContent = thresh.value;
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

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
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
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  const settingIds = [
    "optExact",
    "optSimilar",
    "optNoHumans",
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
  function confirmModal({ title, body, confirmLabel = "Confirm", danger = true }) {
    return new Promise((resolve) => {
      $("modalTitle").textContent = title;
      $("modalBody").textContent = body;
      const btn = $("modalConfirm");
      btn.textContent = confirmLabel;
      btn.className = danger ? "btn danger" : "btn primary";
      $("modalBackdrop").hidden = false;

      const cleanup = (ok) => {
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
    });
  }

  // —— Status / groups ——
  async function refreshStatus() {
    const s = await api("/api/status");
    const statusEl = $("scanStatus");
    const wrap = $("progressWrap");
    const fill = $("progressFill");
    const msg = $("progressMsg");
    const top = $("topStats");

    state.scanning = !!s.scanning;
    state.acting = !!s.acting;
    state.scanId = s.scan_id || state.scanId;
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
      const eta = p.eta_seconds > 0 ? ` · about ${formatDuration(p.eta_seconds)} left` : "";
      msg.textContent =
        groupsSoFar > 0
          ? `${baseMsg}${baseMsg ? " · " : ""}${groupsSoFar} group${groupsSoFar === 1 ? "" : "s"} so far${eta}`
          : `${baseMsg}${eta}`;
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

    if (s.summary) {
      const scanningNote = s.scanning ? " · live" : "";
      top.innerHTML = `
        <span class="stat-chip"><span class="dot"></span><strong>${s.summary.group_count}</strong> groups${scanningNote}</span>
        <span class="stat-chip">${s.summary.exact_groups} exact · ${s.summary.similar_groups} similar · ${s.summary.no_human_files || 0} non-human</span>
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
      $("countNoHumans").textContent = s.summary.no_human_files || 0;
    } else {
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
    return s;
  }

  async function loadGroups({ preserveSelection = false } = {}) {
    const [filtered, all] = await Promise.all([
      api(`/api/groups?kind=${encodeURIComponent(state.kind)}`),
      api(`/api/groups?kind=all`),
    ]);
    state.groups = filtered.groups || [];
    state.allGroups = all.groups || [];

    const exact = state.allGroups.filter((g) => g.kind === "exact").length;
    const similar = state.allGroups.filter((g) => g.kind === "similar").length;
    const noHumans = state.allGroups
      .filter((g) => g.kind === "no_humans")
      .reduce((count, g) => count + (g.member_count || 0), 0);
    $("countAll").textContent = state.allGroups.length;
    $("countExact").textContent = exact;
    $("countSimilar").textContent = similar;
    $("countNoHumans").textContent = noHumans;

    if (state.groups.length) {
      $("emptyState").hidden = true;
      $("results").hidden = false;
    }

    renderGroupList();
    updateSelectionSummary();
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

  function renderGroupList() {
    const list = $("groupList");
    if (!state.groups.length) {
      list.innerHTML = `<div class="group-empty">No groups in this filter.</div>`;
      return;
    }
    list.innerHTML = state.groups
      .map((g) => {
        const active = g.id === state.currentId ? "active" : "";
        const sel = groupSelectedCount(g);
        const badgeLabel = g.kind === "no_humans" ? "non-human" : g.kind;
        const groupSummary = g.kind === "no_humans" && !sel
          ? `${g.member_count} non-human files to review`
          : `${formatBytes(g.reclaimable_bytes)} reclaimable`;
        return `
          <button class="group-item ${active}" data-id="${g.id}" type="button" role="option" aria-selected="${active ? "true" : "false"}">
            <div class="g-top">
              <span>${g.member_count} files${g.kind === "no_humans" ? "" : ` · ${escapeHtml(g.media_type)}`}</span>
              <span class="badge ${g.kind}">${badgeLabel}</span>
            </div>
            <div class="g-sub">
              <span>${groupSummary}</span>
              ${sel ? `<span class="sel-mark">${sel} selected</span>` : ""}
            </div>
          </button>
        `;
      })
      .join("");

    list.querySelectorAll(".group-item[data-id]").forEach((btn) => {
      btn.addEventListener("click", () => selectGroup(btn.dataset.id));
    });
  }

  function updateDetailMeta(g) {
    if (g.kind === "no_humans") {
      const reviewed = new Set(g.reviewed_paths || []);
      const selected = new Set(g.selected_for_removal || []);
      $("detailMeta").textContent =
        `${reviewed.size} of ${g.member_count} reviewed · ${selected.size} selected for removal · detector output is not a guarantee`;
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
    renderGroupList();
    const g = await api(`/api/groups/${id}`);
    const idx = state.groups.findIndex((group) => group.id === g.id);
    if (idx >= 0) state.groups[idx] = g;
    const allIdx = state.allGroups.findIndex((group) => group.id === g.id);
    if (allIdx >= 0) state.allGroups[allIdx] = g;
    updateSelectionSummary();
    $("detailEmpty").hidden = true;
    $("detailBody").hidden = false;
    const kindLabel = g.kind === "no_humans" ? "Non-Human · no person detected" : g.kind;
    $("detailTitle").textContent = g.kind === "no_humans"
      ? `${kindLabel} · ${g.member_count} files`
      : `${kindLabel} · ${g.media_type} · ${g.member_count} files`;
    const deletedPaths = new Set(g.deleted_paths || []);
    $("btnMarkRemainingHuman").hidden =
      g.kind !== "no_humans" || !(g.members || []).some((member) => !deletedPaths.has(member.path));
    $("btnMarkDistinct").hidden = g.kind !== "similar";
    document.querySelector(".selection-toolbar").hidden = g.kind === "no_humans";
    $("smartRule").querySelectorAll("option").forEach((option) => {
      const candidateOnly = option.value === "select_candidates";
      option.disabled = g.kind === "no_humans" ? !candidateOnly && option.value !== "deselect_all" : candidateOnly;
    });
    if ($("smartRule").selectedOptions[0]?.disabled) {
      $("smartRule").value = g.kind === "no_humans" ? "deselect_all" : "automatic";
    }
    $("btnSelectSuggested").textContent =
      g.kind === "no_humans" ? "Review + select all" : "Use suggested";
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
    const pageCount = g.kind === "no_humans"
      ? Math.max(1, Math.ceil(allMembers.length / MEMBER_PAGE_SIZE))
      : 1;
    state.memberPage = Math.max(0, Math.min(pageCount - 1, state.memberPage));
    const pageStart = state.memberPage * MEMBER_PAGE_SIZE;
    const members = g.kind === "no_humans"
      ? allMembers.slice(pageStart, pageStart + MEMBER_PAGE_SIZE)
      : allMembers;
    const summaryText = allMembers.length
      ? `${pageStart + 1}–${Math.min(pageStart + members.length, allMembers.length)} of ${allMembers.length}`
      : "0 results";
    syncMemberPagination(pageCount, summaryText);
    state.lightboxItems = members
      .filter((member) => !deletedPaths.has(member.path))
      .map((member) => ({ path: member.path, mediaType: member.media_type }));
    updateDetailMeta(g);
    const reviewedCount = allMembers.filter((member) => reviewedPaths.has(member.path)).length;
    $("groupSelectionSummary").textContent = g.kind === "no_humans"
      ? `${selected.size} selected · ${reviewedCount} of ${allMembers.length} reviewed`
      : `${selected.size} of ${allMembers.length} selected for removal`;

    box.innerHTML = members
      .map((m, i) => {
        const isKeep = m.path === g.suggested_keep && !selected.has(m.path);
        const isSel = selected.has(m.path);
        const reviewed = reviewedPaths.has(m.path);
        const deleted = deletedPaths.has(m.path);
        const dims = m.width && m.height ? `${m.width}×${m.height}` : "—";
        const thumb = `/api/thumbnail?path=${encodeURIComponent(m.path)}`;
        const focused = i === state.memberFocus ? "focused" : "";
        const lightboxIndex = state.lightboxItems.findIndex((item) => item.path === m.path);
        const fileName = basename(m.path);
        const badge = isSel
          ? `<span class="thumb-badge remove">Remove</span>`
          : isKeep
            ? `<span class="thumb-badge keep">Keep</span>`
            : "";
        const evidence = g.kind === "exact"
          ? "Byte-identical SHA-256 match"
          : g.kind === "similar"
            ? "Direct perceptual match to the suggested keeper"
            : `OpenCV person detection analyzed ${m.human_frames_analyzed || 0} frame(s); no person detected — likely non-human`;
        const selectionTitle = isSel
          ? (g.kind === "no_humans" ? "Reviewed · selected" : "Selected for removal")
          : (g.kind === "no_humans" && reviewed ? "Reviewed · not selected" : "Not selected");
        const selectionHint = isSel
          ? "Click to keep this file"
          : (g.kind === "no_humans" ? "Click to review and remove" : "Click to remove this file");
        const mediaPreview = m.media_type === "video"
          ? `<video class="hover-video" poster="${thumb}" data-src="/api/media?path=${encodeURIComponent(m.path)}" muted loop playsinline preload="none"></video>`
          : `<img class="thumb-image ${m.media_type === "gif" ? "hover-gif" : ""}" src="${thumb}" ${m.media_type === "gif" ? `data-thumbnail="${thumb}" data-src="/api/media?path=${encodeURIComponent(m.path)}"` : ""} alt="Preview of ${escapeHtml(fileName)}" loading="lazy" />`;
        const preview = deleted
          ? `<div class="thumb-wrap deleted-preview"><div class="thumb-fallback">Moved to Trash — undo available</div></div>`
          : `<button class="thumb-wrap" data-path="${escapeHtml(m.path)}" data-index="${lightboxIndex}" type="button" aria-label="Open preview for ${escapeHtml(fileName)}">
              ${badge}
              ${mediaPreview}
              ${["video", "gif"].includes(m.media_type) ? '<span class="video-preview-badge" aria-hidden="true">▶ Hover to play</span>' : ""}
            </button>`;
        const actions = g.kind === "no_humans"
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
          <article class="card ${isKeep ? "keep" : ""} ${isSel ? "selected" : ""} ${deleted ? "deleted" : ""} ${focused}" data-path="${escapeHtml(m.path)}" data-index="${i}">
            ${preview}
            <div class="card-body">
              <div class="name" title="${escapeHtml(m.path)}">${escapeHtml(fileName)}</div>
              <div class="path" title="${escapeHtml(m.path)}">${escapeHtml(m.path)}</div>
              <div class="card-meta">
                <span>${formatBytes(m.size)}</span>
                <span>${dims}</span>
                <span title="Modified">${escapeHtml(formatMtime(m.mtime))}</span>
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

    box.querySelectorAll(".thumb-image").forEach((image) => {
      image.addEventListener("error", () => {
        const fallback = document.createElement("div");
        fallback.className = "thumb-fallback";
        fallback.textContent = "No preview";
        image.replaceWith(fallback);
      });
    });

    box.querySelectorAll(".hover-video").forEach((video) => {
      const wrap = video.closest(".thumb-wrap");
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
          renderGroupList();
          updateSelectionSummary();
        } catch (e) {
          toast(e.message, "error");
          cb.checked = !cb.checked;
        }
      });
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

    async function updateDeletedCandidate(path, endpoint) {
      try {
        const updated = await api(endpoint, {
          method: "POST",
          body: JSON.stringify({ group_id: g.id, path, scan_id: state.scanId }),
        });
        const idx = state.groups.findIndex((candidate) => candidate.id === g.id);
        if (idx >= 0) state.groups[idx] = updated;
        const allIdx = state.allGroups.findIndex((candidate) => candidate.id === g.id);
        if (allIdx >= 0) state.allGroups[allIdx] = updated;
        renderMembers(updated);
        renderGroupList();
        updateSelectionSummary();
        toast(endpoint.endsWith("undo") ? "Image restored" : "Moved to Trash", "ok");
      } catch (err) {
        toast(err.message, "error");
      }
    }

    box.querySelectorAll(".delete-candidate").forEach((btn) => {
      btn.addEventListener("click", () => {
        updateDeletedCandidate(btn.dataset.path, "/api/non-human/delete");
      });
    });

    box.querySelectorAll(".undo-delete").forEach((btn) => {
      btn.addEventListener("click", () => {
        updateDeletedCandidate(btn.dataset.path, "/api/non-human/undo");
      });
    });

    box.querySelectorAll("button.thumb-wrap").forEach((el) => {
      el.addEventListener("click", () => {
        const i = Number(el.dataset.index);
        state.memberFocus = i;
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

  function changeMemberPage(delta) {
    const current = state.allGroups.find((group) => group.id === state.currentId)
      || state.groups.find((group) => group.id === state.currentId);
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
        duplicates: "Exact + Similar",
        exact: "Exact",
        similar: "Similar",
        no_humans: "Non-Human",
        all: "All",
      }[scope] ||
      "All"
    );
  }

  function effectiveSelection(scope = currentScope()) {
    const source = state.allGroups.length ? state.allGroups : state.groups;
    const inScope = (g) =>
      scope === "all" ||
      g.kind === scope ||
      (scope === "duplicates" && (g.kind === "exact" || g.kind === "similar"));
    const selected = new Map();
    for (const g of source) {
      if (!inScope(g)) continue;
      const sel = new Set(g.selected_for_removal || []);
      const reviewed = new Set(g.reviewed_paths || []);
      for (const m of g.members || []) {
        if (sel.has(m.path) && (g.kind !== "no_humans" || reviewed.has(m.path))) {
          selected.set(m.path, m);
        }
      }
    }
    for (const g of source) {
      if (!inScope(g)) continue;
      if (g.kind === "no_humans" || !(g.members || []).length) continue;
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

  async function startScan() {
    const raw = $("paths").value.trim();
    if (!raw) {
      toast("Enter at least one folder path");
      $("paths").focus();
      return;
    }
    const paths = raw.split(",").map((s) => s.trim()).filter(Boolean);
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
      $("countNoHumans").textContent = "0";
      state.groups = [];
      state.allGroups = [];
      state.currentId = null;
      state.groupsVersion = -1;
      const workersRaw = Number($("workers").value);
      const started = await api("/api/scan", {
        method: "POST",
        body: JSON.stringify({
          paths,
          exact: $("optExact").checked,
          similar: $("optSimilar").checked,
          find_no_humans: $("optNoHumans").checked,
          human_backend: "opencv",
          include_images: $("optImages").checked,
          include_gifs: $("optGifs").checked,
          include_videos: $("optVideos").checked,
          threshold: Number($("threshold").value),
          // 0 / Auto → null so backend uses resolve_workers auto
          workers: workersRaw > 0 ? workersRaw : null,
          exclusions: $("exclusions").value.split(",").map((value) => value.trim()).filter(Boolean),
        }),
      });
      state.scanId = started.scan_id || state.scanId;
      if (state.pollTimer) clearInterval(state.pollTimer);
      state.pollTimer = setInterval(async () => {
        try {
          const s = await refreshStatus();
          if (!s.scanning && s.progress?.done) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
            if (s.error) toast(s.error, "error");
            else toast(s.progress.message || "Scan complete", "ok");
          }
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
      await loadGroups();
    });
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
      renderGroupList();
      updateSelectionSummary();
      toast(successMessage, "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  }

  $("btnSelectSuggested").addEventListener("click", async () => {
    const current = state.allGroups.find((group) => group.id === state.currentId)
      || state.groups.find((group) => group.id === state.currentId);
    const rule = current?.kind === "no_humans" ? "select_candidates" : "automatic";
    const message = current?.kind === "no_humans"
      ? "All non-human candidates reviewed and selected"
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
  async function runAction(action, dryRun) {
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
      } catch (error) {
        toast(`Could not verify selection: ${error.message}`, "error");
        return;
      }

      if (action !== "isolate" && preview.fail_count) {
        const reason = preview.items?.find((item) => item.error)?.error;
        toast(`Nothing moved: ${preview.fail_count} selected file(s) failed revalidation${reason ? ` · ${reason}` : ""}`, "error");
        return;
      }
      if (action !== "isolate" && preview.success_count === 0) {
        toast(`No verified ${scopeLabel}files are eligible for this action`);
        await loadGroups();
        return;
      }

      const verifiedCount = action === "isolate" ? count : preview.success_count;
      const counts = preview.selection_counts || {};
      const duplicateBreakdown = scope === "duplicates"
        ? ` (${counts.exact || 0} Exact + ${counts.similar || 0} Similar = ${verifiedCount} unique total)`
        : "";
      const labels = {
        trash: `Move selected ${scopeLabel}files to Trash?`,
        quarantine: `Move selected ${scopeLabel}files to quarantine?`,
        isolate: `Copy ${scope === "all" ? "all groups" : `${scopeLabelFor(scope)} groups`} into a _Dedupe Review folder inside the scan root?`,
      };
      const bodies = {
        trash: `${verifiedCount} verified ${scopeLabel}file(s) will go to Trash${duplicateBreakdown} (recoverable in Finder on macOS).`,
        quarantine: `${verifiedCount} verified ${scopeLabel}file(s) will move to ${quarantine_dir}${duplicateBreakdown}.`,
        isolate: `${scope === "all" ? "Every source" : `Every ${scopeLabelFor(scope)} source`} will be revalidated, then copied into a new timestamped review session. Originals stay put.`,
      };
      const ok = await confirmModal({
        title: labels[action] || "Confirm",
        body: bodies[action] || "",
        confirmLabel: action === "trash" ? "Move to Trash" : action === "quarantine" ? "Quarantine" : "Isolate",
        danger: action === "trash",
      });
      if (!ok) return;
    }

    try {
      const payload = {
        action,
        dry_run: dryRun,
        quarantine_dir,
        scan_id: state.scanId,
        kinds: scope,
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
      let msg = `${mode}: ${res.success_count} ok, ${res.fail_count} failed`;
      if (res.review_root) msg += ` · ${res.review_root}`;
      if (res.log_path) msg += ` · receipt saved`;
      if (res.fail_count && res.items?.find((item) => item.error)?.error) {
        msg += ` · ${res.items.find((item) => item.error).error}`;
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

    video.pause();
    video.hidden = !isVideo;
    $("lbVideoTools").hidden = !isVideo;
    image.hidden = isVideo;
    if (isVideo) {
      image.removeAttribute("src");
      video.src = `/api/media?path=${encodeURIComponent(item.path)}`;
      video.playbackRate = Number($("lbSpeed").value);
    } else {
      video.removeAttribute("src");
      video.load();
      image.src = `/api/thumbnail?path=${encodeURIComponent(item.path)}&full=1`;
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

    if (e.key === "j" || e.key === "ArrowDown") {
      navGroup(1);
      e.preventDefault();
    } else if (e.key === "k" || e.key === "ArrowUp") {
      navGroup(-1);
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

  // —— Init ——
  renderRecent();
  refreshStatus().catch(() => {});
})();
