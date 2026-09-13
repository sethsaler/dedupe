// Swipe review for Similar groups: each undecided member is shown as a pair
// with the reference copy — reference on the left, candidate on the right.
// Left — drag, ←, d, or the Same button — calls it the same photo and moves
// the copy to Trash (undoable). Right — drag or → — records the pair as
// distinct, keeps both files, and hides that pair in future scans. Each
// decision cycles the next pair in until every copy has been addressed.

import { api } from "./api.js";
import { applyResultControls, loadGroups, selectionFiltersActive, updateGroupListItem } from "./groups.js";
import { openLightbox, updateLightbox } from "./lightbox.js";
import { selectGroup } from "./members.js";
import { currentGroup, patchGroup } from "./model.js";
import { scheduleRender } from "./render.js";
import { state } from "./state.js";
import { $, basename, escapeHtml, formatBytes, formatMtime, toast } from "./util.js";

const COMMIT_PX = 130; // horizontal travel that commits a swipe
const DRAG_GRAB_PX = 8; // movement before a horizontal drag engages
const FLICK_VELOCITY = 500; // px/s fast enough to commit without full travel

const reducedMotion = () =>
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

function swipeActive() {
  const g = currentGroup();
  return Boolean(g && g.kind === "similar" && state.similarView === "swipe");
}

// The reference copy the deck is compared against — the suggested keeper until
// the user re-anchors with "Use as reference".
function anchorFor(g) {
  const deleted = new Set(g.deleted_paths || []);
  const memberPaths = new Set((g.members || []).map((member) => member.path));
  const saved = state.swipeAnchors.get(g.id);
  if (saved && memberPaths.has(saved) && !deleted.has(saved)) return saved;
  const anchor = memberPaths.has(g.suggested_keep) && !deleted.has(g.suggested_keep)
    ? g.suggested_keep
    : (g.members || []).find((member) => !deleted.has(member.path))?.path;
  state.swipeAnchors.set(g.id, anchor);
  return anchor;
}

// Undecided members in deck order: live (not trashed) non-anchor members in
// the user's arrangement (skips reorder, undos reinsert) over server order.
function deckFor(g) {
  const deleted = new Set(g.deleted_paths || []);
  const anchor = anchorFor(g);
  const live = (g.members || [])
    .map((member) => member.path)
    .filter((path) => path !== anchor && !deleted.has(path));
  const saved = state.swipeDeckOrder.get(g.id);
  if (!saved) return live;
  const liveSet = new Set(live);
  const ordered = saved.filter((path) => liveSet.has(path));
  for (const path of live) if (!ordered.includes(path)) ordered.push(path);
  state.swipeDeckOrder.set(g.id, ordered);
  return ordered;
}

function undoStackFor(groupId) {
  let stack = state.swipeUndo.get(groupId);
  if (!stack) {
    stack = [];
    state.swipeUndo.set(groupId, stack);
  }
  return stack;
}

function restoreToDeck(groupId, path) {
  const order = state.swipeDeckOrder.get(groupId) || [];
  if (!order.includes(path)) order.unshift(path);
  state.swipeDeckOrder.set(groupId, order);
}

function previewUrl(path) {
  return `/api/thumbnail?path=${encodeURIComponent(path)}&variant=preview`;
}

function memberByPath(g, path) {
  return (g.members || []).find((member) => member.path === path);
}

function memberMeta(member) {
  const dims = Number.isFinite(member.width) && Number.isFinite(member.height)
    && member.width > 0 && member.height > 0
    ? `${member.width}×${member.height}`
    : "—";
  return [formatBytes(member.size), dims, formatMtime(member.mtime)].join(" · ");
}

function mediaHtml(member) {
  const fileName = basename(member.path);
  const playBadge = ["video", "gif"].includes(member.media_type)
    ? '<span class="swipe-play" aria-hidden="true">▶</span>'
    : "";
  return `<img src="${previewUrl(member.path)}" alt="Preview of ${escapeHtml(fileName)}" draggable="false" decoding="async" />${playBadge}`;
}

// Similarity is computed against the suggested keeper; after a re-anchor the
// number no longer describes the on-screen pair, so the chip falls back to a
// neutral label.
function similarityChip(member, g, anchor) {
  if (anchor !== g.suggested_keep) {
    return '<span class="swipe-sim" title="Fingerprint scores measure against the suggested keeper, not your chosen reference">vs your reference</span>';
  }
  const value = member.similarity_percent == null ? null : Number(member.similarity_percent);
  if (!Number.isFinite(value)) {
    return '<span class="swipe-sim">score unavailable</span>';
  }
  return `<span class="swipe-sim" title="Fingerprint agreement with the reference copy, not a probability">${value.toFixed(1).replace(/\.0$/, "")}% match</span>`;
}

