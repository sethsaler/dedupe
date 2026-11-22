// Entry point: wire modules, open the event stream, shut down on tab close.

import "./actions.js";
import "./board.js";
import "./groups.js";
import "./help.js";
import "./hoverpreview.js";
import "./keyboard.js";
import "./lightbox.js";
import "./members.js";
import "./modal.js";
import "./scan.js";
import "./jobs.js";
import { initHistory } from "./history.js";
import { applySettingsToForm, collapseScanPanel, renderRecent, scanGoalShortLabel } from "./settings.js";
import { CSRF_TOKEN } from "./state.js";
import { openEventStream, refreshStatus } from "./status.js";
import { initModernUI } from "./theme.js";
import { $ } from "./util.js";

// —— Init ——
initModernUI();
renderRecent();
initHistory();
openEventStream();
refreshStatus()
  .then((s) => {
    if ((s?.scanning || s?.has_result || s?.summary?.group_count > 0) && s?.scan_settings && Object.keys(s.scan_settings).length) {
      applySettingsToForm(s.scan_settings);
    }
    // With a scan running or results already loaded, the setup form folds
    // away so the results get the screen; fresh pages keep it prominent.
    if (s?.scanning || s?.summary?.group_count > 0 || !$("results").hidden) {
      const paths = (s?.scan_settings?.paths || s?.review_session?.roots || []).join(", ");
      collapseScanPanel(paths, s?.scan_settings ? scanGoalShortLabel(s.scan_settings) : "");
    }
  })
  .catch(() => {});

// Shut down the server when the tab is closed so the Terminal/.command window closes too.
// sendBeacon cannot carry the X-Dedupe-Token header, so use fetch with keepalive,
// which survives page teardown and passes the CSRF check.
window.addEventListener("pagehide", (event) => {
  if (event.persisted) return;
  fetch("/api/shutdown", {
    method: "POST",
    keepalive: true,
    headers: {
      "Content-Type": "application/json",
      "X-Dedupe-Token": CSRF_TOKEN,
    },
    body: "{}",
  }).catch(() => {});
});
