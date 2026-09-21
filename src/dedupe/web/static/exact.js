// Exact duplicates have only a recovery surface, backed by durable receipts.

import { api } from "./api.js";
import { undoAction } from "./actions.js";
import { state } from "./state.js";
import { $, escapeHtml, toast } from "./util.js";

async function refreshExactRecovery() {
  if (!$("exactRecovery").open) return;
  const list = $("exactRecoveryList");
  try {
    const { receipts } = await api("/api/exact-trash");
    list.innerHTML = receipts.length ? receipts.map((receipt) => {
      const count = receipt.success_count;
      const restored = receipt.restored_count;
      const date = new Date(receipt.completed_at).toLocaleString();
      return `<div class="exact-recovery-row">
        <details>
          <summary>${count} exact duplicate${count === 1 ? "" : "s"} moved to Trash · ${escapeHtml(date)}</summary>
          <ul>${receipt.paths.map((path) => `<li>${escapeHtml(path)}</li>`).join("")}</ul>
        </details>
        ${restored === count ? '<span class="muted small">Recovered</span>'
          : restored ? '<span class="muted small">Partially recovered — restore remaining files from system Trash</span>'
          : `<button class="btn primary-soft" type="button" data-receipt="${escapeHtml(receipt.log_path)}">Recover ${count} file${count === 1 ? "" : "s"}</button>`}
      </div>`;
    }).join("") : '<p class="muted small">No automatic exact-match removals recorded yet.</p>';
    syncExactRecoveryBusy();
  } catch (error) {
    list.textContent = `Could not load recovery history: ${error.message}. Close and reopen to retry.`;
  }
}

function syncExactRecoveryBusy() {
  $("exactRecoveryList").querySelectorAll("button").forEach((button) => {
    button.disabled = state.scanning || state.acting || state.actionBusy;
  });
}

$("exactRecovery").addEventListener("toggle", refreshExactRecovery);
$("exactRecoveryList").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-receipt]");
  if (!button || button.disabled) return;
  try {
    await undoAction([button.dataset.receipt]);
  } catch (error) {
    toast(error.message, "error");
  }
  await refreshExactRecovery();
});

export { refreshExactRecovery, syncExactRecoveryBusy };