// One side of the pair: a media pane with a caption. The candidate side is
// also the draggable .swipe-card; the keeper side is static.
function sideHtml(member, { tag, tagClass = "", card = false, meta = "", extraClass = "" }) {
  const fileName = basename(member.path);
  return `
    <figure class="swipe-side ${extraClass}${card ? " swipe-card swipe-top" : ""}"${card ? ` data-path="${escapeHtml(member.path)}"` : ""}>
      <button class="swipe-media${card ? "" : " swipe-keeper-media"}" type="button" aria-label="Open ${escapeHtml(fileName)} in the comparison view">
        ${mediaHtml(member)}
        ${tag ? `<span class="swipe-tag ${tagClass}">${tag}</span>` : ""}
        ${card ? `
        <span class="swipe-flag same" aria-hidden="true">← Same — Trash copy</span>
        <span class="swipe-flag diff" aria-hidden="true">Different — keep both →</span>` : ""}
      </button>
      <figcaption class="swipe-info">
        <div class="swipe-name-row">
          <span class="name" title="${escapeHtml(member.path)}">${escapeHtml(fileName)}</span>
          ${meta}
        </div>
        <div class="path" title="${escapeHtml(member.path)}">${escapeHtml(member.path)}</div>
        <div class="card-meta">${memberMeta(member)}</div>
        ${card ? `
        <div class="swipe-card-tools">
          <button class="linkish swipe-reanchor" type="button" title="Make this copy the reference the rest of the queue is compared against">Use as reference</button>
          <button class="linkish reveal" data-path="${escapeHtml(member.path)}" type="button">Reveal</button>
        </div>` : ""}
      </figcaption>
    </figure>`;
}

function renderSwipeReview(g) {
  const box = $("members");
  box.classList.remove("triage-grid");
  for (const id of ["memberPagination", "memberPaginationBottom", "memberSort", "memberModified"]) {
    const el = $(id);
    if (el) el.hidden = true;
  }

  const anchor = anchorFor(g);
  const anchorMember = memberByPath(g, anchor);
  const deck = deckFor(g);
  const members = g.members || [];
  const trashed = new Set(g.deleted_paths || []);
  const trashedCount = members.filter((member) => trashed.has(member.path)).length;
  const undoStack = undoStackFor(g.id);
  // A decided pair leaves the group (distinct) or lands in deleted_paths
  // (Trash), so progress comes from the undo stack plus trashed members —
  // trashed entries are counted once even though they sit in both places.
  const trashedInStack = undoStack.filter((entry) => entry.action === "trash").length;
  const decided = undoStack.length + trashedCount - trashedInStack;
  const total = decided + deck.length;

  // The lightbox compares each candidate against the reference; the reference
  // itself sits at index 0 so its panel opens the full view too.
  const lightboxEntry = (member, keeper) => ({
    path: member.path,
    mediaType: member.media_type,
    keeper,
    kind: g.kind,
    size: member.size,
    width: member.width,
    height: member.height,
    mtime: member.mtime,
    similarityPercent: member.similarity_percent,
  });
  state.lightboxItems = [
    ...(anchorMember ? [lightboxEntry(anchorMember, anchor)] : []),
    ...deck.map((path) => lightboxEntry(memberByPath(g, path), anchor)),
  ];

  const done = deck.length === 0;
  $("detailMeta").textContent = done
    ? `${decided} compared · ${trashedCount} in Trash · the reference stays on disk`
    : `${decided} of ${total} compared · ${trashedCount} in Trash · ← same photo → different`;

  const candidate = done ? null : memberByPath(g, deck[0]);

  const pairHtml = done
    ? `<div class="swipe-done">
        <strong>All ${decided} ${decided === 1 ? "copy" : "copies"} reviewed</strong>
        <p class="muted">${trashedCount ? `${trashedCount} moved to Trash · ` : ""}the reference and every different photo stay on disk.</p>
        <div class="swipe-done-actions">
          ${undoStack.length ? '<button class="btn ghost" id="swipeUndoDone" type="button">Undo last decision</button>' : ""}
          <button class="btn primary-soft" id="swipeNext" type="button">Next group</button>
        </div>
      </div>`
    : `<div class="swipe-pair">
        ${anchorMember ? sideHtml(anchorMember, {
          tag: `Reference — stays${anchor !== g.suggested_keep ? " · your pick" : ""}`,
          tagClass: "keeper",
          extraClass: "swipe-keeper",
        }) : ""}
        <div class="swipe-vs">
          <button class="swipe-btn swipe-same" id="swipeSame" type="button"
                  title="Same photo — this copy moves to Trash (undoable)">
            <kbd>←</kbd><strong>Same</strong>
          </button>
          <span class="swipe-vs-badge" aria-hidden="true">vs</span>
          <button class="swipe-btn swipe-diff" id="swipeDiff" type="button"
                  title="Different — keep both, the pair never re-matches">
            <strong>Different</strong><kbd>→</kbd>
          </button>
        </div>
        ${sideHtml(candidate, { card: true, meta: similarityChip(candidate, g, anchor) })}
      </div>
      <div class="swipe-mid">
        <span class="swipe-progress">${decided} / ${total} reviewed${deck.length > 1 ? ` · ${deck.length - 1} after this` : ""}</span>
        <button class="linkish" id="swipeSkip" type="button" ${deck.length < 2 ? "disabled" : ""} title="Decide later — send this copy to the back of the queue">Skip</button>
        <button class="linkish" id="swipeUndo" type="button" ${undoStack.length ? "" : "disabled"}>Undo</button>
      </div>
      <p class="swipe-hint muted"><kbd>←</kbd> same photo (Trash) · <kbd>→</kbd> different (keep both) · drag the copy too · <kbd>Enter</kbd> compares closely · <kbd>⌫</kbd> undoes</p>`;

  box.innerHTML = `<div class="swipe-review">${pairHtml}</div>`;
  wireSwipeDom(g);
}

