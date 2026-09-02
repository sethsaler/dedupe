// Entry point: wire modules, open the event stream, shut down on tab close.

import "./actions.js";
import "./groups.js";
import "./help.js";
import "./keyboard.js";
import "./lightbox.js";
import "./members.js";
import "./modal.js";
import "./scan.js";
import { renderRecent } from "./settings.js";
import { CSRF_TOKEN } from "./state.js";
import { openEventStream, refreshStatus } from "./status.js";

// —— Init ——
renderRecent();
openEventStream();
refreshStatus().catch(() => {});

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
