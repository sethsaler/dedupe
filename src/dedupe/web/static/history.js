// Durable cleanup receipts and remembered Not duplicates decisions.
import { api } from "./api.js";
import { undoAction } from "./actions.js";
import { state } from "./state.js";
import { $, escapeHtml, formatBytes, basename, toast } from "./util.js";

let offset = 0;
let current = null;
let loading = false;

function dateLabel(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : "Earlier cleanup";
}

async function refreshHistory() {
  const container = $("historyList");
  if (!container || loading) return;
  loading = true;
  container.setAttribute("aria-busy", "true");
  try {
    current = await api(`/api/history?offset=${offset}&limit=25`);
    const actions = current.actions || [];
    const decisions = current.decisions || [];
    container.innerHTML = `
      <p class="muted">Cleanups and decisions stay here between scans and restarts. Restores are verified before you confirm.</p>
      <h3>Cleanups</h3>
      ${actions.length ? actions.map((entry, index) => `
        <article class="history-entry">
          <div class="history-entry-title"><strong>${escapeHtml(entry.action.replaceAll(":", " · "))}</strong>
            <span>${escapeHtml(dateLabel(entry.started_at))}</span></div>
          <p>${Number(entry.success_count)} successful · ${Number(entry.fail_count)} failed · ${formatBytes(entry.bytes || 0)}</p>
          ${entry.status === "interrupted" ? `<p role="status"><strong>Interrupted cleanup recovered.</strong> ${entry.uncertain_count ? `${Number(entry.uncertain_count)} file outcomes need inspection; only confirmed moves can be restored.` : "Recorded moves were checked without moving any files."}</p>` : ""}
          ${entry.status === "running" ? '<p role="status">Cleanup is still running.</p>' : ""}
          <details><summary>Affected files (${Number(entry.item_count)})</summary><ul class="history-files">
          ${(entry.items || []).map(item => `<li><strong>${escapeHtml(basename(item.path))}</strong> — ${item.ok ? "Completed" : escapeHtml(item.error || "Not completed")}<br><span>${escapeHtml(item.path)}</span>${item.destination ? `<br><span>→ ${escapeHtml(item.destination)}</span>` : ""}</li>`).join("")}
          </ul>${entry.items_truncated ? "<p>Showing the first 200 files. The complete receipt remains saved on disk.</p>" : ""}</details>
          ${entry.undoable ? `<button type="button" data-history-restore="${index}">Restore files…</button>` : `<p class="muted">${escapeHtml(entry.undo_blocked_reason || "No files to restore.")}</p>`}
        </article>`).join("") : '<p class="muted">No cleanups on this page.</p>'}
      <h3>Not duplicates</h3>
      <p class="muted">Reconsider a pair to include it in matching on your next scan.</p>
      ${decisions.length ? decisions.map((entry, index) => `<article class="history-entry">
        <p>${escapeHtml(entry.path_a)}<br>${escapeHtml(entry.path_b)}</p>
        <button type="button" data-history-reconsider="${index}">Reconsider this pair</button>
      </article>`).join("") : '<p class="muted">No remembered pairs on this page.</p>'}
      <div class="history-pagination"><button type="button" data-history-page="previous" ${offset === 0 ? "disabled" : ""}>Previous</button>
        <span>Page ${Math.floor(offset / 25) + 1}</span>
        <button type="button" data-history-page="next" ${offset + 25 >= Math.max(current.action_count || 0, current.decision_count || 0) ? "disabled" : ""}>Next</button></div>
      ${(current.errors || []).map(error => `<p role="alert">${escapeHtml(error)}</p>`).join("")}`;
  } catch (error) {
    container.textContent = `Could not load history: ${error.message}`;
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
    panel.hidden = !panel.hidden;
    button.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) refreshHistory();
  });
  $("btnRefreshHistory")?.addEventListener("click", refreshHistory);
  $("btnCloseHistory")?.addEventListener("click", () => {
    panel.hidden = true;
    button.setAttribute("aria-expanded", "false");
    button.focus();
  });
  $("historyList").addEventListener("click", async event => {
    const target = event.target.closest("button");
    if (!target || loading) return;
    if (target.dataset.historyPage) {
      offset = Math.max(0, offset + (target.dataset.historyPage === "next" ? 25 : -25));
      return refreshHistory();
    }
    if (state.scanning || state.acting || state.actionBusy) {
      toast("Wait for the current scan or cleanup to finish.");
      return;
    }
    target.disabled = true;
    try {
      if (target.dataset.historyRestore !== undefined) {
        const entry = current.actions[Number(target.dataset.historyRestore)];
        await undoAction([entry.id]);
      } else if (target.dataset.historyReconsider !== undefined) {
        const entry = current.decisions[Number(target.dataset.historyReconsider)];
        await api("/api/history/reconsider", { method: "POST", body: JSON.stringify({
          path_a: entry.path_a, path_b: entry.path_b, scan_id: state.scanId,
        }) });
        toast("This pair will be considered again on the next scan.", "ok");
      }
      await refreshHistory();
    } catch (error) {
      toast(error.message, "error");
      target.disabled = false;
    }
  });
}

export { initHistory, refreshHistory };
