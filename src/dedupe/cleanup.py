"""Explain and prioritize review; scores are ordering hints, never probabilities."""
from __future__ import annotations

import math

from .models import GroupKind


def cleanup_opportunity(group, agreements: list[float] | None = None) -> dict:
    keeper = next((m for m in group.members if m.path == group.suggested_keep), None)
    if keeper is None or group.kind not in (GroupKind.EXACT, GroupKind.SIMILAR):
        return {"score": 0, "label": "Browse", "reason": "Individual review"}
    others = [m for m in group.members if m.path != keeper.path]
    reasons = []
    if keeper.keeper_preference:
        reasons.append("in your preferred folder")
    if keeper.capture_metadata:
        reasons.append("has camera capture metadata")
    if others and all(keeper.pixels > m.pixels for m in others):
        reasons.append("higher resolution than every copy")
    keeper_reason = "; ".join(reasons) or "ranked by resolution, export name, size, date and path"
    semantic = group.match_basis == "semantic"
    minimum = min(agreements) if agreements else 0
    if group.kind == GroupKind.EXACT:
        weight, label, reason = 1.0, "Identical files", "Byte-identical copies"
    elif semantic:
        weight, label, reason = 0.25, "Inspect edits", "Visual look-alike; confirm the same shot"
    elif agreements and minimum >= 95:
        weight, label, reason = 0.9, "Strong match", "High fingerprint agreement with the keeper"
    else:
        weight, label, reason = 0.55, "Compare copies", "Perceptual match; inspect differences"
    # A clear resolution winner is quicker to assess, but cannot turn a
    # semantic match into a verified duplicate. Log dampens giant videos.
    clarity = 1.15 if any("higher resolution" in item for item in reasons) else 1.0
    score = math.log2(1 + max(0, group.reclaimable_bytes)) * weight * clarity
    if any(m.error for m in group.members):
        score *= 0.1
        label, reason = "Check file errors", "Some files could not be analyzed completely"
    return {"score": round(score, 4), "label": label, "reason": reason,
            "keeper_reason": keeper_reason}
