// Keyboard shortcut help overlay.

import { $ } from "./util.js";

// Focus is moved into the overlay on open and restored on close.
let previousFocus = null;

function openHelp() {
  previousFocus = document.activeElement;
  $("helpBackdrop").hidden = false;
  $("helpClose").focus();
}
function closeHelp() {
  $("helpBackdrop").hidden = true;
  if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus();
  previousFocus = null;
}
$("helpClose").addEventListener("click", closeHelp);
$("helpBackdrop").addEventListener("click", (e) => {
  if (e.target === $("helpBackdrop")) closeHelp();
});

export { openHelp, closeHelp };
