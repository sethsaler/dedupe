// Shared client state and the per-launch CSRF token."""


const CSRF_TOKEN =
  document.querySelector('meta[name="dedupe-token"]')?.getAttribute("content") || "";
// Sidebar renders at most this many rows initially (state.groupListLimit).
const GROUP_RENDER_CHUNK = 60;

const state = {
  kind: "all",
  groups: [],
  allGroups: [],
  currentId: null,
  pollTimer: null,
  eventSource: null,
  eventFailures: 0,
  memberFocus: 0,
  memberPage: 0,
  memberSort: "faces-desc", // server order for Faces; only re-sort on demand
  lightboxItems: [],
  lightboxIndex: 0,
  scanning: false,
  acting: false,
  actionBusy: false, // a file action run by this tab is in flight
  cpuCount: 0,
  autoWorkers: 0,
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
};

export { CSRF_TOKEN, GROUP_RENDER_CHUNK, state };
