"""Core data models for scan inventory, groups, and results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class MediaType(str, Enum):
    IMAGE = "image"
    GIF = "gif"
    VIDEO = "video"
    OTHER = "other"


class GroupKind(str, Enum):
    EXACT = "exact"
    SIMILAR = "similar"


class SmartRule(str, Enum):
    AUTOMATIC = "automatic"
    NEWEST = "newest"
    OLDEST = "oldest"
    LARGEST = "largest"
    SMALLEST = "smallest"
    SHORTEST_PATH = "shortest_path"
    DESELECT_ALL = "deselect_all"


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
    width: int | None = None
    height: int | None = None
    sha256: str | None = None
    partial_hash: str | None = None
    phash: str | None = None
    dhash: str | None = None
    video_fingerprint: str | None = None
    duration: float | None = None
    error: str | None = None

    @property
    def path_obj(self) -> Path:
        return Path(self.path)

    @property
    def pixels(self) -> int:
        if self.width and self.height:
            return self.width * self.height
        return 0

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
            width=data.get("width"),
            height=data.get("height"),
            sha256=data.get("sha256"),
            partial_hash=data.get("partial_hash"),
            phash=data.get("phash"),
            dhash=data.get("dhash"),
            video_fingerprint=data.get("video_fingerprint"),
            duration=data.get("duration"),
            error=data.get("error"),
        )


@dataclass
class DuplicateGroup:
    id: str
    kind: GroupKind
    media_type: MediaType
    members: list[FileRecord]
    selected_for_removal: list[str] = field(default_factory=list)
    suggested_keep: str | None = None

    @property
    def reclaimable_bytes(self) -> int:
        if not self.members:
            return 0
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
            "media_type": self.media_type.value,
            "members": [m.to_dict() for m in self.members],
            "selected_for_removal": list(self.selected_for_removal),
            "suggested_keep": self.suggested_keep,
            "reclaimable_bytes": self.reclaimable_bytes,
            "member_count": len(self.members),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DuplicateGroup:
        return cls(
            id=data["id"],
            kind=GroupKind(data["kind"]),
            media_type=MediaType(data["media_type"]),
            members=[FileRecord.from_dict(m) for m in data.get("members", [])],
            selected_for_removal=list(data.get("selected_for_removal", [])),
            suggested_keep=data.get("suggested_keep"),
        )


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanResult:
    roots: list[str]
    files: list[FileRecord]
    groups: list[DuplicateGroup]
    exact_groups: int = 0
    similar_groups: int = 0
    reclaimable_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    def recompute_stats(self) -> None:
        self.exact_groups = sum(1 for g in self.groups if g.kind == GroupKind.EXACT)
        self.similar_groups = sum(1 for g in self.groups if g.kind == GroupKind.SIMILAR)
        self.reclaimable_bytes = sum(g.reclaimable_bytes for g in self.groups)

    def to_dict(self) -> dict[str, Any]:
        self.recompute_stats()
        return {
            "roots": list(self.roots),
            "files": [f.to_dict() for f in self.files],
            "groups": [g.to_dict() for g in self.groups],
            "exact_groups": self.exact_groups,
            "similar_groups": self.similar_groups,
            "reclaimable_bytes": self.reclaimable_bytes,
            "file_count": len(self.files),
            "group_count": len(self.groups),
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScanResult:
        result = cls(
            roots=list(data.get("roots", [])),
            files=[FileRecord.from_dict(f) for f in data.get("files", [])],
            groups=[DuplicateGroup.from_dict(g) for g in data.get("groups", [])],
            errors=list(data.get("errors", [])),
        )
        result.recompute_stats()
        return result
