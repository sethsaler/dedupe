"""Core data models for scan inventory, groups, and results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .human_policy import is_current_no_person_decision


class MediaType(str, Enum):
    IMAGE = "image"
    GIF = "gif"
    VIDEO = "video"
    MIXED = "mixed"
    OTHER = "other"


class GroupKind(str, Enum):
    EXACT = "exact"
    SIMILAR = "similar"
    NO_HUMANS = "no_humans"


class ReviewPolicy(str, Enum):
    KEEP_ONE = "keep_one"
    INDEPENDENT_CANDIDATES = "independent_candidates"


class SmartRule(str, Enum):
    AUTOMATIC = "automatic"
    NEWEST = "newest"
    OLDEST = "oldest"
    LARGEST = "largest"
    SMALLEST = "smallest"
    SHORTEST_PATH = "shortest_path"
    DESELECT_ALL = "deselect_all"
    SELECT_CANDIDATES = "select_candidates"


IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".raw",
    ".cr2",
    ".nef",
    ".arw",
    ".dng",
}
GIF_EXTS = {".gif"}
VIDEO_EXTS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
    ".mts",
    ".m2ts",
    ".wmv",
    ".flv",
    ".3gp",
}


def classify_media(path: Path) -> MediaType:
    ext = path.suffix.lower()
    if ext in GIF_EXTS:
        return MediaType.GIF
    if ext in IMAGE_EXTS:
        return MediaType.IMAGE
    if ext in VIDEO_EXTS:
        return MediaType.VIDEO
    return MediaType.OTHER


@dataclass
class FileRecord:
    path: str
    size: int
    mtime: float
    media_type: MediaType
    extension: str
    device: int | None = None
    inode: int | None = None
    mtime_ns: int | None = None
    width: int | None = None
    height: int | None = None
    sha256: str | None = None
    partial_hash: str | None = None
    phash: str | None = None
    dhash: str | None = None
    tile_phashes: str | None = None
    video_fingerprint: str | None = None
    duration: float | None = None
    human_detection_status: str | None = None
    human_detector: str | None = None
    human_detection_signature: str | None = None
    human_frames_analyzed: int | None = None
    human_max_confidence: float | None = None
    error: str | None = None

    @property
    def path_obj(self) -> Path:
        return Path(self.path)

    @property
    def pixels(self) -> int:
        if self.width and self.height:
            return self.width * self.height
        return 0

    @property
    def mtime_sort_stamp(self) -> int:
        """Nanosecond modification stamp for newest-first ordering."""
        if self.mtime_ns is not None:
            return int(self.mtime_ns)
        return int(self.mtime * 1_000_000_000)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["media_type"] = self.media_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileRecord:
        mt = data.get("media_type", "other")
        if isinstance(mt, str):
            mt = MediaType(mt)
        return cls(
            path=data["path"],
            size=int(data["size"]),
            mtime=float(data["mtime"]),
            media_type=mt,
            extension=data.get("extension", Path(data["path"]).suffix.lower()),
            device=data.get("device"),
            inode=data.get("inode"),
            mtime_ns=data.get("mtime_ns"),
            width=data.get("width"),
            height=data.get("height"),
            sha256=data.get("sha256"),
            partial_hash=data.get("partial_hash"),
            phash=data.get("phash"),
            dhash=data.get("dhash"),
            tile_phashes=data.get("tile_phashes"),
            video_fingerprint=data.get("video_fingerprint"),
            duration=data.get("duration"),
            human_detection_status=data.get("human_detection_status"),
            human_detector=data.get("human_detector"),
            human_detection_signature=data.get("human_detection_signature"),
            human_frames_analyzed=data.get("human_frames_analyzed"),
            human_max_confidence=data.get("human_max_confidence"),
            error=data.get("error"),
        )


@dataclass
class ReviewGroup:
    id: str
    kind: GroupKind
    media_type: MediaType
    members: list[FileRecord]
    selected_for_removal: list[str] = field(default_factory=list)
    reviewed_paths: list[str] = field(default_factory=list)
    suggested_keep: str | None = None
    # Source scan root when folders are scanned as independent parallel streams.
    root: str | None = None

    @property
    def policy(self) -> ReviewPolicy:
        if self.kind == GroupKind.NO_HUMANS:
            return ReviewPolicy.INDEPENDENT_CANDIDATES
        return ReviewPolicy.KEEP_ONE

    @property
    def reclaimable_bytes(self) -> int:
        if not self.members:
            return 0
        if self.policy == ReviewPolicy.INDEPENDENT_CANDIDATES:
            selected = set(self.selected_for_removal)
            reviewed = set(self.reviewed_paths)
            return sum(
                m.size
                for m in self.members
                if m.path in selected
                and m.path in reviewed
                and is_current_no_person_decision(
                    m.human_detection_status,
                    m.human_detection_signature,
                )
            )
        keep = self.suggested_keep
        total = sum(m.size for m in self.members)
        if keep:
            keep_size = next((m.size for m in self.members if m.path == keep), 0)
            return max(0, total - keep_size)
        # keep largest as fallback
        return max(0, total - max(m.size for m in self.members))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "policy": self.policy.value,
            "media_type": self.media_type.value,
            "members": [m.to_dict() for m in self.members],
            "selected_for_removal": list(self.selected_for_removal),
            "reviewed_paths": list(self.reviewed_paths),
            "suggested_keep": self.suggested_keep,
            "reclaimable_bytes": self.reclaimable_bytes,
            "member_count": len(self.members),
            "root": self.root,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewGroup:
        kind = GroupKind(data["kind"])
        members = [FileRecord.from_dict(m) for m in data.get("members", [])]
        if kind == GroupKind.NO_HUMANS:
            # Loaded or hand-built results may be stale. A positive or missing
            # detector decision must never appear in the Non-Human review flow.
            # Keep newest-first order so Non-Human pages match a fresh scan.
            members = sorted(
                (
                    member
                    for member in members
                    if is_current_no_person_decision(
                        member.human_detection_status,
                        member.human_detection_signature,
                    )
                ),
                key=lambda member: (-member.mtime_sort_stamp, member.path),
            )
        member_paths = {member.path for member in members}
        return cls(
            id=data["id"],
            kind=kind,
            media_type=MediaType(data["media_type"]),
            members=members,
            selected_for_removal=[
                path
                for path in data.get("selected_for_removal", [])
                if path in member_paths
            ],
            reviewed_paths=[
                path for path in data.get("reviewed_paths", []) if path in member_paths
            ],
            suggested_keep=data.get("suggested_keep"),
            root=data.get("root"),
        )


# Backward-compatible public name for existing JSON/CLI integrations.
DuplicateGroup = ReviewGroup


def effective_selected_paths(groups: list[DuplicateGroup]) -> list[str]:
    """Return unique selections while retaining one member of every duplicate group."""
    ordered: list[str] = []
    sizes: dict[str, int] = {}
    for group in groups:
        members = {member.path: member for member in group.members}
        for path in group.selected_for_removal:
            if group.kind == GroupKind.NO_HUMANS and path not in group.reviewed_paths:
                continue
            if (
                group.kind == GroupKind.NO_HUMANS
                and members.get(path)
                and not is_current_no_person_decision(
                    members[path].human_detection_status,
                    members[path].human_detection_signature,
                )
            ):
                continue
            if path in members and path not in sizes:
                ordered.append(path)
                sizes[path] = members[path].size

    selected = set(ordered)
    for group in groups:
        if group.policy == ReviewPolicy.INDEPENDENT_CANDIDATES or not group.members:
            continue
        member_paths = {member.path for member in group.members}
        if member_paths <= selected:
            keep = group.suggested_keep
            if keep not in member_paths:
                keep = group.members[0].path
            selected.discard(keep)
    return [path for path in ordered if path in selected]


@dataclass
class ScanProgress:
    phase: str = "idle"
    files_found: int = 0
    files_processed: int = 0
    groups_found: int = 0
    bytes_scanned: int = 0
    message: str = ""
    done: bool = False
    error: str | None = None
    elapsed_seconds: float = 0.0
    eta_seconds: float | None = None
    # Set when this progress belongs to one folder's parallel scan stream.
    stream_index: int | None = None
    root: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanResult:
    roots: list[str]
    files: list[FileRecord]
    groups: list[DuplicateGroup]
    exact_groups: int = 0
    similar_groups: int = 0
    no_human_files: int = 0
    reclaimable_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    def recompute_stats(self) -> None:
        self.exact_groups = sum(1 for g in self.groups if g.kind == GroupKind.EXACT)
        self.similar_groups = sum(1 for g in self.groups if g.kind == GroupKind.SIMILAR)
        self.no_human_files = sum(
            len(g.members) for g in self.groups if g.kind == GroupKind.NO_HUMANS
        )
        sizes = {member.path: member.size for group in self.groups for member in group.members}
        self.reclaimable_bytes = sum(
            sizes.get(path, 0) for path in effective_selected_paths(self.groups)
        )

    def to_dict(self) -> dict[str, Any]:
        self.recompute_stats()
        return {
            "roots": list(self.roots),
            "files": [f.to_dict() for f in self.files],
            "groups": [g.to_dict() for g in self.groups],
            "exact_groups": self.exact_groups,
            "similar_groups": self.similar_groups,
            "no_human_files": self.no_human_files,
            "reclaimable_bytes": self.reclaimable_bytes,
            "file_count": len(self.files),
            "group_count": len(self.groups),
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScanResult:
        groups = [DuplicateGroup.from_dict(g) for g in data.get("groups", [])]
        groups = [
            group
            for group in groups
            if group.kind != GroupKind.NO_HUMANS or group.members
        ]
        result = cls(
            roots=list(data.get("roots", [])),
            files=[FileRecord.from_dict(f) for f in data.get("files", [])],
            groups=groups,
            errors=list(data.get("errors", [])),
        )
        result.recompute_stats()
        return result
