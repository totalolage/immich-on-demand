import unittest
from contextlib import aclosing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import ssl
import tempfile

import httpx
import trio

from immich_on_demand.immich import (
    ImmichClient,
    ImmichError,
    ImmichPageLimitError,
    ImmichResponseError,
    ImmichRetryableError,
    ImmichUnavailableError,
    READ_PERMISSIONS,
)
from immich_on_demand.model import Album, Person


OWNER_ID = "87654321-4321-4321-8321-cba987654321"
ASSET_ID = "12345678-1234-4234-8234-123456789abc"
OTHER_ID = "22345678-1234-4234-8234-123456789abc"
UPLOAD_ID = "32345678-1234-4234-8234-123456789abc"
ALBUM_ID = "42345678-1234-4234-8234-123456789abc"


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
        "isFavorite": False,
        "localDateTime": "2026-08-25T10:00:00",
        "libraryId": None,
        "exifInfo": {"fileSizeInByte": 123},
    }


def album(
    album_id: str = ALBUM_ID,
    *,
    name: object = "Holiday",
    updated_at: object = "2026-08-25T12:00:00Z",
    asset_count: object = 2,
    album_users: object = (),
) -> dict[str, object]:
    return {
        "id": album_id,
        "albumName": name,
        "updatedAt": updated_at,
        "assetCount": asset_count,
        "albumUsers": list(album_users) if isinstance(album_users, tuple) else album_users,
    }


def person(
    person_id: str = ALBUM_ID,
    *,
    name: object = "Filip",
    is_hidden: object = False,
    updated_at: object = "2026-08-25T12:00:00Z",
) -> dict[str, object]:
    return {
        "id": person_id,
        "name": name,
        "isHidden": is_hidden,
        "updatedAt": updated_at,
    }


