// Group predicates, diff summaries, and lookups over the shared state.

import { state } from "./state.js";
import { formatBytes } from "./util.js";

function isDecisionReview(g) {
  return g?.kind === "low_resolution" || g?.kind === "random_review";
}

function isIndependentReview(g) {
  return g?.policy === "independent_candidates";
}

function isPagedIndependentReview(g) {
  return g?.kind === "no_humans" || g?.kind === "faces" || g?.kind === "all_files"
    || g?.kind === "sets";
}

// Keep-one duplicate groups (exact/similar) render the same 50-card grid pages
// as the triage reviews once they outgrow a single page. Triage-only behavior
// (Trash affordances, deleted placeholders) still keys off
// isPagedIndependentReview above — this is only the grid-rendering concern.
function isGridPagedGroup(g) {
  return isPagedIndependentReview(g) || g?.kind === "exact" || g?.kind === "similar";
}

function currentGroup() {
  return state.allGroups.find((group) => group.id === state.currentId)
    || state.groups.find((group) => group.id === state.currentId);
}

function patchGroup(updated) {
  if (!updated?.id) return;
  const idx = state.groups.findIndex((candidate) => candidate.id === updated.id);
  if (idx >= 0) state.groups[idx] = updated;
  const allIdx = state.allGroups.findIndex((candidate) => candidate.id === updated.id);
  if (allIdx >= 0) state.allGroups[allIdx] = updated;
}

function groupSelectedCount(g) {
  return (g?.selected_for_removal || []).length;
}

function groupReviewed(g) {
  if (!g) return false;
  if (g.reviewed) return true;
  if (isIndependentReview(g)) {
    const members = g.members || [];
    const reviewed = new Set(g.reviewed_paths || []);
    const cropped = new Set(g.cropped_paths || []);
    return members.every(
      (member) => member.protected || reviewed.has(member.path) || cropped.has(member.path),
    );
  }
  return state.touchedGroups.has(g.id);
}

function groupComplete(g) {
  if (!g) return false;
  if (g.reviewed) return true;
  const members = g.members || [];
  if (isIndependentReview(g)) {
    return groupReviewed(g);
  }
  const protectedCount = members.reduce((count, member) => count + (member.protected ? 1 : 0), 0);
  return (g.selected_for_removal || []).length >= Math.max(0, (g.member_count || 0) - Math.max(1, protectedCount));
}

function groupNeedsAttention(g) {
  if (!g) return false;
  return (g.members || []).some((member) => member.error)
    || (!g.reviewed && (g.deleted_paths || []).length > 0)
    || !groupComplete(g);
}

// Groups the user has actively re-selected in; until then their selection is
// the smart-select suggestion and the UI labels it as such.
function markGroupTouched(id) {
  if (id) state.touchedGroups.add(id);
}

function extFromPath(path) {
  const match = String(path || "").match(/\.([^./\\]+)$/);
  return match ? `.${match[1].toLowerCase()}` : "";
}

function extLabel(record) {
  const raw = record?.extension || extFromPath(record?.path);
  return raw ? raw.replace(/^\./, "").toUpperCase() : "";
}

function parentDirectory(path) {
  const parts = String(path || "").split(/[/\\]/);
  parts.pop();
  return parts.join("/");
}

function parentFolderName(path) {
  const parts = String(path || "").split(/[/\\]/).filter(Boolean);
  return parts.length >= 2 ? parts[parts.length - 2] : "";
}

function formatPxRatio(ratio) {
  if (ratio >= 10) return `${Math.round(ratio)}×`;
  if (ratio >= 1) return `${ratio.toFixed(1).replace(/\.0$/, "")}×`;
  return `${ratio.toFixed(2).replace(/0$/, "").replace(/\.0$/, "")}×`;
}

