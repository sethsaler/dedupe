// Local-only labels let the user evaluate detection on their own decisions.
// Export contains paths, never file bytes. Undo removes the corresponding label.
import { $ } from "./util.js";
const KEY = "dedupe.reviewLabels.v1";
let active = null;
let started = 0;
const pairKey = (a, b) => [a, b].sort().join("\n");
function load() {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) || "[]");
    return Array.isArray(value) ? value.filter((p) => p && typeof p.path_a === "string"
      && typeof p.path_b === "string" && typeof p.similar === "boolean") : [];
  } catch { return []; }
}
function save(labels) { try { localStorage.setItem(KEY, JSON.stringify(labels.slice(-5000))); } catch { /* quota/private mode */ } }
export function noteReviewPair(a, b) {
  const key = pairKey(a, b);
  if (active !== key) { active = key; started = performance.now(); }
}
export function recordReview(a, b, similar) {
  const elapsed = active === pairKey(a, b) ? (performance.now() - started) / 1000 : null;
  const labels = load().filter((p) => pairKey(p.path_a, p.path_b) !== pairKey(a, b));
  labels.push({ path_a: a, path_b: b, similar, category: "user-reviewed", review_seconds: elapsed, reviewed_at: new Date().toISOString() });
  save(labels);
}
export function undoReview(a, b) { save(load().filter((p) => pairKey(p.path_a, p.path_b) !== pairKey(a, b))); }
$("exportReviewLabels").addEventListener("click", () => {
  const pairs = load();
  const timed = pairs.filter((p) => p.review_seconds > 0 && p.review_seconds < 300);
  const seconds = timed.reduce((n, p) => n + p.review_seconds, 0);
  const blob = new Blob([JSON.stringify({
    note: "Local review decisions, not independent ground truth. Restore trashed samples before benchmarking. Timing excludes pairs taking over five minutes.",
    review: { pairs: pairs.length, timed_pairs: timed.length, seconds, decisions_per_minute: seconds ? timed.length * 60 / seconds : null }, pairs,
  }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a"); link.href = url; link.download = "dedupe-review-labels.json"; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
