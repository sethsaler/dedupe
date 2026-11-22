// Durable cleanup receipts and remembered Not duplicates decisions.
import { api } from "./api.js";
import { undoAction } from "./actions.js";
import { state } from "./state.js";
import { $, escapeHtml, formatBytes, basename, promoteToastTopLayer, toast } from "./util.js";

const PAGE_SIZE = 25;
let cleanupOffset = 0;
let distinctOffset = 0;
let current = null;
let loading = false;

function dateLabel(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : "Earlier cleanup";
}

function announceHistoryStatus(text) {
  const live = $("historyStatus");
  if (!live) return;
  live.textContent = text;
}

function captureFocusTarget(container) {
  const active = container.contains(document.activeElement) ? document.activeElement : null;
  if (!active) return null;
  if (active.dataset.historyPage) return { type: "page", value: active.dataset.historyPage };
  if (active.dataset.historyEntryId) return { type: "entry", value: active.dataset.historyEntryId };
  if (active.dataset.historyRestoreBatch !== undefined) return { type: "cropBatch" };
  if (active.dataset.historyReconsider !== undefined) {
    return { type: "reconsider", index: Number(active.dataset.historyReconsider) };
  }
  return null;
}

function restoreFocusTarget(container, token) {
  if (!token) return;
  let next = null;
  if (token.type === "page") {
    next = container.querySelector(`[data-history-page="${CSS.escape(token.value)}"]`);
  } else if (token.type === "entry") {
    next = container.querySelector(`[data-history-entry-id="${CSS.escape(token.value)}"]`);
  } else if (token.type === "cropBatch") {
    next = container.querySelector("[data-history-restore-batch]");
  } else if (token.type === "reconsider") {
    const buttons = [...container.querySelectorAll("[data-history-reconsider]")];
    next = buttons[Math.min(token.index, buttons.length - 1)] || null;
  }
  if (next && !next.disabled) {
    next.focus({ preventScroll: true });
    return;
  }
  const panel = $("historyPanel");
  if (panel?.open && !container.contains(document.activeElement)) {
    $("btnCloseHistory")?.focus({ preventScroll: true });
  }
}

async function fetchHistoryPages() {
  if (cleanupOffset === distinctOffset) {
    const data = await api(`/api/history?offset=${cleanupOffset}&limit=${PAGE_SIZE}`);
    return {
      actions: data.actions || [],
      actionCount: data.action_count || 0,
      decisions: data.decisions || [],
      decisionCount: data.decision_count || 0,
      errors: data.errors || [],
    };
  }
  const [actionData, decisionData] = await Promise.all([
    api(`/api/history?offset=${cleanupOffset}&limit=${PAGE_SIZE}`),
    api(`/api/history?offset=${distinctOffset}&limit=${PAGE_SIZE}`),
  ]);
  return {
    actions: actionData.actions || [],
    actionCount: actionData.action_count || 0,
    decisions: decisionData.decisions || [],
    decisionCount: decisionData.decision_count || 0,
    errors: [...new Set([...(actionData.errors || []), ...(decisionData.errors || [])])],
  };
}

function renderPagination(kind, offset, totalCount) {
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const prevDisabled = offset === 0 ? "disabled" : "";
  const nextDisabled = offset + PAGE_SIZE >= totalCount ? "disabled" : "";
  return `
    <div class="history-pagination">
      <button type="button" class="btn ghost" data-history-page="${kind}-prev" ${prevDisabled}>Previous</button>
      <span>Page ${page} of ${totalPages}</span>
      <button type="button" class="btn ghost" data-history-page="${kind}-next" ${nextDisabled}>Next</button>
    </div>`;
}

