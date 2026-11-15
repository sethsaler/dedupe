// The preview-and-confirm sheet.

import { $, promoteToastTopLayer } from "./util.js";

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
    const dialog = $("modalBackdrop");
    $("modalTitle").textContent = title;
    $("modalBody").innerHTML = body;
    const btn = $("modalConfirm");
    btn.textContent = confirmLabel;
    btn.className = danger ? "btn danger" : "btn primary";
    const validity = $("modalValidity");
    let ticker = null;
    let settled = false;
    const previousFocus = document.activeElement;
    if (!dialog.open) dialog.showModal();
    // Safe default: focus Cancel so a stray Enter can never confirm a
    // destructive action; Enter only confirms when Confirm itself has focus.
    $("modalCancel").focus();
    promoteToastTopLayer();

    const cleanup = (ok) => {
      if (settled) return;
      settled = true;
      if (ticker !== null) clearInterval(ticker);
      ticker = null;
      btn.removeEventListener("click", onOk);
      $("modalCancel").removeEventListener("click", onCancel);
      dialog.removeEventListener("close", onDialogClose);
      if (dialog.open) dialog.close();
      if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus();
      resolve(ok);
    };
    const onOk = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onDialogClose = () => cleanup(false);
    btn.addEventListener("click", onOk);
    $("modalCancel").addEventListener("click", onCancel);
    dialog.addEventListener("close", onDialogClose);

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

export { confirmModal, formatCountdown };
