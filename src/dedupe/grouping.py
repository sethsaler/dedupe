"""Group construction, ranking, and smart-select rules."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .human_policy import is_current_no_person_decision
from .models import DuplicateGroup, FileRecord, GroupKind, MediaType, SmartRule


def rank_keep_candidate(rec: FileRecord) -> tuple:
    """
    Higher is better for 'automatic' keep.
    Prefer: more pixels, larger bytes, newer mtime, shorter path.
    """
    path_depth = len(Path(rec.path).parts)
    name_len = len(Path(rec.path).name)
    return (
        rec.pixels,
        rec.size,
        rec.mtime,
        -path_depth,
        -name_len,
    )


def cluster_around_best(
    records: list[FileRecord], adjacency: dict[str, set[str]]
) -> list[list[FileRecord]]:
    """Build disjoint groups whose members all directly match the best-ranked member.

    Connected-component clustering is unsafe for fuzzy matches: A can match B and B
    can match C even when A does not match C. By choosing the likely keeper first and
    only attaching its verified neighbors, every automatically removable member has
    been compared directly with the file the UI will recommend keeping.
    """
    ordered = sorted(
        records,
        key=lambda record: (rank_keep_candidate(record), record.path),
        reverse=True,
    )
    by_path = {record.path: record for record in records}
    remaining = set(by_path)
    groups: list[list[FileRecord]] = []
    for keeper in ordered:
        if keeper.path not in remaining:
            continue
        matches = [
            by_path[path]
            for path in adjacency.get(keeper.path, set())
            if path in remaining
        ]
        if not matches:
            remaining.remove(keeper.path)
            continue
        matches.sort(
            key=lambda record: (rank_keep_candidate(record), record.path),
            reverse=True,
        )
        members = [keeper, *matches]
        groups.append(members)
        remaining.difference_update(member.path for member in members)
    return groups


def pick_suggested_keep(members: list[FileRecord]) -> str:
    best = max(members, key=rank_keep_candidate)
    return best.path


def make_group_id(kind: GroupKind, members: list[FileRecord]) -> str:
    paths = sorted(m.path for m in members)
    raw = f"{kind.value}|" + "|".join(paths)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def dominant_media_type(members: list[FileRecord]) -> MediaType:
    counts: dict[MediaType, int] = {}
    for m in members:
        counts[m.media_type] = counts.get(m.media_type, 0) + 1
    return max(counts, key=counts.get)  # type: ignore[arg-type]


def build_one_group(
    kind: GroupKind,
    members: list[FileRecord],
    *,
    exact_path_sets: list[set[str]] | None = None,
) -> DuplicateGroup | None:
    """Build a single DuplicateGroup, or None if members don't form a valid group."""
    by_path = {m.path: m for m in members}
    members = list(by_path.values())
    if len(members) < 2:
        return None
    if kind == GroupKind.SIMILAR and exact_path_sets:
        paths = {m.path for m in members}
        # Skip if this set is wholly contained in some exact group
        if any(paths <= eps for eps in exact_path_sets):
            return None
    keep = pick_suggested_keep(members)
    g = DuplicateGroup(
        id=make_group_id(kind, members),
        kind=kind,
        media_type=dominant_media_type(members),
        members=sorted(members, key=lambda m: m.path),
        suggested_keep=keep,
    )
    apply_smart_select(g, SmartRule.AUTOMATIC)
    return g


def build_groups(
    exact_member_lists: list[list[FileRecord]],
    similar_member_lists: list[list[FileRecord]],
) -> list[DuplicateGroup]:
    """Build DuplicateGroups. Similar groups exclude pure-subsets of exact groups."""
    groups: list[DuplicateGroup] = []
    exact_path_sets: list[set[str]] = []

    for members in exact_member_lists:
        g = build_one_group(GroupKind.EXACT, members)
        if g is None:
            continue
        groups.append(g)
        exact_path_sets.append({m.path for m in g.members})

    for members in similar_member_lists:
        # Drop members that are exact-duplicates of each other but keep cross-quality
        # similars (an exact set linked to a re-encoded copy still forms a similar group).
        g = build_one_group(
            GroupKind.SIMILAR, members, exact_path_sets=exact_path_sets
        )
        if g is None:
            continue
        groups.append(g)

    # Sort: most reclaimable first
    groups.sort(key=lambda g: g.reclaimable_bytes, reverse=True)
    return groups


def build_no_human_groups(
    members: list[FileRecord], *, chunk_size: int = 50
) -> list[DuplicateGroup]:
    """Build review groups only from explicit no-person detector decisions."""
    groups: list[DuplicateGroup] = []
    for media_type in (MediaType.IMAGE, MediaType.GIF, MediaType.VIDEO):
        matching = sorted(
            (
                member
                for member in members
                if member.media_type == media_type
                and is_current_no_person_decision(
                    member.human_detection_status,
                    member.human_detection_signature,
                )
            ),
            key=lambda member: member.path,
        )
        for offset in range(0, len(matching), chunk_size):
            chunk = matching[offset : offset + chunk_size]
            group = DuplicateGroup(
                id=make_group_id(GroupKind.NO_HUMANS, chunk),
                kind=GroupKind.NO_HUMANS,
                media_type=media_type,
                members=chunk,
                selected_for_removal=[],
                reviewed_paths=[],
                suggested_keep=None,
            )
            groups.append(group)
    return groups


def apply_smart_select(group: DuplicateGroup, rule: SmartRule) -> None:
    """Mutate selected_for_removal. Always keeps at least one file."""
    members = group.members
    if not members:
        group.selected_for_removal = []
        return

    if rule == SmartRule.DESELECT_ALL:
        group.selected_for_removal = []
        return

    if group.kind == GroupKind.NO_HUMANS:
        group.suggested_keep = None
        if rule == SmartRule.SELECT_CANDIDATES:
            group.reviewed_paths = [m.path for m in members]
            group.selected_for_removal = [m.path for m in members]
        return

    if rule == SmartRule.SELECT_CANDIDATES:
        return

    if rule == SmartRule.AUTOMATIC:
        keep = group.suggested_keep or pick_suggested_keep(members)
        group.suggested_keep = keep
        group.selected_for_removal = [m.path for m in members if m.path != keep]
        return

    if rule == SmartRule.NEWEST:
        keep = max(members, key=lambda m: m.mtime).path
    elif rule == SmartRule.OLDEST:
        keep = min(members, key=lambda m: m.mtime).path
    elif rule == SmartRule.LARGEST:
        keep = max(members, key=lambda m: (m.pixels, m.size)).path
    elif rule == SmartRule.SMALLEST:
        keep = min(members, key=lambda m: (m.pixels if m.pixels else m.size, m.size)).path
    elif rule == SmartRule.SHORTEST_PATH:
        keep = min(members, key=lambda m: (len(Path(m.path).parts), len(m.path))).path
    else:
        keep = group.suggested_keep or pick_suggested_keep(members)

    group.suggested_keep = keep
    group.selected_for_removal = [m.path for m in members if m.path != keep]


def apply_smart_select_all(groups: list[DuplicateGroup], rule: SmartRule) -> None:
    for g in groups:
        apply_smart_select(g, rule)
