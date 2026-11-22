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
let toastCurrentKind = "";
let toastRemainingMs = 0;
let toastDeadline = 0;
let toastFocusBound = false;

function isOverlayOpen(el) {
  if (!el) return false;
  if (el.open === true) return true;
  try {
    return el.matches(":popover-open") || el.classList.contains(":popover-open");
  } catch {
    return el.classList.contains(":popover-open");
  }
}

function syncAppInert() {
  const app = document.querySelector(".app");
  if (!app) return;
  app.inert = isOverlayOpen($("lightbox")) || isOverlayOpen($("cropEditor"));
}

function promoteToastTopLayer() {
  const el = $("toast");
  if (!el || !isOverlayOpen(el)) return;
  el.hidePopover?.();
  el.showPopover?.();
}

function announceToast(msg, kind = "") {
  if (typeof document.ariaNotify === "function") {
    document.ariaNotify(msg);
    return;
  }
  const live = (kind === "error" && $("toastAlertRegion")) || $("toastLiveRegion");
  if (!live) return;
  live.textContent = "";
  requestAnimationFrame(() => {
    live.textContent = msg;
  });
}

function startToastTimer(el, ms) {
  clearTimeout(el._hideTimer);
  if (!(ms > 0)) {
    toastRemainingMs = 0;
    toastDeadline = 0;
    return;
  }
  toastRemainingMs = ms;
  toastDeadline = Date.now() + ms;
  el._hideTimer = setTimeout(hideToast, ms);
}

function bindToastFocusPause(el) {
  if (!el || toastFocusBound) return;
  toastFocusBound = true;
  el.addEventListener("focusin", () => {
    if (toastStickyVisible || !isOverlayOpen(el)) return;
    clearTimeout(el._hideTimer);
    if (toastDeadline > 0) {
      toastRemainingMs = Math.max(1200, toastDeadline - Date.now());
    }
  });
  el.addEventListener("focusout", (e) => {
    if (toastStickyVisible || !isOverlayOpen(el)) return;
    if (e.relatedTarget && el.contains(e.relatedTarget)) return;
    if (toastRemainingMs > 0) startToastTimer(el, toastRemainingMs);
  });
}

function hideToast() {
  const el = $("toast");
  if (!el) return;
  el.classList.remove("show");
  clearTimeout(el._hideTimer);
  clearTimeout(el._fadeTimer);
  toastRemainingMs = 0;
  toastDeadline = 0;
  if (isOverlayOpen(el)) el.hidePopover?.();
  el._fadeTimer = setTimeout(() => {
    toastStickyVisible = false;
    toastCurrentKey = "";
    toastCurrentKind = "";
    // Flush the queue only after the exit fade, so toasts don't overlap visually.
    if (toastQueue.length) showToast(toastQueue.shift());
  }, 150);
}

// Cycle Tab within an overlay (focus trap). Returns true when Tab stays inside
// the container (either handled by wrapping/entering or allowed to step natively).
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
  return true;
}

function showToast({ msg, kind, actionLabel, onAction, duration }) {
  const el = $("toast");
  if (!el) return;
  const message = $("toastMessage") || el;
  const action = $("toastAction");
  const dismiss = $("toastDismiss");
  bindToastFocusPause(el);
  clearTimeout(el._fadeTimer);
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
  if (isOverlayOpen(el)) el.hidePopover?.();
  el.showPopover?.();
  void el.offsetWidth;
  el.classList.add("show");
  toastCurrentKind = kind || "";
  announceToast(msg, toastCurrentKind);
  toastStickyVisible = duration === undefined && Boolean(actionLabel || kind === "error");
  toastCurrentKey = `${kind}|${msg}`;
  const ms = duration ?? (toastStickyVisible ? 0 : 3400);
  startToastTimer(el, ms);
}

function toast(msg, kind = "", { actionLabel, onAction, duration } = {}) {
  const item = { msg, kind, actionLabel, onAction, duration };
  const key = `${kind}|${msg}`;
  // Sticky error toasts queue newer distinct toasts behind them (collapsing
  // repeats). Non-error sticky Undo toasts are immediately preempted by errors
  // and immediately replaced by newer Undo actions so rapid triage stays current.
  if (toastStickyVisible && isOverlayOpen($("toast"))) {
    if (toastCurrentKind !== "error" && (kind === "error" || Boolean(actionLabel))) {
      showToast(item);
      return;
    }
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

// Files rewritten in place (a crop) keep their path, and thumbnails are served
// with immutable cache headers. A per-path version busts the browser cache.
const mediaVersions = new Map();

function noteMediaVersion(path, version) {
  mediaVersions.set(path, String(version));
}

function withMediaVersion(url, path, fallbackVersion = "") {
  const version = mediaVersions.get(path) || fallbackVersion;
  return version ? `${url}&v=${encodeURIComponent(version)}` : url;
}

export { $, hideToast, toast, trapTabKey, isOverlayOpen, promoteToastTopLayer, syncAppInert, sleep, formatBytes, formatMtime, formatDuration, basename, setPreviewAspectRatio, escapeHtml, noteMediaVersion, withMediaVersion };
