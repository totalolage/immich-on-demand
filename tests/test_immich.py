import unittest

import httpx
import trio

from immich_on_demand.immich import ImmichClient, ImmichError


OWNER_ID = "87654321-4321-4321-8321-cba987654321"
ASSET_ID = "12345678-1234-4234-8234-123456789abc"


def asset(asset_id: str = ASSET_ID) -> dict[str, object]:
    return {
        "id": asset_id,
        "ownerId": OWNER_ID,
        "originalFileName": "photo.jpg",
        "originalMimeType": "image/jpeg",
        "fileCreatedAt": "2026-08-25T10:00:00Z",
        "fileModifiedAt": "2026-08-25T11:00:00Z",
        "updatedAt": "2026-08-25T12:00:00Z",
        "checksum": "abc=",
        "visibility": "timeline",
        "isTrashed": False,
        "isOffline": False,
        "libraryId": None,
        "exifInfo": {"fileSizeInByte": 123},
    }


class ImmichClientTest(unittest.TestCase):
    def test_validates_and_paginates(self) -> None:
        seen_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["x-api-key"], "secret")
            path = request.url.path
            if path == "/.well-known/immich":
                return httpx.Response(200, json={"api": {"endpoint": "/api"}})
            if path == "/api/server/version":
                return httpx.Response(200, json={"major": 3, "minor": 0, "patch": 3})
            if path == "/api/api-keys/me":
                return httpx.Response(
                    200,
                    json={"permissions": ["user.read", "asset.read", "asset.view", "asset.download"]},
                )
            if path == "/api/users/me":
                return httpx.Response(200, json={"id": OWNER_ID})
            if path == "/api/server/media-types":
                return httpx.Response(200, json={"image": [".jpg"], "video": [".mp4"], "sidecar": []})
            if path == "/api/server/features":
                return httpx.Response(200, json={"trash": True})
            if path == "/api/search/metadata":
                page = request.read().decode()
                requested_page = 2 if '"page":2' in page else 1
                seen_pages.append(requested_page)
                return httpx.Response(
                    200,
                    json={"assets": {"items": [asset()], "nextPage": 2 if requested_page == 1 else None}},
                )
            raise AssertionError(path)

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                session = await client.validate()
                pages = [page async for page in client.asset_pages(session.owner_id)]
                self.assertEqual(session.version, "3.0.3")
                self.assertEqual(session.media_types, frozenset({".jpg", ".mp4"}))
                self.assertEqual([page[0].id for page in pages], [ASSET_ID, ASSET_ID])

        trio.run(scenario)
        self.assertEqual(seen_pages, [1, 2])

    def test_rejects_extra_permissions_for_read_only_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            responses = {
                "/.well-known/immich": {"api": {"endpoint": "/api"}},
                "/api/server/version": {"major": 3, "minor": 0, "patch": 3},
                "/api/api-keys/me": {
                    "permissions": [
                        "user.read",
                        "asset.read",
                        "asset.view",
                        "asset.download",
                        "asset.delete",
                    ]
                },
            }
            return httpx.Response(200, json=responses[request.url.path])

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaisesRegex(ImmichError, "unexpected permissions: asset.delete"):
                    await client.validate()

        trio.run(scenario)
