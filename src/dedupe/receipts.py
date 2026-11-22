"""Discovery, inspection, and pruning of action receipts.

Every destructive (or previewed) action writes a JSON receipt via
``dedupe.actions._write_action_log``. This module is the read side: it lists,
resolves, loads, and prunes those receipts so both the CLI (``dedupe receipts``)
and a UI can offer an action-history / undo panel without knowing the on-disk
layout.

Executed receipts are named ``action-<stamp>-<session>.json``; dry-run previews
are named ``preview-<stamp>-<session>.json`` so they can be pruned aggressively.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

EXECUTED_PREFIX = "action"
PREVIEW_PREFIX = "preview"
UNDOABLE_ACTIONS = frozenset({"quarantine", "trash", "crop"})


class ReceiptError(RuntimeError):
    """Base class for receipt lookup problems."""


class ReceiptNotFoundError(ReceiptError):
    """No receipt matched the supplied reference."""


class AmbiguousReceiptError(ReceiptError):
    """More than one receipt matched the supplied reference."""


def default_log_dir() -> Path:
    """Directory receipts are written to when no explicit ``log_dir`` is given."""
    return Path.home() / ".cache" / "dedupe" / "logs"


def resolve_log_dir(log_dir: str | Path | None = None) -> Path:
    return Path(log_dir).expanduser() if log_dir else default_log_dir()


def receipt_filename(*, dry_run: bool, stamp: str, session_id: str) -> str:
    prefix = PREVIEW_PREFIX if dry_run else EXECUTED_PREFIX
    return f"{prefix}-{stamp}-{session_id[:8]}.json"


@dataclass
class ReceiptSummary:
    """Parsed, display-ready view of one receipt file."""

    id: str
    log_path: str
    action: str
    dry_run: bool
    executed: bool
    started_at: str | None
    completed_at: str | None
    session_id: str | None
    item_count: int
    success_count: int
    fail_count: int
    bytes: int
    undoable: bool
    undo_blocked_reason: str | None
    review_root: str | None
    receipt_bytes: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PrunedReceipt:
    id: str
    log_path: str
    reason: str
    receipt_bytes: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PruneResult:
    dry_run: bool
    removed: list[PrunedReceipt]
    kept_count: int
    freed_bytes: int
    errors: list[str]

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "removed": [r.to_dict() for r in self.removed],
            "removed_count": self.removed_count,
            "kept_count": self.kept_count,
            "freed_bytes": self.freed_bytes,
            "errors": list(self.errors),
        }


def receipt_id(path: str | Path) -> str:
    """Stable identifier for a receipt: its filename without the ``.json``."""
    return Path(path).stem


def iter_receipt_paths(
    log_dir: str | Path | None = None, *, include_previews: bool = True
) -> list[Path]:
    base = resolve_log_dir(log_dir)
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    prefixes = (
        (f"{EXECUTED_PREFIX}-", f"{PREVIEW_PREFIX}-")
        if include_previews
        else (f"{EXECUTED_PREFIX}-",)
    )
    return [p for p in entries if p.suffix == ".json" and p.name.startswith(prefixes)]


def unreadable_receipt_paths(log_dir: str | Path | None = None) -> list[Path]:
    """Receipt files that exist but cannot be parsed into a summary."""
    return [path for path in iter_receipt_paths(log_dir) if summarize_receipt(path) is None]


def _sort_key(summary: ReceiptSummary) -> str:
    return summary.started_at or summary.id


def _restorable_location(item: dict, action: str) -> str | None:
    ref = item.get("original_in_trash") or item.get("destination") if action == "crop" else item.get("destination")
    if not ref or ref == "Trash":
        return None
    return str(ref)


def _undo_state(data: dict) -> tuple[bool, str | None]:
    action = str(data.get("action") or "")
    if data.get("status") == "running":
        return False, "action is still running"
    if data.get("dry_run"):
        return False, "dry-run previews change nothing"
    if action not in UNDOABLE_ACTIONS:
        return False, f"{action or 'unknown'} actions cannot be undone automatically"
    moved = [item for item in data.get("items") or [] if item.get("ok")]
    # The undo restores from the recorded destination (or original_in_trash for
    # crops); a trash item recorded as the bare "Trash" marker has none, and one
    # blocked item refuses the whole undo (all-or-nothing), so the receipt only
    # counts as undoable when every succeeded item carries a real backup path.
    restorable = [item for item in moved if _restorable_location(item, action) is not None]
    if not restorable:
        return False, "receipt has no restorable items"
    if len(restorable) != len(moved):
        return False, (
            "some cropped files have no recorded original in Trash"
            if action == "crop"
            else "some moved files have no recorded destination"
        )
    return True, None


def _restorable_bytes(data: dict) -> int:
    total = 0
    for item in data.get("items") or []:
        if item.get("ok"):
            total += int(item.get("size") or 0)
    return total


def summarize_receipt_data(
    path: str | Path, data: dict, *, receipt_bytes: int | None = None
) -> ReceiptSummary:
    """Build a :class:`ReceiptSummary` from an already-parsed receipt dict."""
    log_path = Path(path)
    if receipt_bytes is None:
        try:
            receipt_bytes = log_path.stat().st_size
        except OSError:
            receipt_bytes = 0
    items = data.get("items") or []
    undoable, blocked = _undo_state(data)
    dry_run = bool(data.get("dry_run"))
    return ReceiptSummary(
        id=receipt_id(log_path),
        log_path=str(log_path),
        action=str(data.get("action") or "unknown"),
        dry_run=dry_run,
        executed=not dry_run,
        started_at=data.get("started_at"),
        completed_at=data.get("completed_at"),
        session_id=data.get("session_id"),
        item_count=len(items),
        success_count=int(data.get("success_count") or 0),
        fail_count=int(data.get("fail_count") or 0),
        bytes=_restorable_bytes(data),
        undoable=undoable,
        undo_blocked_reason=blocked,
        review_root=data.get("review_root"),
        receipt_bytes=receipt_bytes,
    )


def summarize_receipt(path: str | Path) -> ReceiptSummary | None:
    """Parse one receipt file; returns ``None`` when it is missing or corrupt."""
    log_path = Path(path)
    try:
        raw_bytes = log_path.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return summarize_receipt_data(log_path, data, receipt_bytes=len(raw_bytes))


def list_receipts(
    log_dir: str | Path | None = None,
    *,
    limit: int | None = None,
    include_previews: bool = True,
    actions: set[str] | None = None,
    undoable_only: bool = False,
) -> list[ReceiptSummary]:
    """Return receipt summaries, newest first.

    ``include_previews`` keeps dry-run receipts; ``actions`` filters on the
    receipt's action string (``trash``, ``quarantine``, ``isolate:copy``,
    ``undo:quarantine``, ...).
    """
    summaries: list[ReceiptSummary] = []
    scan_previews = include_previews and not undoable_only
    for path in iter_receipt_paths(log_dir, include_previews=scan_previews):
        summary = summarize_receipt(path)
        if summary is None:
            continue
        if not include_previews and summary.dry_run:
            continue
        if actions and summary.action not in actions:
            continue
        if undoable_only and not summary.undoable:
            continue
        summaries.append(summary)
    summaries.sort(key=_sort_key, reverse=True)
    return summaries[:limit] if limit else summaries


def resolve_receipt_path(reference: str | Path, log_dir: str | Path | None = None) -> Path:
    """Resolve a receipt reference to a path.

    Accepts an existing filesystem path, a receipt id (filename stem), a bare
    filename, or any unique substring of an id (e.g. the 8-char session prefix).
    """
    raw = str(reference).strip()
    if not raw:
        raise ReceiptNotFoundError("no receipt reference given")

    direct = Path(raw).expanduser()
    if direct.is_file():
        return direct.resolve()

    candidates = iter_receipt_paths(log_dir)
    by_id = {receipt_id(p): p for p in candidates}
    if raw in by_id:
        return by_id[raw].resolve()
    name_match = [p for p in candidates if p.name == raw]
    if len(name_match) == 1:
        return name_match[0].resolve()

    lowered = raw.lower()
    partial = sorted({p for p in candidates if lowered in p.name.lower()})
    if len(partial) == 1:
        return partial[0].resolve()
    if partial:
        joined = ", ".join(receipt_id(p) for p in partial[:5])
        raise AmbiguousReceiptError(f"receipt reference '{raw}' matches: {joined}")
    raise ReceiptNotFoundError(
        f"no receipt matching '{raw}' in {resolve_log_dir(log_dir)}"
    )


def load_receipt(reference: str | Path, log_dir: str | Path | None = None) -> dict:
    """Load one receipt's raw JSON, with ``id`` and ``log_path`` guaranteed."""
    path = resolve_receipt_path(reference, log_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ReceiptError(f"receipt {path} is not a JSON object")
    if not data.get("log_path"):
        data["log_path"] = str(path)
    data["id"] = receipt_id(path)
    return data


def prune_receipts(
    log_dir: str | Path | None = None,
    *,
    older_than_days: float | None = None,
    keep: int | None = None,
    drop_previews: bool = False,
    dry_run: bool = True,
) -> PruneResult:
    """Delete old receipts.

    A receipt is removed when it is older than ``older_than_days``, or falls
    outside the ``keep`` newest, or (with ``drop_previews``) is a dry-run
    preview. With no criteria nothing is removed. ``dry_run=True`` only reports.
    """
    summaries = list_receipts(log_dir)
    removed: list[PrunedReceipt] = []
    errors: list[str] = []
    cutoff = (
        datetime.now(UTC) - timedelta(days=float(older_than_days))
        if older_than_days is not None
        else None
    )

    # With any criterion active, unreadable receipts join the cleanup: they can
    # never match a parsed rule, so left alone they would linger forever.
    if older_than_days is not None or keep is not None or drop_previews:
        for path in unreadable_receipt_paths(log_dir):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if not dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    errors.append(f"{path.stem}: {exc}")
                    continue
            removed.append(
                PrunedReceipt(
                    id=path.stem,
                    log_path=str(path),
                    reason="unreadable receipt",
                    receipt_bytes=size,
                )
            )

    for index, summary in enumerate(summaries):
        reason: str | None = None
        started = _started_at(summary)
        if drop_previews and summary.dry_run:
            reason = "dry-run preview"
        elif keep is not None and index >= max(0, int(keep)):
            reason = f"outside the {int(keep)} newest receipts"
        elif cutoff is not None and started is not None and started < cutoff:
            reason = f"older than {older_than_days} days"
        if reason is None:
            continue
        if not dry_run:
            try:
                Path(summary.log_path).unlink()
            except OSError as exc:
                errors.append(f"{summary.id}: {exc}")
                continue
        removed.append(
            PrunedReceipt(
                id=summary.id,
                log_path=summary.log_path,
                reason=reason,
                receipt_bytes=summary.receipt_bytes,
            )
        )

    return PruneResult(
        dry_run=dry_run,
        removed=removed,
        kept_count=len(summaries) - len(removed),
        freed_bytes=sum(r.receipt_bytes for r in removed),
        errors=errors,
    )


def auto_prune_preview_receipts(
    log_dir: str | Path | None = None, *, max_keep: int = 50
) -> int:
    """Unlink oldest ``preview-*.json`` files beyond ``max_keep`` safely."""
    keep = max(0, int(max_keep))
    base = resolve_log_dir(log_dir)
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    previews: list[tuple[int, str, Path]] = []
    for candidate in entries:
        if candidate.suffix == ".json" and candidate.name.startswith(f"{PREVIEW_PREFIX}-"):
            try:
                mtime_ns = candidate.stat().st_mtime_ns
            except OSError:
                continue
            previews.append((mtime_ns, candidate.name, candidate))
    if len(previews) <= keep:
        return 0
    previews.sort()
    removed = 0
    for _, _, path in previews[: len(previews) - keep]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _started_at(summary: ReceiptSummary) -> datetime | None:
    if not summary.started_at:
        return None
    try:
        parsed = datetime.fromisoformat(summary.started_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
