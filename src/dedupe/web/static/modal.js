// The preview-and-confirm sheet.

import { $, trapTabKey } from "./util.js";

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
    const previousFocus = document.activeElement;
    $("modalBackdrop").hidden = false;
    // Safe default: focus Cancel so a stray Enter can never confirm a
    // destructive action; Enter only confirms when Confirm itself has focus.
    $("modalCancel").focus();

    const cleanup = (ok) => {
      if (ticker !== null) clearInterval(ticker);
      ticker = null;
      $("modalBackdrop").hidden = true;
      btn.removeEventListener("click", onOk);
      $("modalCancel").removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey);
      if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus();
      resolve(ok);
    };
    const onOk = () => cleanup(true);
    const onCancel = () => cleanup(false);
      const onKey = (e) => {
        if (e.key === "Escape") cleanup(false);
        if (e.key === "Enter" && document.activeElement === btn) cleanup(true);
        trapTabKey($("modalBackdrop"), e);
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

export { confirmModal, formatCountdown };