function wireSwipeDom(g) {
  const box = $("members");
  box.querySelector(".swipe-keeper-media")?.addEventListener("click", () => openLightbox(0));
  box.querySelector("#swipeSame")?.addEventListener("click", () => decideSwipe("same"));
  box.querySelector("#swipeDiff")?.addEventListener("click", () => decideSwipe("distinct"));
  box.querySelector("#swipeSkip")?.addEventListener("click", skipSwipe);
  box.querySelector("#swipeUndo")?.addEventListener("click", undoSwipe);
  box.querySelector("#swipeUndoDone")?.addEventListener("click", undoSwipe);
  box.querySelector("#swipeNext")?.addEventListener("click", advanceToNextGroup);
  box.querySelector(".swipe-reanchor")?.addEventListener("click", reanchor);
  box.querySelectorAll(".reveal").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        await api(`/api/reveal?path=${encodeURIComponent(btn.dataset.path)}&open=1`);
      } catch (error) {
        toast(error.message, "error");
      }
    });
  });
  const top = box.querySelector(".swipe-top");
  if (top) {
    // A drag that committed suppresses the click that follows pointerup.
    top.querySelector(".swipe-media")?.addEventListener("click", () => {
      if (!top.dataset.dragged) openLightbox(1); // index 0 is the reference
    });
    wireDrag(g, top);
  }
}

// —— Drag physics ——
// 1:1 pointer tracking with a small grab threshold, velocity projection on
// release (a flick commits early), and a spring home when the swipe bails.

// Minimal spring integrator; returns a cancel function. doneWhen defaults to
// "settled at the target" — fly-offs instead pass "past the target".
function spring({ from, to, velocity = 0, stiffness = 230, damping = 20, onUpdate, doneWhen, onDone }) {
  let x = from;
  let v = velocity;
  let last = null;
  let raf = requestAnimationFrame(function step(t) {
    if (last == null) last = t;
    const dt = Math.min(0.032, (t - last) / 1000);
    last = t;
    v += (-stiffness * (x - to) - damping * v) * dt;
    x += v * dt;
    onUpdate(x, v);
    const done = doneWhen ? doneWhen(x, v) : Math.abs(x - to) < 0.5 && Math.abs(v) < 12;
    if (done) {
      onDone?.();
      return;
    }
    raf = requestAnimationFrame(step);
  });
  return () => cancelAnimationFrame(raf);
}

