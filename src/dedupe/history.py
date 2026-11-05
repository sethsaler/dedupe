"""Persistent action and review-decision history for the local UI."""

from __future__ import annotations

from pathlib import Path

from .action_journal import recover_interrupted_actions
from .cache import HashCache
from .receipts import list_receipts, load_receipt


def list_history(log_dir=None, *, cache_path=None, limit=50, offset=0) -> dict:
    """Read durable history with current availability; final Undo still preflights."""
    recover_interrupted_actions(log_dir)
    limit = min(100, max(1, int(limit)))
    offset = max(0, int(offset))
    summaries = list_receipts(log_dir, include_previews=False)
    actions = []
    errors = []
    for summary in summaries[offset:offset + limit]:
        try:
            data = load_receipt(summary.log_path)
            items = data.get("items") or []
            row = {**summary.to_dict(), "status": data.get("status", "complete"),
                   "uncertain_count": data.get("uncertain_count", 0)}
            row["items"] = items[:200]
            row["items_truncated"] = len(items) > 200
            row["restorable_count"] = 0
            for item in items:
                if not item.get("ok") or not row["undoable"]:
                    continue
                destination = Path(item["destination"])
                source = Path(item["path"])
                if destination.is_file() and not destination.is_symlink() and not (
                    source.exists() or source.is_symlink()
                ):
                    row["restorable_count"] += 1
            if row["undoable"] and row["restorable_count"] != summary.success_count:
                row["undoable"] = False
                row["undo_blocked_reason"] = (
                    "Some files are no longer at their recorded destinations, or an original "
                    "path is occupied. They may already have been restored."
                )
            if row["status"] == "running":
                row["undoable"] = False
                row["undo_blocked_reason"] = "This action is still running."
            actions.append(row)
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"Could not read {summary.id}: {exc}")
    cache = HashCache(cache_path)
    try:
        decisions, decision_count = cache.list_distinct_pairs(limit=limit, offset=offset)
    finally:
        cache.close()
    return {"actions": actions, "action_count": len(summaries), "decisions": decisions,
            "decision_count": decision_count, "offset": offset, "limit": limit, "errors": errors}


def reconsider_distinct_decision(path_a: str, path_b: str, *, cache_path=None) -> dict:
    """Remove an explicit persisted exclusion; files can match on the next scan."""
    if not isinstance(path_a, str) or not isinstance(path_b, str) or not path_a or not path_b:
        raise ValueError("Two file paths are required")
    cache = HashCache(cache_path)
    try:
        removed = cache.unmark_distinct_pair(path_a, path_b)
    finally:
        cache.close()
    return {"ok": True, "removed_pairs": removed}
