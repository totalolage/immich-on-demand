from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import ssl
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx
import trio

from .model import Album, Asset, Person, timestamp_nanoseconds


CORE_READ_PERMISSIONS = frozenset(
    {"user.read", "asset.read", "asset.view", "asset.download"}
)
READ_PERMISSIONS = CORE_READ_PERMISSIONS | {"album.read", "person.read"}
UPLOAD_PERMISSIONS = CORE_READ_PERMISSIONS | {"asset.upload"}
MUTATION_PERMISSIONS = UPLOAD_PERMISSIONS | {"asset.copy", "asset.delete"}
LOGGER = logging.getLogger(__name__)
UPLOAD_MARKER_KEY = "immich-on-demand.upload"
UPLOAD_RETRY_STATUSES = frozenset({408, 425, 429}) | frozenset(range(500, 600))


class ImmichError(RuntimeError):
    pass


class ImmichPageLimitError(ImmichError):
    pass


class ImmichResponseError(ImmichError):
    pass


class ImmichUnavailableError(ImmichError):
    pass


class ImmichRetryableError(ImmichError):
    pass


def _contains_tls_error(error: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        pending.extend(
            related
            for related in (current.__cause__, current.__context__)
            if related is not None
        )
    return False


@dataclass(frozen=True, slots=True)
class ServerSession:
    owner_id: str
    version: str
    media_types: frozenset[str]
    trash_enabled: bool


@dataclass(frozen=True, slots=True)
class UploadResult:
    asset_id: str
    created: bool

    @property
    def status(self) -> str:
        return "created" if self.created else "duplicate"


class ImmichClient:
    def __init__(
        self,
        server_url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._origin = f"{server_url.rstrip('/')}/"
        self._api_root = urljoin(self._origin, "api/")
        self._http = httpx.AsyncClient(
            headers={"x-api-key": api_key},
            transport=transport,
            timeout=timeout or httpx.Timeout(30, connect=10, pool=10),
            follow_redirects=False,
        )

    async def __aenter__(self) -> ImmichClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry_statuses: frozenset[int] = frozenset(),
        passthrough_statuses: frozenset[int] = frozenset(),
        **kwargs: object,
    ) -> httpx.Response:
        url = urljoin(self._api_root, path.lstrip("/"))
        display_path = urlsplit(path).path.lstrip("/") if urlsplit(path).scheme else path
        try:
            response = await self._http.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            if _contains_tls_error(error):
                raise ImmichError("Immich TLS validation failed") from error
            raise ImmichUnavailableError("Immich is unavailable") from error
        except httpx.TransportError as error:
            raise ImmichError("Immich transport validation failed") from error
        if response.status_code in retry_statuses:
            LOGGER.info("Immich %s %s -> %s", method, display_path, response.status_code)
            raise ImmichRetryableError("Immich upload is temporarily unavailable")
        if response.status_code not in passthrough_statuses:
            self._raise_for_status(response, method, display_path)
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response, method: str, path: str) -> None:
        LOGGER.info("Immich %s %s -> %s", method, path, response.status_code)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            correlation = response.headers.get("x-correlation-id", "missing")
            raise ImmichError(
                f"Immich {method} {path} failed with {response.status_code}; correlation {correlation}"
            ) from error

    async def _json(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
        response = await self._request(method, path, **kwargs)
        try:
            value = response.json()
        except ValueError as error:
            raise ImmichResponseError(f"Immich {path} returned invalid JSON") from error
        if not isinstance(value, dict):
            raise ImmichResponseError(f"Immich {path} returned a non-object response")
        return value

    async def validate(
        self,
        required_permissions: frozenset[str] = READ_PERMISSIONS,
        *,
        exact_permissions: bool = True,
    ) -> ServerSession:
        discovery = await self._request(
            "GET", urljoin(self._origin, ".well-known/immich")
        )
        discovery_value = discovery.json()
        api = discovery_value.get("api") if isinstance(discovery_value, dict) else None
        endpoint = api.get("endpoint") if isinstance(api, dict) else None
        if not isinstance(endpoint, str):
            raise ImmichError("Immich discovery response has no API endpoint")
        api_root = urljoin(self._origin, endpoint)
        origin = urlsplit(self._origin)
        discovered = urlsplit(api_root)
        if (discovered.scheme, discovered.netloc) != (origin.scheme, origin.netloc):
            raise ImmichError("Immich discovery API endpoint is not on the configured origin")
        self._api_root = f"{api_root.rstrip('/')}/"

        version_value = await self._json("GET", "server/version")
        version_parts = tuple(version_value.get(name) for name in ("major", "minor", "patch"))
        if any(type(part) is not int for part in version_parts):
            raise ImmichError("Immich returned an invalid server version")
        version = ".".join(str(part) for part in version_parts)
        if version != "3.0.3":
            raise ImmichError(f"Immich 3.0.3 is required, server reports {version}")

        key_value = await self._json("GET", "api-keys/me")
        permissions_value = key_value.get("permissions")
        if not isinstance(permissions_value, list) or any(
            not isinstance(value, str) for value in permissions_value
        ):
            raise ImmichError("Immich returned invalid API key permissions")
        permissions = frozenset(permissions_value)
        missing = required_permissions - permissions
        extra = permissions - required_permissions
        if missing:
            raise ImmichError(f"API key is missing permissions: {', '.join(sorted(missing))}")
        if exact_permissions and extra:
            raise ImmichError(f"API key has unexpected permissions: {', '.join(sorted(extra))}")

        user_value = await self._json("GET", "users/me")
        owner_id = user_value.get("id")
        if not isinstance(owner_id, str):
            raise ImmichError("Immich returned an invalid user")
        try:
            UUID(owner_id)
        except ValueError as error:
            raise ImmichError("Immich returned an invalid user") from error
        media_value = await self._json("GET", "server/media-types")
        media_groups = tuple(media_value.get(kind) for kind in ("image", "video", "sidecar"))
        if any(
            not isinstance(group, list)
            or any(not isinstance(extension, str) for extension in group)
            for group in media_groups
        ):
            raise ImmichError("Immich returned invalid media types")
        media_types = frozenset(extension.lower() for group in media_groups for extension in group)
        feature_value = await self._json("GET", "server/features")
        trash_enabled = feature_value.get("trash")
        if type(trash_enabled) is not bool:
            raise ImmichError("Immich returned invalid server features")
        return ServerSession(owner_id, version, media_types, trash_enabled)

    async def asset_pages(
        self,
        owner_id: str,
        page_size: int = 1000,
        *,
        album_id: str | None = None,
        with_people: bool = False,
        updated_after_ms: int | None = None,
        allow_duplicate_ids: bool = False,
        page_limit: int | None = None,
    ) -> AsyncIterator[list[Asset]]:
        UUID(owner_id)
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        if album_id is not None:
            if not isinstance(album_id, str):
                raise ValueError("album_id must be a canonical UUID")
            try:
                canonical_album_id = str(UUID(album_id))
            except ValueError as error:
                raise ValueError("album_id must be a canonical UUID") from error
            if album_id != canonical_album_id:
                raise ValueError("album_id must be a canonical UUID")
        if type(with_people) is not bool:
            raise ValueError("with_people must be a boolean")
        if updated_after_ms is not None and (
            type(updated_after_ms) is not int or updated_after_ms < 0
        ):
            raise ValueError("updated_after_ms must be a non-negative integer")
        if type(allow_duplicate_ids) is not bool:
            raise ValueError("allow_duplicate_ids must be a boolean")
        if page_limit is not None and (type(page_limit) is not int or page_limit < 1):
            raise ValueError("page_limit must be a positive integer")
        page = 1
        seen_asset_ids: set[str] = set()
        while True:
            body: dict[str, object] = {
                "page": page,
                "size": page_size,
                "order": "asc",
                "withExif": True,
                "withDeleted": True,
                "withStacked": True,
            }
            if album_id is not None:
                body["albumIds"] = [album_id]
            if with_people:
                body["withPeople"] = True
            if updated_after_ms is not None:
                body["updatedAfter"] = datetime.fromtimestamp(
                    updated_after_ms / 1000, timezone.utc
                ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            value = await self._json(
                "POST",
                "search/metadata",
                json=body,
            )
            assets_value = value.get("assets")
            if not isinstance(assets_value, dict) or not isinstance(assets_value.get("items"), list):
                raise ImmichResponseError("Immich search response has no asset list")
            items = assets_value["items"]
            if any(not isinstance(item, dict) for item in items):
                raise ImmichResponseError(
                    "Immich search response contains a non-object asset"
                )
            if with_people and any("people" not in item for item in items):
                raise ImmichResponseError(
                    "Immich search response contains invalid asset people"
                )
            count = assets_value.get("count")
            if type(count) is not int or count != len(items):
                raise ImmichResponseError(
                    "Immich search response has an invalid asset count"
                )
            if "nextPage" not in assets_value:
                raise ImmichResponseError(
                    "Immich search response has no next page field"
                )
            next_page = assets_value["nextPage"]
            if next_page is not None and next_page != str(page + 1):
                raise ImmichResponseError(
                    "Immich search response has an invalid next page"
                )

            try:
                assets = [Asset.from_api(item) for item in items]
            except ValueError as error:
                raise ImmichResponseError(
                    "Immich search response contains an invalid asset"
                ) from error
            for asset in assets:
                if asset.id in seen_asset_ids and not allow_duplicate_ids:
                    raise ImmichResponseError(
                        "Immich search response contains a duplicate asset"
                    )
                seen_asset_ids.add(asset.id)
            yield [asset for asset in assets if asset.owner_id == owner_id]
            if next_page is None:
                return
            if page_limit is not None and page >= page_limit:
                raise ImmichPageLimitError("Immich search exceeded its page limit")
            page += 1

    async def albums(self, *, asset_id: str | None = None) -> list[Album]:
        if asset_id is not None:
            if not isinstance(asset_id, str):
                raise ValueError("asset_id must be a canonical UUID")
            try:
                canonical_asset_id = str(UUID(asset_id))
            except ValueError as error:
                raise ValueError("asset_id must be a canonical UUID") from error
            if canonical_asset_id != asset_id:
                raise ValueError("asset_id must be a canonical UUID")
        response = await self._request(
            "GET",
            "albums",
            params={"assetId": asset_id} if asset_id is not None else None,
        )
        try:
            value = response.json()
        except ValueError as error:
            raise ImmichResponseError("Immich returned invalid albums") from error
        if not isinstance(value, list):
            raise ImmichResponseError("Immich returned invalid albums")

        albums: list[Album] = []
        seen_ids: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                raise ImmichResponseError("Immich returned invalid albums")
            album_id = item.get("id")
            name = item.get("albumName")
            updated_at = item.get("updatedAt")
            asset_count = item.get("assetCount")
            if (
                not isinstance(album_id, str)
                or not isinstance(name, str)
                or not isinstance(updated_at, str)
                or type(asset_count) is not int
                or asset_count < 0
                or not isinstance(item.get("albumUsers"), list)
            ):
                raise ImmichResponseError("Immich returned invalid albums")
            try:
                canonical_id = str(UUID(album_id))
                timestamp_nanoseconds(updated_at)
            except ValueError as error:
                raise ImmichResponseError("Immich returned invalid albums") from error
            if canonical_id != album_id or album_id in seen_ids:
                raise ImmichResponseError("Immich returned invalid albums")
            seen_ids.add(album_id)
            albums.append(Album(album_id, name, updated_at, asset_count))
        return sorted(albums, key=lambda album: album.id)

    async def people(self) -> list[Person]:
        people: list[Person] = []
        seen_ids: set[str] = set()
        page = 1
        while True:
            response = await self._request(
                "GET",
                "people",
                params={"page": page, "size": 1000, "withHidden": "false"},
            )
            try:
                value = response.json()
            except ValueError as error:
                raise ImmichResponseError("Immich returned invalid people") from error
            required = {"total", "hidden", "people"}
            allowed = required | {"hasNextPage"}
            if (
                not isinstance(value, dict)
                or not required.issubset(value)
                or not set(value).issubset(allowed)
                or type(value.get("total")) is not int
                or value["total"] < 0
                or type(value.get("hidden")) is not int
                or value["hidden"] < 0
                or not isinstance(value.get("people"), list)
            ):
                raise ImmichResponseError("Immich returned invalid people")
            has_next_page = value.get("hasNextPage", False)
            items = value["people"]
            assert isinstance(items, list)
            if type(has_next_page) is not bool or has_next_page and not items:
                raise ImmichResponseError("Immich returned invalid people")

            for item in items:
                if not isinstance(item, dict):
                    raise ImmichResponseError("Immich returned invalid people")
                person_id = item.get("id")
                name = item.get("name")
                is_hidden = item.get("isHidden")
                updated_at = item.get("updatedAt") if "updatedAt" in item else None
                if (
                    not isinstance(person_id, str)
                    or not isinstance(name, str)
                    or type(is_hidden) is not bool
                    or is_hidden
                    or "updatedAt" in item
                    and not isinstance(updated_at, str)
                ):
                    raise ImmichResponseError("Immich returned invalid people")
                try:
                    canonical_id = str(UUID(person_id))
                    if updated_at is not None:
                        timestamp_nanoseconds(updated_at)
                except ValueError as error:
                    raise ImmichResponseError("Immich returned invalid people") from error
                if canonical_id != person_id or person_id in seen_ids:
                    raise ImmichResponseError("Immich returned invalid people")
                seen_ids.add(person_id)
                people.append(Person(person_id, name, is_hidden, updated_at))

            if not has_next_page:
                return sorted(people, key=lambda person: person.id)
            page += 1

    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
        UUID(asset_id)
        path = f"assets/{asset_id}/thumbnail"
        url = urljoin(self._api_root, path)
        preview = bytearray()
        async with self._http.stream(
            "GET",
            url,
            params={"size": "preview", "edited": "false"},
            headers={"accept-encoding": "identity"},
        ) as response:
            self._raise_for_status(response, "GET", path)
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ImmichError("Immich preview ignored identity content encoding")
            async with aclosing(response.stream.__aiter__()) as chunks:
                async for chunk in chunks:
                    if len(preview) + len(chunk) > 32 * 1024**2:
                        raise ImmichError("Immich preview exceeds 32 MiB")
                    preview.extend(chunk)
        return bytes(preview), response.headers.get("content-type", "application/octet-stream")

    async def asset(self, asset_id: str) -> Asset:
        UUID(asset_id)
        try:
            result = Asset.from_api(
                await self._json(
                    "GET",
                    f"assets/{asset_id}",
                    retry_statuses=UPLOAD_RETRY_STATUSES,
                )
            )
        except ValueError as error:
            raise ImmichResponseError("Immich returned an invalid upload candidate") from error
        if result.id != asset_id:
            raise ImmichResponseError("Immich returned an invalid upload candidate")
        return result

    async def asset_metadata(self, asset_id: str) -> str:
        if not isinstance(asset_id, str):
            raise ValueError("asset ID must be a canonical UUID")
        try:
            canonical_asset_id = str(UUID(asset_id))
        except ValueError as error:
            raise ValueError("asset ID must be a canonical UUID") from error
        if canonical_asset_id != asset_id:
            raise ValueError("asset ID must be a canonical UUID")
        response = await self._request(
            "GET",
            f"assets/{asset_id}/metadata",
            retry_statuses=UPLOAD_RETRY_STATUSES,
        )
        try:
            value = response.json()
        except ValueError as error:
            raise ImmichResponseError("Immich returned invalid upload metadata") from error
        if not isinstance(value, list):
            raise ImmichResponseError("Immich returned invalid upload metadata")
        markers: list[dict[str, object]] = []
        for item in value:
            if (
                not isinstance(item, dict)
                or set(item) != {"key", "value", "updatedAt"}
                or not isinstance(item.get("key"), str)
                or not isinstance(item.get("value"), dict)
                or not isinstance(item.get("updatedAt"), str)
            ):
                raise ImmichResponseError("Immich returned invalid upload metadata")
            try:
                timestamp_nanoseconds(item["updatedAt"])
            except ValueError as error:
                raise ImmichResponseError("Immich returned invalid upload metadata") from error
            if item["key"] == UPLOAD_MARKER_KEY:
                marker = item["value"]
                assert isinstance(marker, dict)
                markers.append(marker)
        if len(markers) != 1:
            raise ImmichResponseError("Immich returned invalid upload metadata")
        marker = markers[0]
        upload_id = marker.get("uploadId")
        if (
            set(marker) != {"formatVersion", "uploadId"}
            or type(marker.get("formatVersion")) is not int
            or marker["formatVersion"] != 1
            or not isinstance(upload_id, str)
        ):
            raise ImmichResponseError("Immich returned invalid upload metadata")
        try:
            canonical_upload_id = str(UUID(upload_id))
        except ValueError as error:
            raise ImmichResponseError("Immich returned invalid upload metadata") from error
        if canonical_upload_id != upload_id:
            raise ImmichResponseError("Immich returned invalid upload metadata")
        return upload_id

    async def copy_albums(
        self, source_asset_id: str, target_asset_id: str
    ) -> None:
        for label, asset_id in (
            ("source asset ID", source_asset_id),
            ("target asset ID", target_asset_id),
        ):
            if not isinstance(asset_id, str):
                raise ValueError(f"{label} must be a canonical UUID")
            try:
                canonical_asset_id = str(UUID(asset_id))
            except ValueError as error:
                raise ValueError(f"{label} must be a canonical UUID") from error
            if canonical_asset_id != asset_id:
                raise ValueError(f"{label} must be a canonical UUID")
        response = await self._request(
            "PUT",
            "assets/copy",
            json={
                "sourceId": source_asset_id,
                "targetId": target_asset_id,
                "albums": True,
                "favorite": False,
                "sharedLinks": False,
                "sidecar": False,
                "stack": False,
            },
        )
        if response.status_code != 204 or response.content:
            raise ImmichResponseError("Immich returned an invalid album copy response")

    async def upload(
        self,
        descriptor: int,
        requested_name: str,
        media_types: frozenset[str],
        upload_id: str,
        *,
        replacement_source: Asset | None = None,
    ) -> UploadResult:
        if not isinstance(requested_name, str):
            raise ValueError("requested upload name must be a string")
        extension = Path(requested_name).suffix.lower()
        if extension not in media_types:
            raise ImmichError(f"Immich does not accept the {extension or 'extensionless'} file type")
        if not isinstance(upload_id, str):
            raise ValueError("upload ID must be a canonical UUID")
        try:
            canonical_upload_id = str(UUID(upload_id))
        except ValueError as error:
            raise ValueError("upload ID must be a canonical UUID") from error
        if canonical_upload_id != upload_id:
            raise ValueError("upload ID must be a canonical UUID")
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("upload payload descriptor is invalid")
        if replacement_source is not None and (
            not isinstance(replacement_source, Asset)
            or type(replacement_source.created_ns) is not int
            or type(replacement_source.is_favorite) is not bool
            or not isinstance(replacement_source.visibility, str)
            or replacement_source.visibility
            not in {"archive", "hidden", "locked", "timeline"}
        ):
            raise ValueError("replacement source has invalid metadata")
        checksum = await trio.to_thread.run_sync(_sha1, descriptor)
        stats = os.fstat(descriptor)
        if replacement_source is None:
            created = datetime.fromtimestamp(stats.st_ctime, timezone.utc).isoformat()
        else:
            seconds, nanoseconds = divmod(replacement_source.created_ns, 1_000_000_000)
            try:
                created = (
                    datetime.fromtimestamp(seconds, timezone.utc)
                    .replace(microsecond=nanoseconds // 1000)
                    .isoformat()
                )
            except (OverflowError, OSError, ValueError) as error:
                raise ValueError("replacement source has invalid metadata") from error
        modified = datetime.fromtimestamp(stats.st_mtime, timezone.utc).isoformat()
        metadata = json.dumps(
            [
                {
                    "key": UPLOAD_MARKER_KEY,
                    "value": {"formatVersion": 1, "uploadId": upload_id},
                }
            ],
            separators=(",", ":"),
        )
        data = {
            "fileCreatedAt": created,
            "fileModifiedAt": modified,
            "filename": requested_name,
            "metadata": metadata,
        }
        if replacement_source is not None:
            data["isFavorite"] = "true" if replacement_source.is_favorite else "false"
            data["visibility"] = replacement_source.visibility
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            stream.seek(0)
            response = await self._request(
                "POST",
                "assets",
                retry_statuses=UPLOAD_RETRY_STATUSES,
                passthrough_statuses=frozenset(range(200, 400)),
                headers={"x-immich-checksum": base64.b64encode(checksum).decode("ascii")},
                data=data,
                files={
                    "assetData": (
                        requested_name,
                        stream,
                        "application/octet-stream",
                    )
                },
            )
        try:
            value = response.json()
        except ValueError as error:
            raise ImmichResponseError("Immich returned an invalid upload response") from error
        expected_status = {200: "duplicate", 201: "created"}.get(response.status_code)
        if (
            expected_status is None
            or not isinstance(value, dict)
            or set(value) != {"status", "id"}
            or value.get("status") != expected_status
            or not isinstance(value.get("id"), str)
        ):
            raise ImmichResponseError("Immich returned an invalid upload response")
        asset_id = value["id"]
        assert isinstance(asset_id, str)
        try:
            canonical_asset_id = str(UUID(asset_id))
        except ValueError as error:
            raise ImmichResponseError("Immich returned an invalid upload response") from error
        if canonical_asset_id != asset_id:
            raise ImmichResponseError("Immich returned an invalid upload response")
        return UploadResult(asset_id, expected_status == "created")

    async def trash(self, asset_id: str) -> None:
        UUID(asset_id)
        features = await self._json("GET", "server/features")
        if features.get("trash") is not True:
            raise ImmichError("Immich trash is disabled; refusing remote deletion")
        await self._request("DELETE", "assets", json={"ids": [asset_id], "force": False})

    async def restore(self, asset_id: str) -> None:
        UUID(asset_id)
        features = await self._json("GET", "server/features")
        if features.get("trash") is not True:
            raise ImmichError("Immich trash is disabled; refusing restore")
        value = await self._json("POST", "trash/restore/assets", json={"ids": [asset_id]})
        if type(value.get("count")) is not int or value["count"] != 1:
            raise ImmichError("Immich did not restore the requested asset")

    @asynccontextmanager
    async def original(self, asset_id: str) -> AsyncIterator[httpx.Response]:
        UUID(asset_id)
        url = urljoin(self._api_root, f"assets/{asset_id}/original")
        async with self._http.stream("GET", url, params={"edited": "false"}) as response:
            self._raise_for_status(response, "GET", f"assets/{asset_id}/original")
            yield response


def _sha1(descriptor: int) -> bytes:
    digest = hashlib.sha1(usedforsecurity=False)
    offset = 0
    while block := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(block)
        offset += len(block)
    return digest.digest()