function wireDrag(g, card) {
  const media = card.querySelector(".swipe-media");
  if (!media) return;
  let drag = null;
  let cancelAnimation = null;

  const setOffset = (dx) => {
    const rotate = Math.max(-14, Math.min(14, dx / 18));
    card.style.transform = `translateX(${dx}px) rotate(${rotate}deg)`;
    card.style.setProperty("--swipe", String(Math.max(-1, Math.min(1, dx / COMMIT_PX))));
  };

  const snapBack = (dx, velocity) => {
    if (reducedMotion()) {
      setOffset(0);
      delete card.dataset.dragged;
      return;
    }
    cancelAnimation = spring({
      from: dx,
      to: 0,
      velocity,
      stiffness: 260,
      damping: 19,
      onUpdate: setOffset,
      onDone: () => {
        setOffset(0);
        delete card.dataset.dragged;
        cancelAnimation = null;
      },
    });
  };

  const flyOff = (direction, dx, velocity) => {
    const target = (direction === "same" ? -1 : 1) * Math.max(card.offsetWidth + 120, window.innerWidth * 0.55);
    if (reducedMotion()) {
      card.style.opacity = "0";
      decideSwipe(direction);
      return;
    }
    cancelAnimation = spring({
      from: dx,
      to: target,
      velocity: Math.abs(velocity) > 240 ? velocity : Math.sign(target) * 1500,
      stiffness: 90,
      damping: 16,
      onUpdate: (x) => {
        setOffset(x);
        card.style.opacity = String(Math.max(0.15, 1 - (Math.abs(x) / Math.abs(target)) * 0.85));
      },
      doneWhen: (x) => Math.abs(x) >= Math.abs(target) * 0.92,
      onDone: () => decideSwipe(direction),
    });
  };

  media.addEventListener("pointerdown", (event) => {
    if (event.button !== undefined && event.button !== 0) return;
    cancelAnimation?.();
    cancelAnimation = null;
    delete card.dataset.dragged;
    drag = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      engaged: false,
      dx: 0,
      samples: [{ x: event.clientX, t: performance.now() }],
    };
    media.setPointerCapture(event.pointerId);
  });

  media.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    drag.samples.push({ x: event.clientX, t: performance.now() });
    if (drag.samples.length > 6) drag.samples.shift();
    if (!drag.engaged) {
      // Direction lock: commit to horizontal only once intent is clear.
      if (Math.abs(dx) > DRAG_GRAB_PX && Math.abs(dx) > Math.abs(dy)) {
        drag.engaged = true;
        card.classList.add("dragging");
      } else if (Math.abs(dy) > DRAG_GRAB_PX * 2) {
        drag = null; // vertical scroll wins; release the gesture
        return;
      }
      return;
    }
    event.preventDefault();
    card.dataset.dragged = "1";
    drag.dx = dx;
    setOffset(dx);
  });

  const finish = (event, cancelled) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const { engaged, dx, samples } = drag;
    drag = null;
    card.classList.remove("dragging");
    if (!engaged) return; // a click — the media button handles the lightbox
    const newest = samples[samples.length - 1];
    const oldest = samples[0];
    const elapsed = Math.max(1, newest.t - oldest.t);
    const velocity = ((newest.x - oldest.x) / elapsed) * 1000; // px/s
    if (cancelled) {
      snapBack(dx, velocity);
      return;
    }
    // Apple's momentum projection: decide from where the gesture is going,
    // not where it stopped.
    const projected = dx + (velocity / 1000) * 0.998 / (1 - 0.998);
    if (Math.abs(projected) > COMMIT_PX || Math.abs(velocity) > FLICK_VELOCITY) {
      flyOff(dx < 0 ? "same" : "distinct", dx, velocity);
    } else {
      snapBack(dx, velocity);
    }
  };

  media.addEventListener("pointerup", (event) => finish(event, false));
  media.addEventListener("pointercancel", (event) => finish(event, true));
}

// —— Decisions ——

function refreshAfterDecision(g) {
  // If the lightbox is open its item list was just rebuilt; keep it aimed at
  // the file the user is looking at (or the next one when it was decided).
  const openPath = state.lightboxItems[state.lightboxIndex]?.path;
  const live = currentGroup();
  if (live && live.id === g.id) renderSwipeReview(live);
  if (!$("lightbox").hidden && state.lightboxItems.length) {
    const idx = state.lightboxItems.findIndex((item) => item.path === openPath);
    state.lightboxIndex = idx >= 0
      ? idx
      : Math.min(state.lightboxIndex, state.lightboxItems.length - 1);
    updateLightbox();
  }
  if (selectionFiltersActive() || !updateGroupListItem(g)) {
    scheduleRender({ groupList: true });
  } else {
    applyResultControls();
  }
  scheduleRender({ selection: true });
}

