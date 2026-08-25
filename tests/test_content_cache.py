from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
import base64
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import trio

from immich_on_demand.content_cache import (
    CacheBusyError,
    CacheIntegrityError,
    ContentCache,
)
from immich_on_demand.model import Asset


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
OTHER_ID = "22345678-1234-4234-8234-123456789abc"
THIRD_ID = "32345678-1234-4234-8234-123456789abc"
OWNER_ID = "87654321-4321-4321-8321-cba987654321"


def asset(content: bytes, asset_id: str = ASSET_ID, *, library_id: str | None = None) -> Asset:
    return Asset(
        id=asset_id,
        owner_id=OWNER_ID,
        original_name="photo.jpg",
        mime_type="image/jpeg",
        size=len(content),
        created_ns=1,
        modified_ns=2,
        updated_at="2026-08-25T12:00:00Z",
        checksum=base64.b64encode(hashlib.sha1(content).digest()).decode(),
        visibility="timeline",
        is_trashed=False,
        is_offline=False,
        library_id=library_id,
    )


class Response:
    def __init__(self, chunks: list[bytes], gate: trio.Event | None = None) -> None:
        self.chunks = chunks
        self.gate = gate

    async def aiter_bytes(self):  # type annotation omitted to keep the fake small
        for chunk in self.chunks:
            if self.gate is not None:
                await self.gate.wait()
            yield chunk


class Client:
    def __init__(self, chunks: list[bytes], gate: trio.Event | None = None) -> None:
        self.chunks = chunks
        self.gate = gate
        self.calls = 0
        self.started = trio.Event()

    @asynccontextmanager
    async def original(self, asset_id: str):
        self.calls += 1
        self.started.set()
        yield Response(self.chunks, self.gate)


class ContentCacheTest(unittest.TestCase):
    def test_hydrates_once_and_reads_cached_bytes(self) -> None:
        content = b"complete original"

        async def scenario(root: Path) -> None:
            client = Client([content[:8], content[8:]])
            cache = ContentCache(root, client)  # type: ignore[arg-type]
            item = asset(content)

            first = await cache.hydrate(item)
            old_atime = 1_000_000_000
            os.utime(first, ns=(old_atime, first.stat().st_mtime_ns))
            self.assertEqual(await cache.read(item, 9, 8), b"original")
            self.assertEqual(client.calls, 1)
            self.assertEqual(first.read_bytes(), content)
            self.assertGreater(first.stat().st_atime_ns, old_atime)
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory) / "originals")

    def test_concurrent_readers_share_one_hydration(self) -> None:
        content = b"one network response"

        async def scenario(root: Path) -> None:
            gate = trio.Event()
            client = Client([content], gate)
            cache = ContentCache(root, client)  # type: ignore[arg-type]
            item = asset(content)
            results: list[bytes] = []

            async def read() -> None:
                results.append(await cache.read(item, 0, len(content)))

            async with trio.open_nursery() as nursery:
                nursery.start_soon(read)
                await client.started.wait()
                nursery.start_soon(read)
                await trio.lowlevel.checkpoint()
                gate.set()

            self.assertEqual(results, [content, content])
            self.assertEqual(client.calls, 1)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_failed_integrity_never_publishes_a_file(self) -> None:
        content = b"bad"

        async def scenario(root: Path) -> None:
            for item in (
                replace(asset(content), size=len(content) + 1),
                replace(asset(content), checksum=base64.b64encode(b"wrong").decode()),
            ):
                cache = ContentCache(root, Client([content]))  # type: ignore[arg-type]
                with self.assertRaises(CacheIntegrityError):
                    await cache.hydrate(item)
                self.assertEqual(list(root.iterdir()), [])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_external_asset_uses_size_without_treating_checksum_as_content_hash(self) -> None:
        content = b"external"

        async def scenario(root: Path) -> None:
            item = replace(asset(content, library_id="library"), checksum="not-content-sha1")
            cache = ContentCache(root, Client([content]))  # type: ignore[arg-type]
            self.assertEqual((await cache.hydrate(item)).read_bytes(), content)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_manual_eviction_refuses_open_and_inflight_assets(self) -> None:
        content = b"busy"

        async def scenario(root: Path) -> None:
            gate = trio.Event()
            client = Client([content], gate)
            cache = ContentCache(root, client)  # type: ignore[arg-type]
            item = asset(content)

            async with trio.open_nursery() as nursery:
                nursery.start_soon(cache.hydrate, item)
                await client.started.wait()
                with self.assertRaises(CacheBusyError):
                    cache.evict(item.id)
                gate.set()

            cache.acquire(item.id)
            with self.assertRaises(CacheBusyError):
                cache.evict(item.id)
            cache.release(item.id)
            self.assertTrue(cache.evict(item.id))
            self.assertFalse(cache.evict(item.id))

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_policy_evicts_complete_files_by_age_size_and_free_space(self) -> None:
        now = 1_000 * 1_000_000_000

        def put(root: Path, asset_id: str, content: bytes, age_seconds: int) -> None:
            path = root / asset_id
            path.write_bytes(content)
            atime = now - age_seconds * 1_000_000_000
            os.utime(path, ns=(atime, atime))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = ContentCache(root, Client([]))  # type: ignore[arg-type]
            put(root, ASSET_ID, b"aa", 200)
            put(root, OTHER_ID, b"bb", 50)
            put(root, THIRD_ID, b"cc", 10)
            (root / ".incomplete").write_bytes(b"never count this")
            cache.acquire(OTHER_ID)

            with patch(
                "immich_on_demand.content_cache.shutil.disk_usage",
                return_value=SimpleNamespace(free=0),
            ):
                removed = cache.evict_to_limits(
                    max_age_seconds=100,
                    max_bytes=3,
                    minimum_free_bytes=3,
                    now_ns=now,
                )

            self.assertEqual(removed, [ASSET_ID, THIRD_ID])
            self.assertTrue((root / OTHER_ID).exists())
            self.assertTrue((root / ".incomplete").exists())


if __name__ == "__main__":
    unittest.main()
