"""Explain and prioritize review; scores are ordering hints, never probabilities."""
from __future__ import annotations

from .models import GroupKind

_HASH_ERROR_PREFIXES = ("partial hash failed", "sha256 failed")


def _has_relevant_error(group) -> bool:
    if group.kind == GroupKind.EXACT:
        return any(
            m.error
            and (m.error.startswith(_HASH_ERROR_PREFIXES) or not m.sha256)
            for m in group.members
        )
    return any(m.error for m in group.members)


def cleanup_opportunity(group, agreements: list[float] | None = None) -> dict:
    keeper = next((m for m in group.members if m.path == group.suggested_keep), None)
    if keeper is None or group.kind not in (GroupKind.EXACT, GroupKind.SIMILAR):
        return {"score": 0, "label": "Browse", "reason": "Individual review"}
    others = [m for m in group.members if m.path != keeper.path]
    reasons = []
    if keeper.protected:
        reasons.append("in your protected library")
    elif group.match_basis == "video_version" and group.match_details.get("relation") == "excerpt":
        reasons.append("the longer recording")
    if keeper.keeper_preference:
        reasons.append("in your preferred folder")
    if (
        group.kind != GroupKind.EXACT
        and keeper.capture_metadata
        and any(not m.capture_metadata for m in others)
    ):
        reasons.append("has camera capture metadata")
    keeper_px = keeper.effective_pixels
    other_pxs = [m.effective_pixels for m in others]
    if others and keeper_px > 0 and all(keeper_px > px for px in other_pxs):
        reasons.append("higher resolution than every copy")
    elif (
        others
        and keeper_px > 0
        and all(keeper_px >= px for px in other_pxs)
        and any(keeper_px > px for px in other_pxs)
    ):
        reasons.append("matches highest resolution among copies")
    keeper_reason = "; ".join(reasons) or "ranked by resolution, export name, size, date and path"
    semantic = group.match_basis == "semantic"
    minimum = min(agreements) if agreements else 0
    if group.kind == GroupKind.EXACT:
        weight, label, reason = 1.0, "Identical files", "Byte-identical copies"
    elif semantic:
        weight, label, reason = 0.25, "Inspect edits", "Visual look-alike; confirm the same shot"
    elif group.match_basis == "video_version":
        weight, label, reason = 0.25, "Review video versions", "Inspect matching sections and audio"
    elif agreements:
        conf = max(0.0, min(100.0, float(minimum))) / 100.0
        weight = 0.40 + 0.55 * (conf * conf)
        if minimum >= 95:
            label, reason = "Strong match", "High fingerprint agreement with the keeper"
        else:
            label, reason = "Compare copies", "Perceptual match; inspect differences"
    else:
        weight, label, reason = 0.55, "Compare copies", "Perceptual match; inspect differences"
    # A clear resolution winner is quicker to assess, but cannot turn a
    # semantic match into a verified duplicate. Sublinear power curve preserves
    # byte-savings priority across small-vs-large duplicates without crushing
    # gigabyte-scale media behind tiny exact copies.
    clarity = 1.15 if any("higher resolution" in item for item in reasons) else 1.0
    score = (max(0, group.reclaimable_bytes) ** 0.35) * weight * clarity
    if _has_relevant_error(group):
        score *= 0.1
        label, reason = "Check file errors", "Some files could not be analyzed completely"
    return {
        "score": round(score, 4),
        "label": label,
        "reason": reason,
        "keeper_reason": keeper_reason,
    }
