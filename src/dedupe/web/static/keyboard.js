// Global keyboard map.

import { openHelp, closeHelp } from "./help.js";
import { closeLightbox, openLightbox } from "./lightbox.js";
import { reviewCandidate, selectGroup, trashReviewCandidate } from "./members.js";
import { currentGroup, groupNeedsAttention, isDecisionReview, isPagedIndependentReview } from "./model.js";
import { state } from "./state.js";
import { $, toast, trapTabKey } from "./util.js";

// —— Keyboard ——
document.addEventListener("keydown", async (e) => {
  const tag = (e.target && e.target.tagName) || "";
  const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target?.isContentEditable;

  if (e.key === "Escape") {
    if (!$("lightbox").hidden) {
      closeLightbox();
      e.preventDefault();
      return;
    }
    if (!$("helpBackdrop").hidden) {
      closeHelp();
      e.preventDefault();
      return;
    }
    if (!$("modalBackdrop").hidden) return; // handled by modal
  }

    if (!$("helpBackdrop").hidden && e.key === "Tab") {
      trapTabKey($("helpBackdrop"), e);
      return;
    }

    if (!$("modalBackdrop").hidden || !$("helpBackdrop").hidden) return;

  if (!typing && (e.key === "?" || (e.shiftKey && e.key === "/"))) {
    openHelp();
    e.preventDefault();
    return;
  }

    if (!$("lightbox").hidden) {
      if (e.key === "Tab") {
        trapTabKey($("lightbox"), e);
        return;
      }
      if (typing || e.target === $("lbVideo")) return;
    if (e.key === "ArrowLeft") {
      $("lbPrev").click();
      e.preventDefault();
    } else if (e.key === "ArrowRight") {
      $("lbNext").click();
      e.preventDefault();
    } else if (["d", "Delete", "Backspace"].includes(e.key)) {
      $("lbDelete").click();
      e.preventDefault();
    }
    return;
  }

  if (typing || $("results").hidden) return;

  if (e.metaKey || e.ctrlKey || e.altKey) return;

  if (e.key === "j" || e.key === "ArrowDown") {
    navGroup(1);
    e.preventDefault();
  } else if (e.key === "k" || e.key === "ArrowUp") {
    navGroup(-1);
    e.preventDefault();
  } else if (e.key === "]") {
    navAttention(1);
    e.preventDefault();
  } else if (e.key === "[") {
    navAttention(-1);
    e.preventDefault();
  } else if (e.key === "u" && state.currentId) {
    $("btnSelectSuggested").click();
    e.preventDefault();
  } else if (e.key === "s" && state.currentId) {
    $("btnSmartGroup").click();
    e.preventDefault();
  } else if (e.key === "a") {
    $("btnTrashExact").click();
    e.preventDefault();
  } else if (e.key === "Enter" && state.currentId) {
    const focused = document.querySelector("#members .card.focused .thumb-wrap");
    const index = Number(focused?.dataset.index ?? state.memberFocus ?? 0);
    openLightbox(Number.isFinite(index) ? index : 0);
    e.preventDefault();
  } else if (["d", "Delete", "Backspace"].includes(e.key) && state.currentId) {
    const group = currentGroup();
    if (!isPagedIndependentReview(group)) return;
    const cards = [...document.querySelectorAll("#members .card:not(.deleted)")];
    const card = document.querySelector("#members .card.focused:not(.deleted)") || cards[state.memberFocus] || cards[0];
    if (card?.dataset.path) {
      trashReviewCandidate(group, card.dataset.path);
      e.preventDefault();
    }
  } else if (e.key === " " && state.currentId) {
    const cards = [...document.querySelectorAll("#members .card")];
    const card = cards[state.memberFocus] || cards[0];
    if (card) {
      const cb = card.querySelector(".sel-cb");
      if (cb) {
        cb.checked = !cb.checked;
        cb.dispatchEvent(new Event("change"));
      }
    }
    e.preventDefault();
  } else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && state.currentId) {
    const current = currentGroup();
    if (isDecisionReview(current)) {
      const member = (current.members || [])[state.memberFocus];
      if (member) await reviewCandidate(current, member.path, e.key === "ArrowLeft");
      e.preventDefault();
      return;
    }
    const cards = document.querySelectorAll("#members .card");
    if (!cards.length) return;
    if (e.key === "ArrowRight") state.memberFocus = Math.min(cards.length - 1, state.memberFocus + 1);
    else state.memberFocus = Math.max(0, state.memberFocus - 1);
    cards.forEach((c) => c.classList.remove("focused"));
    cards[state.memberFocus].classList.add("focused");
    cards[state.memberFocus].scrollIntoView({ block: "nearest" });
    e.preventDefault();
  }
});

function navGroup(delta) {
  if (!state.groups.length) return;
  let idx = state.groups.findIndex((g) => g.id === state.currentId);
  if (idx < 0) idx = delta > 0 ? -1 : 0;
  idx = Math.max(0, Math.min(state.groups.length - 1, idx + delta));
  selectGroup(state.groups[idx].id).catch((e) => toast(e.message || String(e), "error"));
}

function navAttention(delta) {
  const candidates = state.groups.filter(groupNeedsAttention);
  if (!candidates.length) return toast("No shown groups need attention", "ok");
  const current = candidates.findIndex((group) => group.id === state.currentId);
  const next = current < 0
    ? (delta > 0 ? 0 : candidates.length - 1)
    : (current + delta + candidates.length) % candidates.length;
  selectGroup(candidates[next].id).catch((e) => toast(e.message || String(e), "error"));
}