// Compact A-vs-B comparison pills for Similar Board tiles & Similar List cards.
function memberDiffPills(member, keeper) {
  if (!member || !keeper || member.path === keeper.path) return [];
  const pills = [];

  const mSize = Number(member.size);
  const kSize = Number(keeper.size);
  if (Number.isFinite(mSize) && Number.isFinite(kSize) && kSize > 0) {
    const delta = mSize - kSize;
    if (Math.abs(delta) >= 1024 || Math.abs(delta) / kSize >= 0.05) {
      if (delta !== 0) {
        const sign = delta > 0 ? "+" : "−";
        pills.push({
          kind: delta > 0 ? "size-larger" : "size-smaller",
          label: `${sign}${formatBytes(Math.abs(delta))}`,
          title: `${formatBytes(mSize)} vs keeper ${formatBytes(kSize)}`,
        });
      }
    }
  }

  const mw = Number(member.width);
  const mh = Number(member.height);
  const kw = Number(keeper.width);
  const kh = Number(keeper.height);
  if (mw > 0 && mh > 0 && kw > 0 && kh > 0) {
    const mPx = mw * mh;
    const kPx = kw * kh;
    if (Math.abs(mPx - kPx) / kPx >= 0.02) {
      pills.push({
        kind: mPx > kPx ? "res-higher" : "res-lower",
        label: `${formatPxRatio(mPx / kPx)} px`,
        title: `${mw}×${mh} vs keeper ${kw}×${kh}`,
      });
    }
    const mRatio = Math.max(mw / mh, mh / mw);
    const kRatio = Math.max(kw / kh, kh / kw);
    if (Math.abs(mRatio - kRatio) / Math.max(mRatio, kRatio) > 0.04) {
      pills.push({
        kind: "aspect-diff",
        label: "Aspect diff",
        title: `${mw}×${mh} aspect differs from keeper ${kw}×${kh}`,
      });
    }
  }

  const mExt = extLabel(member);
  const kExt = extLabel(keeper);
  if (mExt && kExt && mExt !== kExt) {
    pills.push({
      kind: "format-diff",
      label: `${mExt} vs ${kExt}`,
      title: `Format ${mExt} differs from keeper ${kExt}`,
    });
  }

  const mDir = parentDirectory(member.path);
  const kDir = parentDirectory(keeper.path);
  if (mDir !== kDir) {
    const folder = parentFolderName(member.path) || "other folder";
    pills.push({
      kind: "folder-diff",
      label: `in ${folder}`,
      title: `Different folder from keeper: ${mDir}`,
    });
  }

  if (member.duration != null && keeper.duration != null) {
    const dDelta = Number(member.duration) - Number(keeper.duration);
    if (Number.isFinite(dDelta) && Math.abs(dDelta) >= 0.5) {
      const sign = dDelta > 0 ? "+" : "−";
      pills.push({
        kind: "duration-diff",
        label: `${sign}${Math.abs(dDelta).toFixed(1).replace(/\.0$/, "")}s`,
        title: `Duration differs from keeper (${Number(member.duration).toFixed(1)}s vs ${Number(keeper.duration).toFixed(1)}s)`,
      });
    }
  }

  return pills;
}

function lightboxItemFor(member, group, keeperPathOverride = null) {
  const members = group?.members || [];
  const keeperPath = keeperPathOverride ?? group?.suggested_keep ?? null;
  const keeperMember = keeperPath
    ? members.find((candidate) => candidate.path === keeperPath) || null
    : null;
  return {
    path: member.path,
    mediaType: member.media_type,
    keeper: keeperPath,
    kind: group.kind,
    group_kind: group.kind,
    size: member.size,
    width: member.width,
    height: member.height,
    mtime: member.mtime,
    extension: member.extension || extFromPath(member.path),
    duration: member.duration ?? null,
    media_version: member.media_version ?? null,
    similarityPercent: member.similarity_percent,
    orientationLabel: member.orientation_label,
    orientation_label: member.orientation_label ?? null,
    match_label: member.match_label ?? null,
    match_basis: group.match_basis ?? null,
    is_keeper: member.path === keeperPath,
    protected: Boolean(member.protected),
    keeper_size: keeperMember?.size ?? null,
    keeper_width: keeperMember?.width ?? null,
    keeper_height: keeperMember?.height ?? null,
    keeper_extension: keeperMember
      ? (keeperMember.extension || extFromPath(keeperMember.path))
      : null,
    keeper_mtime: keeperMember?.mtime ?? null,
    keeper_reason: group.cleanup?.keeper_reason ?? null,
  };
}

export {
  isDecisionReview,
  isIndependentReview,
  isPagedIndependentReview,
  isGridPagedGroup,
  currentGroup,
  patchGroup,
  groupSelectedCount,
  groupReviewed,
  groupComplete,
  groupNeedsAttention,
  markGroupTouched,
  parentDirectory,
  parentFolderName,
  memberDiffPills,
  lightboxItemFor,
};
