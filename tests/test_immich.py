import unittest
from contextlib import aclosing
import logging
from pathlib import Path
import tempfile

import httpx
import trio

from immich_on_demand.immich import ImmichClient, ImmichError


OWNER_ID = "87654321-4321-4321-8321-cba987654321"
ASSET_ID = "12345678-1234-4234-8234-123456789abc"
OTHER_ID = "22345678-1234-4234-8234-123456789abc"


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
    def test_rejects_non_object_asset_items(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "assets": {
                        "items": [asset(), "not-an-asset"],
                        "count": 2,
                        "nextPage": None,
                    }
                },
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                async with aclosing(client.asset_pages(OWNER_ID)) as pages:
                    with self.assertRaisesRegex(ImmichError, "non-object asset"):
                        await anext(pages)

        trio.run(scenario)

    def test_rejects_invalid_next_pages(self) -> None:
        async def scenario(next_page: object) -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={"assets": {"items": [asset()], "count": 1, "nextPage": next_page}},
                )

            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                async with aclosing(client.asset_pages(OWNER_ID)) as pages:
                    with self.assertRaisesRegex(ImmichError, "invalid next page"):
                        await anext(pages)

        for next_page in (True, 2, 2.0, "02", "2.0", " 2", 1, "1", 0, -1, "3"):
            with self.subTest(next_page=next_page):
                trio.run(scenario, next_page)

    def test_rejects_duplicate_asset_ids_across_pages(self) -> None:
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(
                200,
                json={
                    "assets": {
                        "items": [asset()],
                        "count": 1,
                        "nextPage": "2" if requests == 1 else None,
                    }
                },
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                async with aclosing(client.asset_pages(OWNER_ID)) as pages:
                    await anext(pages)
                    with self.assertRaisesRegex(ImmichError, "duplicate asset"):
                        await anext(pages)

        trio.run(scenario)
        self.assertEqual(requests, 2)

    def test_requires_an_explicit_terminal_page_and_matching_count(self) -> None:
        async def scenario(assets_value: dict[str, object], message: str) -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={"assets": assets_value})

            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                async with aclosing(client.asset_pages(OWNER_ID)) as pages:
                    with self.assertRaisesRegex(ImmichError, message):
                        await anext(pages)

        cases = (
            ({"items": [], "count": 0}, "next page"),
            ({"items": [], "count": True, "nextPage": None}, "count"),
            ({"items": [], "count": "0", "nextPage": None}, "count"),
            ({"items": [], "count": 1, "nextPage": None}, "count"),
        )
        for response, message in cases:
            with self.subTest(response=response):
                trio.run(scenario, response, message)

    def test_accepts_an_explicit_full_terminal_page(self) -> None:
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(
                200,
                json={"assets": {"items": [asset()], "count": 1, "nextPage": None}},
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                pages = [page async for page in client.asset_pages(OWNER_ID, page_size=1)]
                self.assertEqual([[item.id for item in page] for page in pages], [[ASSET_ID]])

        trio.run(scenario)
        self.assertEqual(requests, 1)

    def test_thumbnail_stream_stops_at_the_memory_limit(self) -> None:
        class Oversized(httpx.AsyncByteStream):
            chunks = 0

            def __aiter__(self):
                return self

            async def __anext__(self) -> bytes:
                if self.chunks == 10_000:
                    raise StopAsyncIteration
                self.chunks += 1
                return b"x" * 4096

        stream = Oversized()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaisesRegex(ImmichError, "exceeds 32 MiB"):
                    await client.thumbnail(ASSET_ID)

        trio.run(scenario)
        self.assertEqual(stream.chunks, 8193)

    def test_rejects_a_cross_origin_discovery_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(
                200, json={"api": {"endpoint": "https://attacker.example/api"}}
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaisesRegex(ImmichError, "configured origin"):
                    await client.validate()

        trio.run(scenario)
        self.assertEqual(seen, ["https://photos.example.test/.well-known/immich"])

    def test_discovery_failure_uses_sanitized_status_and_correlation_errors(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                503,
                headers={"x-correlation-id": "request-7"},
                text="internal response with secret",
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "api-secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertLogs(
                    "immich_on_demand.immich", level=logging.INFO
                ) as logs, self.assertRaises(ImmichError) as raised:
                    await client.validate()

            message = str(raised.exception)
            self.assertEqual(
                message,
                "Immich GET .well-known/immich failed with 503; correlation request-7",
            )
            output = "\n".join(logs.output)
            self.assertIn("Immich GET .well-known/immich -> 503", output)
            self.assertNotIn("photos.example.test", message + output)
            self.assertNotIn("api-secret", message + output)
            self.assertNotIn("internal response", message + output)

        trio.run(scenario)

    def test_rejects_malformed_validation_fields_without_coercion(self) -> None:
        base = {
            "/.well-known/immich": {"api": {"endpoint": "/api"}},
            "/api/server/version": {"major": 3, "minor": 0, "patch": 3},
            "/api/api-keys/me": {
                "permissions": ["user.read", "asset.read", "asset.view", "asset.download"]
            },
            "/api/users/me": {"id": OWNER_ID},
            "/api/server/media-types": {"image": [".jpg"], "video": [], "sidecar": []},
            "/api/server/features": {"trash": True},
        }
        cases = (
            ("/api/server/version", {"major": "3", "minor": 0, "patch": 3}, "version"),
            (
                "/api/api-keys/me",
                {
                    "permissions": [
                        "user.read",
                        "asset.read",
                        "asset.view",
                        "asset.download",
                        7,
                    ]
                },
                "permissions",
            ),
            ("/api/users/me", {"id": 123}, "user"),
            (
                "/api/server/media-types",
                {"image": [123], "video": [], "sidecar": []},
                "media types",
            ),
            ("/api/server/features", {"trash": 1}, "features"),
        )

        async def scenario(changed_path: str, changed: object, message: str) -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                value = changed if request.url.path == changed_path else base[request.url.path]
                return httpx.Response(200, json=value)

            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaisesRegex(ImmichError, message):
                    await client.validate()

        for changed_path, changed, message in cases:
            with self.subTest(path=changed_path):
                trio.run(scenario, changed_path, changed, message)

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
                    json={
                        "assets": {
                            "items": [asset(ASSET_ID if requested_page == 1 else OTHER_ID)],
                            "count": 1,
                            "nextPage": "2" if requested_page == 1 else None,
                        }
                    },
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
                self.assertEqual([page[0].id for page in pages], [ASSET_ID, OTHER_ID])

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

    def test_upload_checks_duplicates_before_sending_bytes(self) -> None:
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            if request.url.path == "/api/assets/bulk-upload-check":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "photo.jpg",
                                "action": "reject",
                                "reason": "duplicate",
                                "assetId": ASSET_ID,
                                "isTrashed": False,
                            }
                        ]
                    },
                )
            raise AssertionError(request.url.path)

        async def scenario(path: Path) -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                result = await client.upload(path, frozenset({".jpg"}))
                self.assertEqual(result.asset_id, ASSET_ID)
                self.assertFalse(result.created)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"content")
            trio.run(scenario, path)
        self.assertEqual(requests, ["/api/assets/bulk-upload-check"])

    def test_trash_refetches_feature_and_never_requests_permanent_deletion(self) -> None:
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.url.path == "/api/server/features":
                return httpx.Response(200, json={"trash": True})
            self.assertEqual((request.method, request.url.path), ("DELETE", "/api/assets"))
            self.assertEqual(request.read(), b'{"ids":["12345678-1234-4234-8234-123456789abc"],"force":false}')
            return httpx.Response(204)

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                await client.trash(ASSET_ID)
                await client.trash(ASSET_ID)

        trio.run(scenario)
        self.assertEqual(
            requests,
            [
                ("GET", "/api/server/features"),
                ("DELETE", "/api/assets"),
                ("GET", "/api/server/features"),
                ("DELETE", "/api/assets"),
            ],
        )

    def test_trash_requires_literal_true_from_fresh_feature_response(self) -> None:
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            return httpx.Response(200, json={"trash": 1})

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaisesRegex(ImmichError, "trash is disabled"):
                    await client.trash(ASSET_ID)

        trio.run(scenario)
        self.assertEqual(requests, [("GET", "/api/server/features")])

    def test_restore_requires_a_literal_integer_count(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"count": True})

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaisesRegex(ImmichError, "did not restore"):
                    await client.restore(ASSET_ID)

        trio.run(scenario)
