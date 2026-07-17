(() => {
  const $ = (id) => document.getElementById(id);

  const RECENT_KEY = "dedupe.recentPaths";
  const QUAR_KEY = "dedupe.quarantineDir";
  const WORKERS_KEY = "dedupe.workers";

  const state = {
    kind: "all",
    groups: [],
    allGroups: [],
    currentId: null,
    pollTimer: null,
    memberFocus: 0,
    lightboxPaths: [],
    lightboxIndex: 0,
    scanning: false,
    cpuCount: 0,
    autoWorkers: 0,
    groupsVersion: -1, // tracks streaming updates mid-scan
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
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
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

  // —— Folder pick (server native dialog when available) ——
  $("btnPickFolder").addEventListener("click", async () => {
    try {
      const data = await api("/api/pick-folder", { method: "POST", body: "{}" });
      if (data.path) {
        const cur = $("paths").value.trim();
        $("paths").value = cur ? `${cur}, ${data.path}` : data.path;
        toast("Folder added", "ok");
      } else if (data.cancelled) {
        /* user cancelled */
      } else {
        toast(data.message || "Paste a folder path instead");
      }
    } catch (e) {
      toast(e.message || "Could not open folder picker — paste a path instead");
    }
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
      msg.textContent =
        groupsSoFar > 0
          ? `${baseMsg}${baseMsg ? " · " : ""}${groupsSoFar} group${groupsSoFar === 1 ? "" : "s"} so far`
          : baseMsg;
    } else {
      $("btnScan").disabled = false;
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
        <span class="stat-chip">${s.summary.exact_groups} exact · ${s.summary.similar_groups} similar</span>
        <span class="stat-chip reclaim"><span class="dot"></span><strong>${s.summary.reclaimable_human}</strong> reclaimable</span>
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
    $("countAll").textContent = state.allGroups.length;
    $("countExact").textContent = exact;
    $("countSimilar").textContent = similar;

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
        return `
          <button class="group-item ${active}" data-id="${g.id}" type="button" role="option" aria-selected="${active ? "true" : "false"}">
            <div class="g-top">
              <span>${g.member_count} files · ${escapeHtml(g.media_type)}</span>
              <span class="badge ${g.kind}">${g.kind}</span>
            </div>
            <div class="g-sub">
              <span>${formatBytes(g.reclaimable_bytes)} reclaimable</span>
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

  async function selectGroup(id, { silent = false } = {}) {
    state.currentId = id;
    state.memberFocus = 0;
    renderGroupList();
    const g = await api(`/api/groups/${id}`);
    $("detailEmpty").hidden = true;
    $("detailBody").hidden = false;
    $("detailTitle").textContent = `${g.kind} · ${g.media_type} · ${g.member_count} files`;
    $("detailMeta").textContent = `${formatBytes(g.reclaimable_bytes)} reclaimable if extras removed`;
    renderMembers(g);
    // keep list item in view
    const active = document.querySelector(`.group-item[data-id="${id}"]`);
    if (active && !silent) active.scrollIntoView({ block: "nearest" });
  }

  function renderMembers(g) {
    const box = $("members");
    const selected = new Set(g.selected_for_removal || []);
    const members = g.members || [];
    state.lightboxPaths = members.map((m) => m.path);

    box.innerHTML = members
      .map((m, i) => {
        const isKeep = m.path === g.suggested_keep && !selected.has(m.path);
        const isSel = selected.has(m.path);
        const dims = m.width && m.height ? `${m.width}×${m.height}` : "—";
        const thumb = `/api/thumbnail?path=${encodeURIComponent(m.path)}`;
        const focused = i === state.memberFocus ? "focused" : "";
        const badge = isSel
          ? `<span class="thumb-badge remove">Remove</span>`
          : isKeep
            ? `<span class="thumb-badge keep">Keep</span>`
            : "";
        return `
          <article class="card ${isKeep ? "keep" : ""} ${isSel ? "selected" : ""} ${focused}" data-path="${escapeHtml(m.path)}" data-index="${i}">
            <div class="thumb-wrap" data-path="${escapeHtml(m.path)}" data-index="${i}" title="Click to enlarge">
              ${badge}
              <img src="${thumb}" alt="" loading="lazy"
                onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb-fallback',textContent:'No preview'}))" />
            </div>
            <div class="card-body">
              <div class="name" title="${escapeHtml(m.path)}">${escapeHtml(basename(m.path))}</div>
              <div class="path" title="${escapeHtml(m.path)}">${escapeHtml(m.path)}</div>
              <div class="card-meta">
                <span>${formatBytes(m.size)}</span>
                <span>${dims}</span>
              </div>
              <div class="card-actions">
                <label>
                  <input type="checkbox" class="sel-cb" data-path="${escapeHtml(m.path)}" ${isSel ? "checked" : ""} />
                  Remove
                </label>
                <button class="linkish reveal" data-path="${escapeHtml(m.path)}" type="button">Reveal</button>
              </div>
            </div>
          </article>
        `;
      })
      .join("");

    box.querySelectorAll(".sel-cb").forEach((cb) => {
      cb.addEventListener("change", async () => {
        const checks = [...box.querySelectorAll(".sel-cb")];
        const selectedPaths = checks.filter((c) => c.checked).map((c) => c.dataset.path);
        try {
          const updated = await api("/api/selection", {
            method: "POST",
            body: JSON.stringify({ group_id: g.id, selected: selectedPaths }),
          });
          const idx = state.groups.findIndex((x) => x.id === g.id);
          if (idx >= 0) state.groups[idx] = updated;
          const aidx = state.allGroups.findIndex((x) => x.id === g.id);
          if (aidx >= 0) state.allGroups[aidx] = updated;
          renderMembers(updated);
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

    box.querySelectorAll(".thumb-wrap").forEach((el) => {
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

  function updateSelectionSummary() {
    let count = 0;
    let bytes = 0;
    const source = state.allGroups.length ? state.allGroups : state.groups;
    for (const g of source) {
      const sel = new Set(g.selected_for_removal || []);
      for (const m of g.members || []) {
        if (sel.has(m.path)) {
          count += 1;
          bytes += m.size || 0;
        }
      }
    }
    $("selectionSummary").textContent = `${count} file${count === 1 ? "" : "s"} selected · ${formatBytes(bytes)}`;
  }

  // —— Scan ——
  $("btnScan").addEventListener("click", startScan);
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
      state.groups = [];
      state.allGroups = [];
      state.currentId = null;
      state.groupsVersion = -1;
      const workersRaw = Number($("workers").value);
      await api("/api/scan", {
        method: "POST",
        body: JSON.stringify({
          paths,
          exact: $("optExact").checked,
          similar: $("optSimilar").checked,
          include_images: $("optImages").checked,
          include_gifs: $("optGifs").checked,
          include_videos: $("optVideos").checked,
          threshold: Number($("threshold").value),
          // 0 / Auto → null so backend uses resolve_workers auto
          workers: workersRaw > 0 ? workersRaw : null,
        }),
      });
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

  $("btnSmartGroup").addEventListener("click", async () => {
    if (!state.currentId) return toast("Select a group first");
    try {
      const g = await api("/api/smart-select", {
        method: "POST",
        body: JSON.stringify({ rule: $("smartRule").value, group_id: state.currentId }),
      });
      const idx = state.groups.findIndex((x) => x.id === g.id);
      if (idx >= 0) state.groups[idx] = g;
      const aidx = state.allGroups.findIndex((x) => x.id === g.id);
      if (aidx >= 0) state.allGroups[aidx] = g;
      renderMembers(g);
      renderGroupList();
      updateSelectionSummary();
      toast("Smart select applied to group", "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  });

  $("btnSmartAll").addEventListener("click", async () => {
    try {
      await api("/api/smart-select", {
        method: "POST",
        body: JSON.stringify({ rule: $("smartRule").value }),
      });
      await loadGroups();
      toast("Smart select applied to all groups", "ok");
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

    // selection check
    let count = 0;
    const source = state.allGroups.length ? state.allGroups : state.groups;
    for (const g of source) count += groupSelectedCount(g);
    if (action !== "isolate" && count === 0 && !dryRun) {
      toast("No files selected for removal");
      return;
    }

    if (!dryRun) {
      const labels = {
        trash: "Move selected files to Trash?",
        quarantine: "Move selected files to quarantine?",
        isolate: "Copy all groups into a _Dedupe Review folder inside the scan root?",
      };
      const bodies = {
        trash: `${count} file(s) will go to Trash (recoverable in Finder on macOS).`,
        quarantine: `${count} file(s) will move to ${quarantine_dir}.`,
        isolate: "Originals stay put (copy mode). Opens a review tree for inspection.",
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
      const payload = { action, dry_run: dryRun, quarantine_dir };
      if (action === "isolate") {
        payload.isolate_mode = "copy";
      }
      const res = await api("/api/action", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const mode = dryRun ? "Preview" : "Done";
      let msg = `${mode}: ${res.success_count} ok, ${res.fail_count} failed`;
      if (res.review_root) msg += ` · ${res.review_root}`;
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

  // —— Lightbox ——
  function openLightbox(index) {
    if (!state.lightboxPaths.length) return;
    state.lightboxIndex = Math.max(0, Math.min(index, state.lightboxPaths.length - 1));
    updateLightbox();
    $("lightbox").hidden = false;
  }

  function closeLightbox() {
    $("lightbox").hidden = true;
  }

  function updateLightbox() {
    const path = state.lightboxPaths[state.lightboxIndex];
    if (!path) return;
    $("lbImage").src = `/api/thumbnail?path=${encodeURIComponent(path)}&full=1`;
    $("lbMeta").textContent = path;
    $("lbPrev").disabled = state.lightboxIndex <= 0;
    $("lbNext").disabled = state.lightboxIndex >= state.lightboxPaths.length - 1;
  }

  $("lbClose").addEventListener("click", closeLightbox);
  $("lbPrev").addEventListener("click", () => {
    if (state.lightboxIndex > 0) {
      state.lightboxIndex -= 1;
      updateLightbox();
    }
  });
  $("lbNext").addEventListener("click", () => {
    if (state.lightboxIndex < state.lightboxPaths.length - 1) {
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