async function refreshHistory({ statusMessage = "" } = {}) {
  const container = $("historyList");
  if (!container || loading) return;
  const focusToken = captureFocusTarget(container);
  loading = true;
  container.setAttribute("aria-busy", "true");
  try {
    let data = await fetchHistoryPages();
    let reclamped = false;
    if (cleanupOffset > 0 && cleanupOffset >= data.actionCount) {
      cleanupOffset = Math.max(0, Math.floor((data.actionCount - 1) / PAGE_SIZE) * PAGE_SIZE);
      reclamped = true;
    }
    if (distinctOffset > 0 && distinctOffset >= data.decisionCount) {
      distinctOffset = Math.max(0, Math.floor((data.decisionCount - 1) / PAGE_SIZE) * PAGE_SIZE);
      reclamped = true;
    }
    if (reclamped) data = await fetchHistoryPages();
    current = data;
    const { actions, actionCount, decisions, decisionCount, errors } = data;
    const undoableCrops = actions.filter((entry) => entry.action === "crop" && entry.undoable);
    container.innerHTML = `
      <p class="muted">Cleanups and decisions stay here between scans and restarts. Restores are verified before you confirm.</p>
      <div class="section-heading">
        <h3>Cleanups (${actionCount})</h3>
        ${undoableCrops.length > 1 ? `<button type="button" class="btn ghost" data-history-restore-batch="crop">Restore all ${undoableCrops.length} crop batches…</button>` : ""}
      </div>
      ${actions.length ? actions.map((entry, index) => {
        const title = entry.action_label || String(entry.action || "action").replaceAll(":", " · ");
        const restoreLabel = entry.action === "crop"
          ? `Restore ${entry.restorable_count === 1 ? "original" : "originals"}…`
          : "Restore files…";
        return `
        <article class="history-entry">
          <div class="history-entry-title"><strong>${escapeHtml(title)}</strong>
            <span>${escapeHtml(dateLabel(entry.started_at))}</span></div>
          <p>${Number(entry.success_count)} successful · ${Number(entry.fail_count)} failed · ${formatBytes(entry.bytes || 0)}</p>
          ${entry.status === "interrupted" ? `<p role="status"><strong>Interrupted cleanup recovered.</strong> ${entry.uncertain_count ? `${Number(entry.uncertain_count)} file outcomes need inspection; only confirmed moves can be restored.` : "Recorded moves were checked without moving any files."}</p>` : ""}
          ${entry.status === "running" ? '<p role="status">Cleanup is still running.</p>' : ""}
          <details><summary>Affected files (${Number(entry.item_count)})</summary><ul class="history-files">
          ${(entry.items || []).map((item) => `<li><strong>${escapeHtml(basename(item.path))}</strong> — ${item.ok ? "Completed" : escapeHtml(item.error || "Not completed")}<br><span>${escapeHtml(item.path)}</span>${item.destination ? `<br><span>→ ${escapeHtml(item.destination)}</span>` : ""}</li>`).join("")}
          </ul>${entry.items_truncated ? "<p>Showing the first 200 files. The complete receipt remains saved on disk.</p>" : ""}</details>
          ${entry.undoable ? `<button type="button" class="btn ghost" data-history-restore="${index}" data-history-entry-id="${escapeHtml(entry.id)}">${restoreLabel}</button>` : `<p class="muted">${escapeHtml(entry.undo_blocked_reason || "No files to restore.")}</p>`}
        </article>`;
      }).join("") : '<p class="muted">No cleanups on this page.</p>'}
      ${actionCount > PAGE_SIZE ? renderPagination("cleanups", cleanupOffset, actionCount) : ""}
      <h3>Not duplicates (${decisionCount})</h3>
      <p class="muted">Reconsider a pair to include it in matching on your next scan.</p>
      ${decisions.length ? decisions.map((entry, index) => `<article class="history-entry">
        <p>${escapeHtml(entry.path_a)}<br>${escapeHtml(entry.path_b)}</p>
        <button type="button" class="btn ghost" data-history-reconsider="${index}">Reconsider this pair</button>
      </article>`).join("") : '<p class="muted">No remembered pairs on this page.</p>'}
      ${decisionCount > PAGE_SIZE ? renderPagination("distinct", distinctOffset, decisionCount) : ""}
      ${errors.map((error) => `<p role="alert">${escapeHtml(error)}</p>`).join("")}`;
    restoreFocusTarget(container, focusToken);
    announceHistoryStatus(
      statusMessage || `Loaded ${actionCount} cleanup${actionCount === 1 ? "" : "s"} and ${decisionCount} remembered pair${decisionCount === 1 ? "" : "s"}.`,
    );
  } catch (error) {
    container.textContent = `Could not load history: ${error.message}`;
    announceHistoryStatus(`Could not load history: ${error.message}`);
  } finally {
    loading = false;
    container.removeAttribute("aria-busy");
  }
}

function initHistory() {
  const panel = $("historyPanel");
  const button = $("btnHistory");
  if (!panel || !button || !$("historyList")) return;
  button.addEventListener("click", () => {
    if (!panel.open) {
      panel.showModal();
      $("btnCloseHistory")?.focus();
      promoteToastTopLayer();
      refreshHistory();
    } else {
      panel.close();
    }
  });
  $("btnCloseHistory")?.addEventListener("click", () => {
    if (panel.open) panel.close();
  });
  panel.addEventListener("close", (e) => {
    if (e.target === panel) button.focus();
  });
  $("historyList").addEventListener("click", async (event) => {
    const target = event.target.closest("button");
    if (!target || loading) return;
    if (target.dataset.historyPage) {
      const action = target.dataset.historyPage;
      if (action === "cleanups-prev") cleanupOffset = Math.max(0, cleanupOffset - PAGE_SIZE);
      else if (action === "cleanups-next") cleanupOffset += PAGE_SIZE;
      else if (action === "distinct-prev") distinctOffset = Math.max(0, distinctOffset - PAGE_SIZE);
      else if (action === "distinct-next") distinctOffset += PAGE_SIZE;
      return refreshHistory({
        statusMessage: action.startsWith("cleanups")
          ? `Showing cleanups page ${Math.floor(cleanupOffset / PAGE_SIZE) + 1}`
          : `Showing not duplicates page ${Math.floor(distinctOffset / PAGE_SIZE) + 1}`,
      });
    }
    if (state.scanning || state.acting || state.actionBusy) {
      toast("Wait for the current scan or cleanup to finish.");
      return;
    }
    target.disabled = true;
    try {
      let statusMessage = "";
      if (target.dataset.historyRestoreBatch === "crop") {
        const cropIds = (current?.actions || [])
          .filter((entry) => entry.action === "crop" && entry.undoable)
          .map((entry) => entry.id);
        if (cropIds.length) {
          await undoAction(cropIds);
          statusMessage = "Updated crop restore history.";
        }
      } else if (target.dataset.historyRestore !== undefined) {
        const entry = current.actions[Number(target.dataset.historyRestore)];
        if (entry) {
          await undoAction([entry.id]);
          statusMessage = "Updated cleanup history.";
        }
      } else if (target.dataset.historyReconsider !== undefined) {
        const entry = current.decisions[Number(target.dataset.historyReconsider)];
        if (entry) {
          await api("/api/history/reconsider", {
            method: "POST",
            body: JSON.stringify({
              path_a: entry.path_a, path_b: entry.path_b, scan_id: state.scanId,
            }),
          });
          toast("This pair will be considered again on the next scan.", "ok");
          statusMessage = "Pair removed from remembered decisions.";
        }
      }
      await refreshHistory({ statusMessage });
    } catch (error) {
      toast(error.message, "error");
      target.disabled = false;
    }
  });
}

export { initHistory, refreshHistory };
