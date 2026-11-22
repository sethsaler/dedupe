"""Persistent action and review-decision history for the local UI."""

from __future__ import annotations

import json
from pathlib import Path

from .action_journal import recover_interrupted_actions
from .cache import HashCache
from .receipts import ReceiptSummary, iter_receipt_paths, summarize_receipt_data

_ACTION_LABELS = {
    "trash": "Moved to Trash",
    "quarantine": "Moved to Quarantine",
    "delete": "Permanently Deleted",
    "crop": "Cropped Borders",
    "undo": "Restored Files",
    "undo:trash": "Restored from Trash",
    "undo:quarantine": "Restored from Quarantine",
    "undo:crop": "Restored Pre-Crop Original",
    "isolate:copy": "Isolated (Copy)",
    "isolate:move": "Isolated (Move)",
    "isolate:hardlink": "Isolated (Hardlink)",
    "isolate:symlink": "Isolated (Symlink)",
}


def action_label(action: str) -> str:
    """Return a human-readable label for a receipt action string."""
    normalized = str(action or "").strip()
    if not normalized:
        return "Unknown Action"
    if normalized in _ACTION_LABELS:
        return _ACTION_LABELS[normalized]
    if normalized.startswith("undo:"):
        target = normalized.split(":", 1)[1].replace("_", " ").strip()
        return f"Restored ({target})" if target else "Restored Files"
    if normalized.startswith("isolate:"):
        mode = normalized.split(":", 1)[1].replace("_", " ").strip()
        return f"Isolated ({mode.title()})" if mode else "Isolated"
    return normalized.replace(":", " · ").replace("_", " ").title()


def _is_restorable_item(action: str, item: dict) -> bool:
    if action == "crop":
        backup_ref = item.get("original_in_trash") or item.get("destination")
        if not backup_ref or backup_ref == "Trash":
            return False
        backup = Path(backup_ref)
        return backup.is_file() and not backup.is_symlink()
    dest_ref = item.get("destination")
    if not dest_ref or dest_ref == "Trash":
        return False
    destination = Path(dest_ref)
    source = Path(item["path"])
    return (
        destination.is_file()
        and not destination.is_symlink()
        and not (source.exists() or source.is_symlink())
    )


def list_history(log_dir=None, *, cache_path=None, limit=50, offset=0) -> dict:
    """Read durable history with current availability; final Undo still preflights."""
    recover_interrupted_actions(log_dir)
    limit = min(100, max(1, int(limit)))
    offset = max(0, int(offset))
    parsed: list[tuple[ReceiptSummary, dict]] = []
    for path in iter_receipt_paths(log_dir, include_previews=False):
        try:
            raw_bytes = path.read_bytes()
            data = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        summary = summarize_receipt_data(path, data, receipt_bytes=len(raw_bytes))
        if summary.dry_run:
            continue
        parsed.append((summary, data))
    parsed.sort(key=lambda pair: pair[0].started_at or pair[0].id, reverse=True)

    actions = []
    errors = []
    for summary, data in parsed[offset:offset + limit]:
        try:
            items = data.get("items") or []
            row = {
                **summary.to_dict(),
                "action_label": action_label(summary.action),
                "status": data.get("status", "complete"),
                "uncertain_count": data.get("uncertain_count", 0),
                "items": items[:200],
                "items_truncated": len(items) > 200,
                "restorable_count": 0,
            }
            for item in items:
                if not item.get("ok") or not row["undoable"]:
                    continue
                if _is_restorable_item(summary.action, item):
                    row["restorable_count"] += 1
            expected_restorable = summary.success_count or sum(
                1 for item in items if item.get("ok")
            )
            if row["undoable"] and row["restorable_count"] != expected_restorable:
                row["undoable"] = False
                row["undo_blocked_reason"] = (
                    "The original file before cropping is no longer in the Trash."
                    if summary.action == "crop"
                    else "Some files are no longer at their recorded destinations, or an "
                    "original path is occupied. They may already have been restored."
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
    return {
        "actions": actions,
        "action_count": len(parsed),
        "decisions": decisions,
        "decision_count": decision_count,
        "offset": offset,
        "limit": limit,
        "errors": errors,
    }


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
