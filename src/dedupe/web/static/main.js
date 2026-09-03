// Entry point: wire modules, open the event stream, shut down on tab close.

import "./actions.js";
import "./groups.js";
import "./help.js";
import "./keyboard.js";
import "./lightbox.js";
import "./members.js";
import "./modal.js";
import "./scan.js";
import { collapseScanPanel, renderRecent } from "./settings.js";
import { CSRF_TOKEN } from "./state.js";
import { openEventStream, refreshStatus } from "./status.js";

// —— Init ——
renderRecent();
openEventStream();
refreshStatus()
  .then((s) => {
    // With a scan running or results already loaded, the setup form folds
    // away so the results get the screen; fresh pages keep it prominent.
    if (s?.scanning || s?.summary) collapseScanPanel();
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