class ImmichClientTest(unittest.TestCase):
    def test_people_validates_paginates_and_sorts_the_inventory(self) -> None:
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            self.assertEqual((request.method, request.url.path), ("GET", "/api/people"))
            self.assertEqual(
                request.url.query,
                f"page={requests}&size=1000&withHidden=false".encode(),
            )
            if requests == 1:
                return httpx.Response(
                    200,
                    json={
                        "total": 2,
                        "hidden": 0,
                        "people": [person(ALBUM_ID, name="Zoo")],
                        "hasNextPage": True,
                    },
                )
            without_timestamp = person(ASSET_ID, name="Alpha")
            del without_timestamp["updatedAt"]
            return httpx.Response(
                200,
                json={"total": 2, "hidden": 0, "people": [without_timestamp]},
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                self.assertEqual(
                    await client.people(),
                    [
                        Person(ASSET_ID, "Alpha", False, None),
                        Person(ALBUM_ID, "Zoo", False, "2026-08-25T12:00:00Z"),
                    ],
                )

        trio.run(scenario)
        self.assertEqual(requests, 2)

    def test_people_rejects_invalid_objects_and_empty_continuations(self) -> None:
        valid_page = {"total": 1, "hidden": 0, "people": [person()]}
        cases: tuple[object, ...] = (
            [],
            {**valid_page, "extra": True},
            {"hidden": 0, "people": []},
            {"total": True, "hidden": 0, "people": []},
            {"total": -1, "hidden": 0, "people": []},
            {"total": 0, "hidden": True, "people": []},
            {"total": 0, "hidden": -1, "people": []},
            {"total": 0, "hidden": 0, "people": {}},
            {"total": 1, "hidden": 0, "people": ["not-an-object"]},
            {"total": 1, "hidden": 0, "people": [person(ALBUM_ID.upper())]},
            {"total": 2, "hidden": 0, "people": [person(), person()]},
            {"total": 1, "hidden": 0, "people": [person(name=True)]},
            {"total": 1, "hidden": 0, "people": [person(is_hidden=True)]},
            {"total": 1, "hidden": 0, "people": [person(is_hidden=0)]},
            {"total": 1, "hidden": 0, "people": [person(updated_at=None)]},
            {
                "total": 1,
                "hidden": 0,
                "people": [person(updated_at="2026-08-25T12:00:00")],
            },
            {**valid_page, "hasNextPage": 1},
            {**valid_page, "hasNextPage": "true"},
            {"total": 0, "hidden": 0, "people": [], "hasNextPage": True},
        )

        async def scenario(response: object) -> None:
            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json=response)
                ),
            ) as client:
                with self.assertRaisesRegex(
                    ImmichResponseError, "^Immich returned invalid people$"
                ):
                    await client.people()

        for response in cases:
            with self.subTest(response=response):
                trio.run(scenario, response)

    def test_people_rejects_duplicate_ids_across_pages(self) -> None:
        requests = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "hidden": 0,
                    "people": [person()],
                    "hasNextPage": requests == 1,
                },
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaisesRegex(
                    ImmichResponseError, "^Immich returned invalid people$"
                ):
                    await client.people()

        trio.run(scenario)
        self.assertEqual(requests, 2)

    def test_albums_validates_and_sorts_the_complete_inventory(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual((request.method, request.url.path), ("GET", "/api/albums"))
            self.assertEqual(request.url.query, b"")
            return httpx.Response(
                200,
                json=[
                    album(ALBUM_ID, name="Zoo", asset_count=0),
                    album(ASSET_ID, name="Alpha", album_users=[{"role": "owner"}]),
                ],
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                self.assertEqual(
                    await client.albums(),
                    [
                        Album(ASSET_ID, "Alpha", "2026-08-25T12:00:00Z", 2),
                        Album(ALBUM_ID, "Zoo", "2026-08-25T12:00:00Z", 0),
                    ],
                )

        trio.run(scenario)

    def test_albums_rejects_malformed_or_duplicate_records(self) -> None:
        cases: tuple[object, ...] = (
            {},
            ["not-an-object"],
            [album(ALBUM_ID.upper())],
            [album(), album()],
            [album(name=True)],
            [album(updated_at=True)],
            [album(updated_at="2026-08-25T12:00:00")],
            [album(asset_count=True)],
            [album(asset_count=-1)],
            [album(asset_count=2.0)],
            [album(album_users={})],
        )

        async def scenario(response: object) -> None:
            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json=response)
                ),
            ) as client:
                with self.assertRaisesRegex(
                    ImmichResponseError, "^Immich returned invalid albums$"
                ):
                    await client.albums()

        for response in cases:
            with self.subTest(response=response):
                trio.run(scenario, response)

    def test_asset_pages_emits_exact_relation_filters(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                json.loads(request.content),
                {
                    "page": 1,
                    "size": 1000,
                    "order": "asc",
                    "withExif": True,
                    "withDeleted": True,
                    "withStacked": True,
                    "albumIds": [ALBUM_ID],
                    "withPeople": True,
                },
            )
            item = asset()
            item["people"] = [{"id": OTHER_ID}, {"id": ASSET_ID}]
            return httpx.Response(
                200,
                json={"assets": {"items": [item], "count": 1, "nextPage": None}},
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                pages = [
                    page
                    async for page in client.asset_pages(
                        OWNER_ID,
                        album_id=ALBUM_ID,
                        with_people=True,
                    )
                ]
                self.assertEqual(pages[0][0].person_ids, (ASSET_ID, OTHER_ID))

        trio.run(scenario)

    def test_asset_pages_requires_people_when_requested(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"assets": {"items": [asset()], "count": 1, "nextPage": None}},
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                async with aclosing(client.asset_pages(OWNER_ID, with_people=True)) as pages:
                    with self.assertRaisesRegex(
                        ImmichResponseError, "invalid asset people"
                    ):
                        await anext(pages)

        trio.run(scenario)

    def test_classifies_only_no_response_network_failures_as_unavailable(self) -> None:
        async def scenario(error: Exception) -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                raise error

            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaisesRegex(
                    ImmichUnavailableError, "^Immich is unavailable$"
                ):
                    await client.validate()

        for error in (
            httpx.ConnectError("connection refused"),
            httpx.ConnectTimeout("connect timed out"),
            httpx.ReadError("connection lost"),
            httpx.ReadTimeout("read timed out"),
        ):
            with self.subTest(error=type(error).__name__):
                trio.run(scenario, error)

    def test_tls_and_protocol_failures_are_not_offline_availability(self) -> None:
        async def scenario(error: Exception, message: str) -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                raise error

            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaisesRegex(ImmichError, message) as raised:
                    await client.validate()
                self.assertNotIsInstance(raised.exception, ImmichUnavailableError)

        tls = httpx.ConnectError("certificate verify failed")
        bridge = trio.BrokenResourceError()
        bridge.__context__ = ssl.SSLCertVerificationError(
            "certificate verify failed"
        )
        tls.__cause__ = bridge
        trio.run(scenario, tls, "^Immich TLS validation failed$")
        trio.run(
            scenario,
            httpx.RemoteProtocolError("invalid HTTP"),
            "^Immich transport validation failed$",
        )

    def test_asset_pages_sends_an_inclusive_millisecond_update_bound(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.assertEqual(body["updatedAfter"], "2026-08-25T12:00:00.123Z")
            self.assertNotIn("albumIds", body)
            self.assertNotIn("withPeople", body)
            return httpx.Response(
                200,
                json={"assets": {"items": [], "count": 0, "nextPage": None}},
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                self.assertEqual(
                    [page async for page in client.asset_pages(
                        OWNER_ID,
                        updated_after_ms=1_787_659_200_123,
                    )],
                    [[]],
                )

        trio.run(scenario)

    def test_asset_pages_rejects_invalid_relation_options_before_network(self) -> None:
        requests = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            raise AssertionError("invalid relation options made a request")

        async def scenario(option: str, value: object) -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaises(ValueError):
                    pages = (
                        client.asset_pages(OWNER_ID, album_id=value)  # type: ignore[arg-type]
                        if option == "album_id"
                        else client.asset_pages(OWNER_ID, with_people=value)  # type: ignore[arg-type]
                    )
                    await anext(pages)

        for option, value in (
            ("album_id", True),
            ("album_id", "not-a-uuid"),
            ("album_id", ALBUM_ID.upper()),
            ("with_people", 1),
            ("with_people", "true"),
        ):
            with self.subTest(option=option, value=value):
                trio.run(scenario, option, value)
        self.assertEqual(requests, 0)

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

    def test_incremental_pages_allow_shifted_duplicate_asset_ids(self) -> None:
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
                pages = [
                    page
                    async for page in client.asset_pages(
                        OWNER_ID,
                        updated_after_ms=0,
                        allow_duplicate_ids=True,
                    )
                ]
                self.assertEqual([[item.id for item in page] for page in pages], [[ASSET_ID], [ASSET_ID]])

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

    def test_page_limit_stops_before_the_next_request(self) -> None:
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
                        "nextPage": str(requests + 1),
                    }
                },
            )

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                async with aclosing(client.asset_pages(OWNER_ID, page_limit=1)) as pages:
                    self.assertEqual((await anext(pages))[0].id, ASSET_ID)
                    with self.assertRaisesRegex(ImmichPageLimitError, "page limit"):
                        await anext(pages)

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
            "/api/api-keys/me": {"permissions": sorted(READ_PERMISSIONS)},
            "/api/users/me": {"id": OWNER_ID},
            "/api/server/media-types": {"image": [".jpg"], "video": [], "sidecar": []},
            "/api/server/features": {"trash": True},
        }
        cases = (
            ("/api/server/version", {"major": "3", "minor": 0, "patch": 3}, "version"),
            (
                "/api/api-keys/me",
                {"permissions": [*sorted(READ_PERMISSIONS), 7]},
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
                    json={"permissions": sorted(READ_PERMISSIONS)},
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

    def test_read_validation_requires_album_and_person_permissions(self) -> None:
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
                    ]
                },
            }
            return httpx.Response(200, json=responses[request.url.path])

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaisesRegex(
                    ImmichError,
                    "^API key is missing permissions: album.read, person.read$",
                ):
                    await client.validate()

        trio.run(scenario)

    def test_rejects_extra_permissions_for_read_only_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            responses = {
                "/.well-known/immich": {"api": {"endpoint": "/api"}},
                "/api/server/version": {"major": 3, "minor": 0, "patch": 3},
                "/api/api-keys/me": {
                    "permissions": [*sorted(READ_PERMISSIONS), "asset.delete"]
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

    def test_upload_posts_checksum_marker_and_accepts_created(self) -> None:
        requests: list[str] = []
        expected_times: dict[str, bytes] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            self.assertEqual((request.method, request.url.path), ("POST", "/api/assets"))
            self.assertEqual(
                request.headers["x-immich-checksum"],
                "BA8G/XdAkkeNRQd09bowxdp4rMg=",
            )
            body = request.read()
            self.assertIn(b'name="fileCreatedAt"', body)
            self.assertIn(b'name="fileModifiedAt"', body)
            self.assertIn(expected_times["created"], body)
            self.assertIn(expected_times["modified"], body)
            self.assertIn(b'name="filename"\r\n\r\nphoto.jpg', body)
            self.assertIn(
                b'[{"key":"immich-on-demand.upload","value":{"formatVersion":1,'
                b'"uploadId":"32345678-1234-4234-8234-123456789abc"}}]',
                body,
            )
            self.assertIn(b'name="assetData"; filename="photo.jpg"', body)
            self.assertIn(b"content", body)
            return httpx.Response(201, json={"status": "created", "id": ASSET_ID})

        async def scenario(descriptor: int) -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                result = await client.upload(
                    descriptor, "photo.jpg", frozenset({".jpg"}), UPLOAD_ID
                )
                self.assertEqual(result.asset_id, ASSET_ID)
                self.assertTrue(result.created)
                self.assertEqual(result.status, "created")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload"
            path.write_bytes(b"content")
            stats = path.stat()
            expected_times["created"] = datetime.fromtimestamp(
                stats.st_ctime, timezone.utc
            ).isoformat().encode()
            expected_times["modified"] = datetime.fromtimestamp(
                stats.st_mtime, timezone.utc
            ).isoformat().encode()
            with path.open("rb") as payload:
                trio.run(scenario, payload.fileno())
        self.assertEqual(requests, ["/api/assets"])

    def test_upload_uses_the_supplied_validated_descriptor(self) -> None:
        bodies: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(request.read())
            return httpx.Response(201, json={"status": "created", "id": ASSET_ID})

        async def scenario(descriptor: int) -> None:
            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                await client.upload(
                    descriptor, "photo.jpg", frozenset({".jpg"}), UPLOAD_ID
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "payload"
            path.write_bytes(b"validated bytes")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                path.unlink()
                path.symlink_to(root / "replacement")
                (root / "replacement").write_bytes(b"different bytes")
                trio.run(scenario, descriptor)
            finally:
                os.close(descriptor)

        self.assertIn(b"validated bytes", bodies[0])
        self.assertNotIn(b"different bytes", bodies[0])

    def test_upload_returns_duplicate_candidate_and_reads_its_marker(self) -> None:
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.method == "POST":
                return httpx.Response(
                    200, json={"status": "duplicate", "id": ASSET_ID}
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "unrelated",
                        "value": {"kept": True},
                        "updatedAt": "2026-08-26T10:00:00.000Z",
                    },
                    {
                        "key": "immich-on-demand.upload",
                        "value": {"formatVersion": 1, "uploadId": UPLOAD_ID},
                        "updatedAt": "2026-08-26T10:00:01.000Z",
                    },
                ],
            )

        async def scenario(descriptor: int) -> None:
            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                result = await client.upload(
                    descriptor, "photo.jpg", frozenset({".jpg"}), UPLOAD_ID
                )
                self.assertEqual(result.asset_id, ASSET_ID)
                self.assertFalse(result.created)
                self.assertEqual(result.status, "duplicate")
                self.assertEqual(await client.asset_metadata(ASSET_ID), UPLOAD_ID)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload"
            path.write_bytes(b"content")
            with path.open("rb") as payload:
                trio.run(scenario, payload.fileno())
        self.assertEqual(
            requests,
            [
                ("POST", "/api/assets"),
                ("GET", f"/api/assets/{ASSET_ID}/metadata"),
            ],
        )

    def test_upload_rejects_malformed_success_responses(self) -> None:
        cases: tuple[tuple[int, object | None], ...] = (
            (302, None),
            (202, {"status": "created", "id": ASSET_ID}),
            (201, {"status": "duplicate", "id": ASSET_ID}),
            (200, {"status": "created", "id": ASSET_ID}),
            (201, {"status": "unknown", "id": ASSET_ID}),
            (201, {"status": "created", "id": ASSET_ID, "extra": True}),
            (201, {"status": "created"}),
            (201, {"status": "created", "id": ASSET_ID.upper()}),
            (201, {"status": "created", "id": True}),
            (201, ["not", "an", "object"]),
            (201, None),
        )

        async def scenario(descriptor: int, status: int, value: object | None) -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, "/api/assets")
                if value is None:
                    return httpx.Response(status, content=b"api-secret malformed")
                return httpx.Response(status, json=value)

            async with ImmichClient(
                "https://photos.example.test",
                "api-secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaisesRegex(
                    ImmichError, "^Immich returned an invalid upload response$"
                ) as raised:
                    await client.upload(
                        descriptor, "photo.jpg", frozenset({".jpg"}), UPLOAD_ID
                    )
                self.assertNotIn("api-secret", str(raised.exception))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload"
            path.write_bytes(b"content")
            for status, value in cases:
                with self.subTest(status=status, value=value):
                    with path.open("rb") as payload:
                        trio.run(scenario, payload.fileno(), status, value)

    def test_asset_metadata_requires_one_exact_upload_marker(self) -> None:
        updated_at = "2026-08-26T10:00:00.000Z"
        valid_marker = {
            "key": "immich-on-demand.upload",
            "value": {"formatVersion": 1, "uploadId": UPLOAD_ID},
            "updatedAt": updated_at,
        }
        unrelated = {
            "key": "unrelated",
            "value": {"kept": True},
            "updatedAt": updated_at,
        }
        cases: tuple[object, ...] = (
            {},
            ["not-an-object"],
            [{"key": "unrelated", "value": {}, "updatedAt": updated_at, "extra": 1}],
            [{"key": 1, "value": {}, "updatedAt": updated_at}],
            [{"key": "unrelated", "value": [], "updatedAt": updated_at}],
            [{"key": "unrelated", "value": {}, "updatedAt": 1}],
            [{"key": "unrelated", "value": {}, "updatedAt": "not-a-date"}],
            [],
            [unrelated],
            [valid_marker, valid_marker],
            [
                {
                    **valid_marker,
                    "value": {"formatVersion": True, "uploadId": UPLOAD_ID},
                }
            ],
            [
                {
                    **valid_marker,
                    "value": {"formatVersion": 2, "uploadId": UPLOAD_ID},
                }
            ],
            [
                {
                    **valid_marker,
                    "value": {"formatVersion": 1, "uploadId": True},
                }
            ],
            [
                {
                    **valid_marker,
                    "value": {"formatVersion": 1, "uploadId": UPLOAD_ID.upper()},
                }
            ],
            [
                {
                    **valid_marker,
                    "value": {
                        "formatVersion": 1,
                        "uploadId": UPLOAD_ID,
                        "extra": True,
                    },
                }
            ],
        )

        async def scenario(value: object) -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(
                    request.url.path, f"/api/assets/{ASSET_ID}/metadata"
                )
                return httpx.Response(200, json=value)

            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaisesRegex(
                    ImmichError, "^Immich returned invalid upload metadata$"
                ):
                    await client.asset_metadata(ASSET_ID)

        for value in cases:
            with self.subTest(value=value):
                trio.run(scenario, value)

    def test_upload_candidate_verification_retries_transient_http_statuses(self) -> None:
        async def scenario(operation: str) -> None:
            def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(503, content=b"private response")

            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaisesRegex(
                    ImmichRetryableError,
                    "^Immich upload is temporarily unavailable$",
                ):
                    if operation == "asset":
                        await client.asset(ASSET_ID)
                    else:
                        await client.asset_metadata(ASSET_ID)

        for operation in ("asset", "metadata"):
            with self.subTest(operation=operation):
                trio.run(scenario, operation)

    def test_upload_candidate_rejects_malformed_asset_schema(self) -> None:
        async def scenario(value: object) -> None:
            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json=value)
                ),
            ) as client:
                with self.assertRaisesRegex(
                    ImmichResponseError,
                    "^Immich returned an invalid upload candidate$",
                ):
                    await client.asset(ASSET_ID)

        for value in ({"id": ASSET_ID}, asset(OTHER_ID)):
            with self.subTest(value=value):
                trio.run(scenario, value)

    def test_upload_checks_requested_name_extension_before_network(self) -> None:
        requests = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            raise AssertionError("upload made a request")

        async def scenario(descriptor: int) -> None:
            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaisesRegex(ImmichError, "does not accept the .raw"):
                    await client.upload(
                        descriptor, "photo.raw", frozenset({".jpg"}), UPLOAD_ID
                    )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload"
            path.write_bytes(b"content")
            with path.open("rb") as payload:
                trio.run(scenario, payload.fileno())
        self.assertEqual(requests, 0)

    def test_only_upload_retry_statuses_use_the_retryable_error(self) -> None:
        async def upload_scenario(descriptor: int, status: int) -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, "/api/assets")
                return httpx.Response(status, content=b"api-secret response")

            async with ImmichClient(
                "https://photos.example.test",
                "api-secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaisesRegex(
                    ImmichRetryableError,
                    "^Immich upload is temporarily unavailable$",
                ) as raised:
                    await client.upload(
                        descriptor, "photo.jpg", frozenset({".jpg"}), UPLOAD_ID
                    )
                self.assertNotIsInstance(raised.exception, ImmichUnavailableError)
                self.assertNotIn("api-secret", str(raised.exception))

        async def authoritative_upload_scenario(descriptor: int, status: int) -> None:
            def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(status)

            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaises(ImmichError) as raised:
                    await client.upload(
                        descriptor, "photo.jpg", frozenset({".jpg"}), UPLOAD_ID
                    )
                self.assertNotIsInstance(raised.exception, ImmichRetryableError)
                self.assertNotIsInstance(raised.exception, ImmichUnavailableError)
                self.assertIn(str(status), str(raised.exception))

        async def non_upload_scenario() -> None:
            def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(503)

            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaises(ImmichError) as raised:
                    await client.trash(ASSET_ID)
                self.assertNotIsInstance(raised.exception, ImmichRetryableError)
                self.assertNotIsInstance(raised.exception, ImmichUnavailableError)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload"
            path.write_bytes(b"content")
            with path.open("rb") as payload:
                for status in (408, 425, 429, 500, 503, 599):
                    with self.subTest(retryable=status):
                        trio.run(upload_scenario, payload.fileno(), status)
                for status in (400, 401, 403, 404, 409, 422, 499):
                    with self.subTest(authoritative=status):
                        trio.run(
                            authoritative_upload_scenario,
                            payload.fileno(),
                            status,
                        )
        trio.run(non_upload_scenario)

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

    def test_restore_refetches_feature_and_restores_exactly_one_asset(self) -> None:
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.url.path == "/api/server/features":
                return httpx.Response(200, json={"trash": True})
            self.assertEqual(
                (request.method, request.url.path),
                ("POST", "/api/trash/restore/assets"),
            )
            self.assertEqual(
                request.read(),
                b'{"ids":["12345678-1234-4234-8234-123456789abc"]}',
            )
            return httpx.Response(200, json={"count": 1})

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                await client.restore(ASSET_ID)
                await client.restore(ASSET_ID)

        trio.run(scenario)
        self.assertEqual(
            requests,
            [
                ("GET", "/api/server/features"),
                ("POST", "/api/trash/restore/assets"),
                ("GET", "/api/server/features"),
                ("POST", "/api/trash/restore/assets"),
            ],
        )

    def test_restore_requires_literal_true_from_fresh_feature_response(self) -> None:
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            return httpx.Response(200, json={"trash": 1})

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaisesRegex(
                    ImmichError, "^Immich trash is disabled; refusing restore$"
                ):
                    await client.restore(ASSET_ID)

        trio.run(scenario)
        self.assertEqual(requests, [("GET", "/api/server/features")])

    def test_restore_rejects_non_object_server_responses(self) -> None:
        payloads = iter(([], {"trash": True}, []))

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(payloads))

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test",
                "secret",
                transport=httpx.MockTransport(handler),
            ) as client:
                with self.assertRaisesRegex(ImmichError, "non-object response"):
                    await client.restore(ASSET_ID)
                with self.assertRaisesRegex(ImmichError, "non-object response"):
                    await client.restore(ASSET_ID)

        trio.run(scenario)

    def test_restore_requires_a_literal_integer_count(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/server/features":
                return httpx.Response(200, json={"trash": True})
            return httpx.Response(200, json={"count": True})

        async def scenario() -> None:
            async with ImmichClient(
                "https://photos.example.test", "secret", transport=httpx.MockTransport(handler)
            ) as client:
                with self.assertRaisesRegex(ImmichError, "did not restore"):
                    await client.restore(ASSET_ID)

        trio.run(scenario)
