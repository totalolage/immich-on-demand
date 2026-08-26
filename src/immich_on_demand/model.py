from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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


def timestamp_nanoseconds(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("asset timestamp has no timezone")
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (
        (delta.days * 24 * 60 * 60 + delta.seconds) * 1_000_000
        + delta.microseconds
    ) * 1000


def _string(value: dict[str, object], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str):
        raise ValueError(f"asset {name} is not a string")
    return result


def _boolean(value: dict[str, object], name: str) -> bool:
    result = value.get(name)
    if type(result) is not bool:
        raise ValueError(f"asset {name} is not a boolean")
    return result


def _local_date(value: dict[str, object]) -> str:
    timestamp = _string(value, "localDateTime")
    if "T" not in timestamp:
        raise ValueError("asset localDateTime is not a datetime")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("asset localDateTime is not a datetime") from error
    return parsed.date().isoformat()


def _person_ids(value: dict[str, object]) -> tuple[str, ...]:
    if "people" not in value:
        return ()
    people = value["people"]
    if not isinstance(people, list):
        raise ValueError("asset people is not a list")
    person_ids: set[str] = set()
    for person in people:
        if not isinstance(person, dict):
            raise ValueError("asset person is not an object")
        person_id = _string(person, "id")
        try:
            canonical_id = str(UUID(person_id))
        except ValueError as error:
            raise ValueError("asset person id is not a UUID") from error
        if person_id != canonical_id:
            raise ValueError("asset person id is not canonical")
        person_ids.add(person_id)
    return tuple(sorted(person_ids))


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
class Album:
    id: str
    name: str
    updated_at: str
    asset_count: int


@dataclass(frozen=True, slots=True)
class Person:
    id: str
    name: str
    is_hidden: bool
    updated_at: str | None


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
    local_date: str | None = None
    is_favorite: bool = False
    person_ids: tuple[str, ...] = ()
    live_photo_video_id: str | None = None

    @classmethod
    def from_api(cls, value: dict[str, object]) -> Asset:
        asset_id = _string(value, "id")
        owner_id = _string(value, "ownerId")
        UUID(asset_id)
        UUID(owner_id)
        exif = value.get("exifInfo")
        if exif is None:
            exif = {}
        if not isinstance(exif, dict):
            raise ValueError("asset exifInfo is not an object")
        raw_size = exif.get("fileSizeInByte")
        if raw_size is not None and type(raw_size) is not int:
            raise ValueError("asset size is not an integer")
        size = raw_size
        if size is not None and size < 0:
            raise ValueError("asset size is negative")
        created_at = _string(value, "fileCreatedAt")
        modified_at = _string(value, "fileModifiedAt")
        updated_at = _string(value, "updatedAt")
        timestamp_nanoseconds(updated_at)
        library_id = value.get("libraryId")
        if library_id is not None:
            if not isinstance(library_id, str):
                raise ValueError("asset libraryId is not a string")
            UUID(library_id)
        live_photo_video_id = value.get("livePhotoVideoId")
        if live_photo_video_id is not None:
            if not isinstance(live_photo_video_id, str):
                raise ValueError("asset livePhotoVideoId is not a string")
            try:
                canonical_live_photo_video_id = str(UUID(live_photo_video_id))
            except ValueError as error:
                raise ValueError("asset livePhotoVideoId is not a UUID") from error
            if (
                canonical_live_photo_video_id != live_photo_video_id
                or live_photo_video_id == asset_id
            ):
                raise ValueError("asset livePhotoVideoId is not a related canonical UUID")

        return cls(
            id=asset_id,
            owner_id=owner_id,
            original_name=_string(value, "originalFileName"),
            mime_type=_string(value, "originalMimeType"),
            size=size,
            created_ns=timestamp_nanoseconds(created_at),
            modified_ns=timestamp_nanoseconds(modified_at),
            updated_at=updated_at,
            checksum=_string(value, "checksum"),
            visibility=_string(value, "visibility"),
            is_trashed=_boolean(value, "isTrashed"),
            is_offline=_boolean(value, "isOffline"),
            library_id=library_id,
            local_date=_local_date(value),
            is_favorite=_boolean(value, "isFavorite"),
            person_ids=_person_ids(value),
            live_photo_video_id=live_photo_video_id,
        )

    @property
    def visible(self) -> bool:
        return not self.is_trashed and not self.is_offline and self.visibility != "hidden" and self.size is not None
