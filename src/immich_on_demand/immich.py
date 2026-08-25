from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
from urllib.parse import urljoin
from uuid import UUID

import httpx

from .model import Asset


READ_PERMISSIONS = frozenset({"user.read", "asset.read", "asset.view", "asset.download"})
MUTATION_PERMISSIONS = READ_PERMISSIONS | {"asset.upload", "asset.delete"}
LOGGER = logging.getLogger(__name__)


class ImmichError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServerSession:
    owner_id: str
    version: str
    media_types: frozenset[str]
    trash_enabled: bool


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

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        url = urljoin(self._api_root, path.lstrip("/"))
        response = await self._http.request(method, url, **kwargs)
        LOGGER.info("Immich %s %s -> %s", method, path, response.status_code)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            correlation = response.headers.get("x-correlation-id", "missing")
            raise ImmichError(
                f"Immich {method} {path} failed with {response.status_code}; correlation {correlation}"
            ) from error
        return response

    async def _json(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
        response = await self._request(method, path, **kwargs)
        value = response.json()
        if not isinstance(value, dict):
            raise ImmichError(f"Immich {path} returned a non-object response")
        return value

    async def validate(
        self,
        required_permissions: frozenset[str] = READ_PERMISSIONS,
        *,
        exact_permissions: bool = True,
    ) -> ServerSession:
        discovery = await self._http.get(urljoin(self._origin, ".well-known/immich"))
        discovery.raise_for_status()
        endpoint = discovery.json().get("api", {}).get("endpoint")
        if not isinstance(endpoint, str):
            raise ImmichError("Immich discovery response has no API endpoint")
        self._api_root = f"{urljoin(self._origin, endpoint).rstrip('/')}/"

        version_value = await self._json("GET", "server/version")
        version = ".".join(str(version_value[name]) for name in ("major", "minor", "patch"))
        if version != "3.0.3":
            raise ImmichError(f"Immich 3.0.3 is required, server reports {version}")

        key_value = await self._json("GET", "api-keys/me")
        permissions = frozenset(str(value) for value in key_value.get("permissions", []))
        missing = required_permissions - permissions
        extra = permissions - required_permissions
        if missing:
            raise ImmichError(f"API key is missing permissions: {', '.join(sorted(missing))}")
        if exact_permissions and extra:
            raise ImmichError(f"API key has unexpected permissions: {', '.join(sorted(extra))}")

        user_value = await self._json("GET", "users/me")
        owner_id = str(user_value["id"])
        UUID(owner_id)
        media_value = await self._json("GET", "server/media-types")
        media_types = frozenset(
            str(extension).lower()
            for kind in ("image", "video", "sidecar")
            for extension in media_value.get(kind, [])
        )
        feature_value = await self._json("GET", "server/features")
        return ServerSession(owner_id, version, media_types, bool(feature_value.get("trash")))

    async def asset_pages(self, owner_id: str, page_size: int = 1000) -> AsyncIterator[list[Asset]]:
        UUID(owner_id)
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        page = 1
        while True:
            value = await self._json(
                "POST",
                "search/metadata",
                json={
                    "page": page,
                    "size": page_size,
                    "order": "asc",
                    "withExif": True,
                    "withDeleted": True,
                    "withStacked": True,
                },
            )
            assets_value = value.get("assets")
            if not isinstance(assets_value, dict) or not isinstance(assets_value.get("items"), list):
                raise ImmichError("Immich search response has no asset list")
            assets = [Asset.from_api(item) for item in assets_value["items"] if isinstance(item, dict)]
            yield [asset for asset in assets if asset.owner_id == owner_id]
            next_page = assets_value.get("nextPage")
            if next_page is None:
                return
            page = int(next_page)

    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
        UUID(asset_id)
        response = await self._request(
            "GET", f"assets/{asset_id}/thumbnail", params={"size": "preview", "edited": "false"}
        )
        if len(response.content) > 32 * 1024**2:
            raise ImmichError("Immich preview exceeds 32 MiB")
        return response.content, response.headers.get("content-type", "application/octet-stream")

    @asynccontextmanager
    async def original(self, asset_id: str) -> AsyncIterator[httpx.Response]:
        UUID(asset_id)
        url = urljoin(self._api_root, f"assets/{asset_id}/original")
        async with self._http.stream("GET", url, params={"edited": "false"}) as response:
            LOGGER.info("Immich GET original %s -> %s", asset_id, response.status_code)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise ImmichError(
                    f"Immich original download failed with {response.status_code}"
                ) from error
            yield response
