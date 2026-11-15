// Keyboard shortcut help overlay.

import { $, promoteToastTopLayer } from "./util.js";

// Focus is moved into the overlay on open and restored on close.
let previousFocus = null;

function openHelp() {
  const dialog = $("helpBackdrop");
  previousFocus = document.activeElement;
  if (!dialog.open) dialog.showModal();
  $("helpClose").focus();
  promoteToastTopLayer();
}
function closeHelp() {
  const dialog = $("helpBackdrop");
  if (dialog.open) dialog.close();
}
$("btnHelp").addEventListener("click", openHelp);
$("helpClose").addEventListener("click", closeHelp);
$("helpBackdrop").addEventListener("close", () => {
  if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus();
  previousFocus = null;
});

export { openHelp, closeHelp };