// Decide one card — the deck's top card by default, or a specific member when
// the lightbox is open on it. Only live deck members are decidable; the
// reference and already-decided paths no-op.
async function decideSwipe(direction, path = null) {
  const g = currentGroup();
  if (!swipeActive() || !g) return;
  const deck = deckFor(g);
  const target = path || deck[0];
  if (!target || !deck.includes(target)) return;
  if (state.swipeBusy) {
    // Held arrow keys repeat faster than the round-trip: keep the latest.
    state.pendingSwipeDecision = { direction, path: target };
    return;
  }
  state.swipeBusy = true;
  const anchor = anchorFor(g);
  undoStackFor(g.id).push({ action: direction === "same" ? "trash" : "distinct", path: target, anchor });
  try {
    if (direction === "same") {
      const updated = await api("/api/review-candidate/delete", {
        method: "POST",
        body: JSON.stringify({
          group_id: g.id,
          path: target,
          scan_id: state.scanId,
          dry_run: false,
        }),
      });
      patchGroup(updated);
      refreshAfterDecision(updated);
      toast(`Same photo — moved ${basename(target)} to Trash`, "ok", {
        actionLabel: "Undo",
        onAction: () => undoSwipe(),
      });
    } else {
      const res = await api("/api/similar/mark-distinct", {
        method: "POST",
        body: JSON.stringify({
          group_id: g.id,
          path: target,
          anchor,
          scan_id: state.scanId,
        }),
      });
      if (res.dissolved || !res.group) {
        toast("Different — pair marked, group finished", "ok");
        await loadGroups();
        return;
      }
      patchGroup(res.group);
      refreshAfterDecision(res.group);
      toast("Different — both copies stay, the pair never re-matches", "ok", {
        actionLabel: "Undo",
        onAction: () => undoSwipe(),
      });
    }
  } catch (error) {
    const stack = state.swipeUndo.get(g.id);
    const index = stack ? stack.findIndex((entry) => entry.path === target) : -1;
    if (index >= 0) stack.splice(index, 1);
    toast(error.message || "Could not apply that decision", "error");
    const live = currentGroup();
    if (live && live.id === g.id) renderSwipeReview(live);
  } finally {
    state.swipeBusy = false;
    const pending = state.pendingSwipeDecision;
    state.pendingSwipeDecision = null;
    if (pending) await decideSwipe(pending.direction, pending.path);
  }
}

async function undoSwipe() {
  const g = currentGroup();
  if (!g || g.kind !== "similar") return;
  const stack = state.swipeUndo.get(g.id);
  const entry = stack ? stack[stack.length - 1] : null;
  if (!entry) return;
  try {
    if (entry.action === "trash") {
      const updated = await api("/api/review-candidate/undo", {
        method: "POST",
        body: JSON.stringify({ group_id: g.id, path: entry.path, scan_id: state.scanId }),
      });
      patchGroup(updated);
      restoreToDeck(g.id, entry.path);
      stack.pop();
      refreshAfterDecision(updated);
      toast("Image restored", "ok");
    } else {
      const res = await api("/api/similar/unmark-distinct", {
        method: "POST",
        body: JSON.stringify({
          group_id: g.id,
          path: entry.path,
          anchor: entry.anchor,
          scan_id: state.scanId,
        }),
      });
      stack.pop();
      if (res.group) {
        patchGroup(res.group);
        restoreToDeck(g.id, entry.path);
        refreshAfterDecision(res.group);
        toast("Back in the deck — the pair can match again", "ok");
      } else {
        toast("Decision reverted — the pair may reappear on the next scan", "ok");
      }
    }
  } catch (error) {
    toast(error.message || "Could not undo that decision", "error");
  }
}

function skipSwipe() {
  const g = currentGroup();
  if (!g) return;
  const deck = deckFor(g);
  if (deck.length < 2) return;
  state.swipeDeckOrder.set(g.id, [...deck.slice(1), deck[0]]);
  renderSwipeReview(g);
}

// "Use as reference": the top card becomes the anchor; the old reference goes
// back to the top of the deck to be judged against it.
function reanchor() {
  const g = currentGroup();
  if (!g) return;
  const deck = deckFor(g);
  const candidate = deck[0];
  const oldAnchor = anchorFor(g);
  if (!candidate || !oldAnchor) return;
  state.swipeAnchors.set(g.id, candidate);
  state.swipeDeckOrder.set(g.id, [oldAnchor, ...deck.slice(1)]);
  renderSwipeReview(g);
  toast(`${basename(candidate)} is now the reference`, "ok");
}

function advanceToNextGroup() {
  const index = state.groups.findIndex((group) => group.id === state.currentId);
  const next = state.groups[index + 1] || state.groups.find((group) => group.id !== state.currentId);
  if (!next) {
    toast("No more groups in this view", "ok");
    return;
  }
  selectGroup(next.id).catch((e) => toast(e.message || String(e), "error"));
}

export { renderSwipeReview, decideSwipe, undoSwipe, skipSwipe, swipeActive };
