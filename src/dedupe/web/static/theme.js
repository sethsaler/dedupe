// Progressive-enhancement helpers: theme toggle, range progress fallback,
// closedby="any" dialog fallback, and horizontal tab fade fallback.

const THEME_STORAGE_KEY = "dedupe.theme";
const THEME_CYCLE = ["system", "light", "dark"];
const THEME_LABELS = { system: "Auto", light: "Light", dark: "Dark" };
const THEME_NAMES = { system: "System", light: "Light", dark: "Dark" };
const THEME_ICONS = { system: "◐", light: "☀", dark: "☾" };

function storedTheme() {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    return saved === "light" || saved === "dark" ? saved : "system";
  } catch {
    return "system";
  }
}

function applyThemeChoice(choice) {
  const root = document.documentElement;
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (choice === "light" || choice === "dark") {
    root.dataset.theme = choice;
    if (meta) meta.setAttribute("content", `only ${choice}`);
  } else {
    delete root.dataset.theme;
    if (meta) meta.setAttribute("content", "light dark");
  }
  const btn = document.getElementById("btnTheme");
  const label = document.getElementById("themeLabel");
  const icon = btn?.querySelector(".theme-icon");
  if (label) label.textContent = THEME_LABELS[choice];
  if (icon) icon.textContent = THEME_ICONS[choice];
  if (btn) {
    btn.setAttribute("aria-label", `Color theme: ${THEME_NAMES[choice]}`);
    btn.title = `Switch color theme (${THEME_NAMES[choice]})`;
  }
}

function initThemeSwitcher() {
  applyThemeChoice(storedTheme());
  const btn = document.getElementById("btnTheme");
  btn?.addEventListener("click", () => {
    const current = storedTheme();
    const next = THEME_CYCLE[(THEME_CYCLE.indexOf(current) + 1) % THEME_CYCLE.length];
    try {
      if (next === "system") localStorage.removeItem(THEME_STORAGE_KEY);
      else localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Ignore storage failures in restricted contexts.
    }
    applyThemeChoice(next);
  });
  window.addEventListener("storage", (e) => {
    if (e.key === THEME_STORAGE_KEY) applyThemeChoice(storedTheme());
  });
}

function syncRangeInput(input) {
  if (!input || input.type !== "range") return;
  const min = Number(input.min || 0);
  const max = Number(input.max || 100);
  const value = Number(input.value || 0);
  const span = max - min;
  const pct = span > 0 ? Math.max(0, Math.min(100, ((value - min) / span) * 100)) : 0;
  input.style.setProperty("--attr-min", String(min));
  input.style.setProperty("--attr-max", String(max));
  input.style.setProperty("--control-value", String(value));
  input.style.setProperty("--slider-pct", `${pct}%`);
}

function initRangeSliders() {
  document.querySelectorAll('input[type="range"]').forEach(syncRangeInput);
  const onRangeEvent = (e) => {
    if (e.target instanceof HTMLInputElement && e.target.type === "range") {
      syncRangeInput(e.target);
    }
  };
  document.addEventListener("input", onRangeEvent);
  document.addEventListener("change", onRangeEvent);
}

function initDialogLightDismissFallback() {
  if ("closedBy" in HTMLDialogElement.prototype) return;
  for (const id of ["modalBackdrop", "helpBackdrop", "historyPanel"]) {
    const dialog = document.getElementById(id);
    dialog?.addEventListener("click", (e) => {
      if (e.target === dialog && dialog.open) dialog.close();
    });
  }
}

function initTabScrollFadesFallback() {
  if (typeof CSS !== "undefined" && CSS.supports?.("animation-timeline", "scroll()")) return;
  const tabs = document.querySelector(".filter-tabs");
  if (!tabs) return;
  const sync = () => {
    const maxScroll = tabs.scrollWidth - tabs.clientWidth;
    tabs.classList.toggle("fade-left", tabs.scrollLeft > 2);
    tabs.classList.toggle("fade-right", maxScroll > 2 && tabs.scrollLeft < maxScroll - 2);
  };
  tabs.addEventListener("scroll", sync, { passive: true });
  window.addEventListener("resize", sync);
  if (typeof ResizeObserver === "function") new ResizeObserver(sync).observe(tabs);
  sync();
}

function initModernUI() {
  initThemeSwitcher();
  initRangeSliders();
  initDialogLightDismissFallback();
  initTabScrollFadesFallback();
}

export { applyThemeChoice, initModernUI, syncRangeInput };
