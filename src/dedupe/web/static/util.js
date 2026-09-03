// DOM helper, toasts, and formatting utilities.

const $ = (id) => document.getElementById(id);

// —— Toast ——
// One visible slot plus a small queue. Error toasts and action toasts (Undo)
// are sticky: they stay until dismissed or acted on, so a failure — or a
// reversible action — never becomes unavailable because a timer elapsed
// (WCAG 2.2.1). A new toast never destroys a sticky one; it queues behind it.
const toastQueue = [];
const TOAST_QUEUE_MAX = 4;
let toastStickyVisible = false;
let toastCurrentKey = "";

function hideToast() {
  const el = $("toast");
  el.classList.remove("show");
  clearTimeout(el._hideTimer);
  clearTimeout(el._fadeTimer);
  el._fadeTimer = setTimeout(() => {
    el.hidden = true;
    toastStickyVisible = false;
    toastCurrentKey = "";
    // Flush the queue only after the fade, so toasts don't overlap visually.
    if (toastQueue.length) showToast(toastQueue.shift());
  }, 220);
}

// Cycle Tab within an overlay (focus trap). Returns true when it handled the key.
function trapTabKey(container, e) {
  if (e.key !== "Tab" || !container) return false;
  const focusable = [...container.querySelectorAll(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((el) => el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  if (!focusable.length) return false;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    last.focus();
    e.preventDefault();
    return true;
  }
  if (!e.shiftKey && document.activeElement === last) {
    first.focus();
    e.preventDefault();
    return true;
  }
  // Focus can start outside the overlay (e.g. it was never moved in).
  if (!container.contains(document.activeElement)) {
    first.focus();
    e.preventDefault();
    return true;
  }
  return false;
}

function showToast({ msg, kind, actionLabel, onAction, duration }) {
  const el = $("toast");
  const message = $("toastMessage") || el;
  const action = $("toastAction");
  const dismiss = $("toastDismiss");
  message.textContent = msg;
  el.className = "toast" + (kind ? ` ${kind}` : "");
  if (action) {
    action.hidden = !actionLabel;
    action.textContent = actionLabel || "Undo";
    action.onclick = actionLabel && onAction
      ? () => {
          hideToast();
          onAction();
        }
      : null;
    el.classList.toggle("has-action", Boolean(actionLabel));
  }
  if (dismiss) dismiss.hidden = false;
  el.hidden = false;
  void el.offsetWidth;
  el.classList.add("show");
  clearTimeout(el._hideTimer);
  toastStickyVisible = duration === undefined && Boolean(actionLabel || kind === "error");
  toastCurrentKey = `${kind}|${msg}`;
  const ms = duration ?? (toastStickyVisible ? 0 : 3400);
  if (ms > 0) el._hideTimer = setTimeout(hideToast, ms);
}

function toast(msg, kind = "", { actionLabel, onAction, duration } = {}) {
  const item = { msg, kind, actionLabel, onAction, duration };
  const key = `${kind}|${msg}`;
  // A sticky toast (error / Undo) must survive until the user deals with it:
  // queue anything that arrives while one is up. Identical messages collapse.
  if (toastStickyVisible && !$("toast").hidden) {
    if (key === toastCurrentKey || toastQueue.some((queued) => `${queued.kind}|${queued.msg}` === key)) {
      return;
    }
    if (toastQueue.length >= TOAST_QUEUE_MAX) toastQueue.shift();
    toastQueue.push(item);
    return;
  }
  showToast(item);
}

$("toastDismiss")?.addEventListener("click", hideToast);

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export { $, hideToast, toast, trapTabKey, sleep, formatBytes, formatMtime, formatDuration, basename, setPreviewAspectRatio, escapeHtml };
