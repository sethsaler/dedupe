// Global keyboard map.

import { openHelp, closeHelp } from "./help.js";
import { closeLightbox, openLightbox } from "./lightbox.js";
import { changeMemberPage, reviewCandidate, selectGroup, trashReviewCandidate } from "./members.js";
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
      // Never let Tab escape an open overlay: when the trap has nothing to
      // cycle (controls briefly hidden mid-repaint), swallow the key instead.
      if (!trapTabKey($("helpBackdrop"), e)) e.preventDefault();
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
        // Same anti-escape guard as the help overlay: an empty moment in the
        // focusable set must not hand focus to the page behind the lightbox.
        if (!trapTabKey($("lightbox"), e)) e.preventDefault();
        return;
      }
      if (typing || e.target === $("lbVideo")) return;
    // A focused button handles Space/Enter natively (e.g. hold-to-flicker).
    const onButton = e.target?.tagName === "BUTTON";
    if (e.key === "ArrowLeft") {
      $("lbPrev").click();
      e.preventDefault();
    } else if (e.key === "ArrowRight") {
      $("lbNext").click();
      e.preventDefault();
    } else if (e.key === "z" || e.key === "Z") {
      if (!$("lbStageTools").hidden) $("lbZoom").click();
      e.preventDefault();
    } else if (e.key === "r" || e.key === "R") {
      $("lbReveal").click();
      e.preventDefault();
    } else if (e.key === " " && !onButton) {
      if (!$("lbSelectWrap").hidden) $("lbSelect").click();
      e.preventDefault();
    } else if (["d", "Delete", "Backspace"].includes(e.key)) {
      // Triage reviews trash in one click; duplicate groups toggle removal.
      if (!$("lbActions").hidden) $("lbDelete").click();
      else if (!$("lbSelectWrap").hidden) $("lbSelect").click();
      e.preventDefault();
    }
    return;
  }

  if (typing || $("results").hidden) return;

  if (e.metaKey || e.ctrlKey || e.altKey) return;

  // Enter/Space on a focused button activate it natively; don't also run the
  // global meaning. Member thumbnails are the exception: Space/Enter there
  // mean toggle-remove / open-lightbox and are handled (preventDefault) below.
  const onMemberThumb = Boolean(e.target?.closest?.("#members .thumb-wrap"));
  if (
    (e.key === " " || e.key === "Enter")
    && e.target?.tagName === "BUTTON"
    && !onMemberThumb
  ) return;

  if (e.key === "j" || e.key === "ArrowDown") {
    // In a decision review, ↓ steps to the next candidate without deciding;
    // j always moves between groups.
    if (e.key === "ArrowDown" && isDecisionReview(currentGroup())) changeMemberPage(1);
    else navGroup(1);
    e.preventDefault();
  } else if (e.key === "k" || e.key === "ArrowUp") {
    // ↑ goes back to the previous candidate in a decision review (its
    // decision stays as made; the opposite arrow key revises it).
    if (e.key === "ArrowUp" && isDecisionReview(currentGroup())) changeMemberPage(-1);
    else navGroup(-1);
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
  } else if (e.key === "A") {
    $("btnTrashSimilar").click();
    e.preventDefault();
  } else if (e.key === "D") {
    $("btnTrashReview").click();
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
  } else if ((e.key === "r" || e.key === "R") && state.currentId) {
    // Same Reveal as the card's button (and the lightbox's r): works in every
    // kind of group; decision-review cards have no Reveal control, so no-op.
    const cards = [...document.querySelectorAll("#members .card:not(.deleted)")];
    const card = document.querySelector("#members .card.focused:not(.deleted)") || cards[state.memberFocus] || cards[0];
    const reveal = card?.querySelector(".reveal");
    if (reveal) {
      reveal.click();
      e.preventDefault();
    }
  } else if (e.key === " " && state.currentId) {
    const cards = [...document.querySelectorAll("#members .card")];
    const card = document.querySelector("#members .card.focused") || cards[state.memberFocus] || cards[0];
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
    // Focus follows the card: screen readers announce it, and Space/Enter keep
    // working through the thumb button's handlers.
    cards[state.memberFocus]
      .querySelector(".thumb-wrap")
      ?.focus({ preventScroll: true });
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
