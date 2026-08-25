from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
import re
import unicodedata
from uuid import UUID


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _truncate_utf8(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _nanoseconds(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("asset timestamp has no timezone")
    return int(parsed.timestamp() * 1_000_000_000)


def safe_filename(original: str, asset_id: str, limit: int = 255) -> str:
    UUID(asset_id)
    if limit <= 0:
        raise ValueError("filename byte limit must be positive")
    name = unicodedata.normalize("NFC", PurePath(original).name)
    name = _CONTROL.sub("_", name).replace("/", "_")
    if name in {"", ".", ".."}:
        name = asset_id
    if name.startswith("."):
        name = f"_{name[1:]}"

    encoded = name.encode("utf-8")
    if len(encoded) <= limit:
        return name

    suffix = PurePath(name).suffix
    suffix_bytes = suffix.encode("utf-8")
    stem = name[: -len(suffix)] if suffix else name
    budget = limit - len(suffix_bytes)
    truncated_stem = _truncate_utf8(stem, budget)
    if suffix and truncated_stem:
        return f"{truncated_stem}{suffix}"
    truncated = _truncate_utf8(name, limit)
    return truncated or _truncate_utf8(asset_id, limit)


def collision_name(
    name: str, asset_id: str, limit: int = 255, *, ordinal: int = 1
) -> str:
    UUID(asset_id)
    if limit <= 0:
        raise ValueError("filename byte limit must be positive")
    if isinstance(ordinal, bool) or ordinal <= 0:
        raise ValueError("collision ordinal must be positive")
    suffix = PurePath(name).suffix
    stem = name[: -len(suffix)] if suffix else name
    marker = f"__{asset_id}" if ordinal == 1 else f"__{asset_id}__{ordinal}"
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= limit:
        return _truncate_utf8(marker, limit)

    suffix_bytes = suffix.encode("utf-8")
    stem_budget = limit - len(marker_bytes) - len(suffix_bytes)
    if suffix and stem_budget >= 0:
        return f"{_truncate_utf8(stem, stem_budget)}{marker}{suffix}"
    return f"{_truncate_utf8(stem, limit - len(marker_bytes))}{marker}"


@dataclass(frozen=True, slots=True)
class Asset:
    id: str
    owner_id: str
    original_name: str
    mime_type: str
    size: int | None
    created_ns: int
    modified_ns: int
    updated_at: str
    checksum: str
    visibility: str
    is_trashed: bool
    is_offline: bool
    library_id: str | None

    @classmethod
    def from_api(cls, value: dict[str, object]) -> Asset:
        asset_id = str(value["id"])
        owner_id = str(value["ownerId"])
        UUID(asset_id)
        UUID(owner_id)
        exif = value.get("exifInfo") or {}
        if not isinstance(exif, dict):
            raise ValueError("asset exifInfo is not an object")
        raw_size = exif.get("fileSizeInByte")
        size = int(raw_size) if raw_size is not None else None
        if size is not None and size < 0:
            raise ValueError("asset size is negative")

        return cls(
            id=asset_id,
            owner_id=owner_id,
            original_name=str(value["originalFileName"]),
            mime_type=str(value["originalMimeType"]),
            size=size,
            created_ns=_nanoseconds(str(value["fileCreatedAt"])),
            modified_ns=_nanoseconds(str(value["fileModifiedAt"])),
            updated_at=str(value["updatedAt"]),
            checksum=str(value["checksum"]),
            visibility=str(value["visibility"]),
            is_trashed=bool(value["isTrashed"]),
            is_offline=bool(value["isOffline"]),
            library_id=str(value["libraryId"]) if value.get("libraryId") else None,
        )

    @property
    def visible(self) -> bool:
        return not self.is_trashed and not self.is_offline and self.visibility != "hidden" and self.size is not None
