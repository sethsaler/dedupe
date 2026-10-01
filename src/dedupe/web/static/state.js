// Shared client state and the per-launch CSRF token."""


const CSRF_TOKEN =
  document.querySelector('meta[name="dedupe-token"]')?.getAttribute("content") || "";
// Sidebar renders at most this many rows initially (state.groupListLimit).
const GROUP_RENDER_CHUNK = 50;

const state = {
  kind: "all",
  groups: [],
  allGroups: [],
  currentId: null,
  pollTimer: null,
  eventSource: null,
  eventFailures: 0,
  memberFocus: 0,
  // Last loaded 50-card batch, or the candidate index in decision review.
  memberPage: 0,
  // Per-kind member ordering; the default entry is each kind's server order.
  memberSortByKind: { faces: "faces-desc", all_files: "path" },
  // Modified-time window for the Files tab's member list ("any" = no filtering).
  memberModified: "any",
  reviewView: "gallery",
  lightboxItems: [],
  lightboxIndex: 0,
  scanning: false,
  acting: false,
  actionBusy: false, // a file action run by this tab is in flight
  cpuCount: 0,
  autoWorkers: 0,
  capabilities: null,
  keepDecisionsError: null,
  trashUndoClearedNotified: false,
  autoDeleteNotified: null,
  dismissedSessionKey: "",
  emptyResumeNotified: false,
  groupsVersion: -1, // tracks streaming updates mid-scan
  scanId: null,
  reviewSession: null,
  groupListStart: 0, // first sidebar row in the rendered window
  groupListLimit: GROUP_RENDER_CHUNK, // how many sidebar rows are in the DOM
  groupsLoadToken: 0,
  selectToken: 0,
  pollFailures: 0,
  reviewingCandidate: false,
  pendingReviewDecision: null,
  showDeleted: false,
  deleteBusy: new Set(),
  // Paths trashed on the current triage page: their cards stay in place as
  // "Moved to Trash" placeholders so the grid never reflows mid-review.
  trashedInPlace: new Set(),
  // Group ids whose selection the user changed; others show the suggestion.
  touchedGroups: new Set(),
  // Similar review layout: "board" (every group as a row, Similar tab only),
  // "swipe" (pair deck), or "grid" (classic cards). Outside the Similar tab a
  // "board" preference opens single groups in the swipe deck.
  similarView: "board",
  // Similar board: rows in the DOM, the focused row and tile, the board's own
  // filter ("all" | "uncertain"), and "Not duplicates" rows that stay in place
  // with an Undo (group id → { group, index }) for the scan they were made in.
  boardLimit: 30,
  boardFocus: { id: null, index: 0 },
  boardFilter: "all",
  boardDismissed: new Map(),
  boardDismissedScanId: null,
  // Per-group swipe review state, keyed by group id so it survives re-renders:
  // the reference (anchor) path, the remaining deck order, and the decisions
  // available for undo.
  swipeAnchors: new Map(),
  swipeDeckOrder: new Map(),
  swipeUndo: new Map(),
  // A swipe decision is in flight; held input queues like the review flows.
  swipeBusy: false,
  pendingSwipeDecision: null,
};

export { CSRF_TOKEN, GROUP_RENDER_CHUNK, state };
