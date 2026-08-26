from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
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
    CacheError,
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
        self.yielded = 0

    async def aiter_bytes(self):  # type annotation omitted to keep the fake small
        for chunk in self.chunks:
            self.yielded += 1
            if self.gate is not None:
                await self.gate.wait()
            yield chunk


class Client:
    def __init__(self, chunks: list[bytes], gate: trio.Event | None = None) -> None:
        self.chunks = chunks
        self.gate = gate
        self.calls = 0
        self.started = trio.Event()
        self.response: Response | None = None

    @asynccontextmanager
    async def original(self, asset_id: str):
        self.calls += 1
        self.started.set()
        self.response = Response(self.chunks, self.gate)
        yield self.response


class ContentCacheTest(unittest.TestCase):
    def test_disabled_downloads_reuse_a_complete_cache_after_restart(self) -> None:
        content = b"trusted original"

        async def scenario(root: Path) -> None:
            await ContentCache(root, Client([content])).hydrate(  # type: ignore[arg-type]
                asset(content)
            )
            client = Client([])
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                downloads_enabled=False,
            )

            self.assertEqual(await cache.read(asset(content), 0, len(content)), content)
            self.assertEqual(client.calls, 0)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_disabled_downloads_leave_missing_and_corrupt_cache_untouched(self) -> None:
        content = b"trusted original"

        async def scenario(root: Path, corrupt: bool) -> None:
            item = asset(content)
            if corrupt:
                original = root / item.id
                original.write_bytes(b"x" * len(content))
                os.utime(original, ns=(1, item.modified_ns))
            unrelated = root / OTHER_ID
            unrelated.write_bytes(b"keep")
            before = {OTHER_ID: b"keep"}
            if corrupt:
                before[item.id] = b"x" * len(content)
            for path in root.iterdir():
                os.utime(path, ns=(1, path.stat().st_mtime_ns))
            before_atimes = {
                path.name: path.stat().st_atime_ns for path in root.iterdir()
            }
            client = Client([content])
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                max_bytes=0,
                downloads_enabled=False,
            )

            with self.assertRaisesRegex(
                CacheError, "^original is unavailable while downloads are disabled$"
            ):
                await cache.hydrate(item)

            self.assertEqual(client.calls, 0)
            self.assertEqual(
                {path.name: path.stat().st_atime_ns for path in root.iterdir()},
                before_atimes,
            )
            self.assertEqual(
                {path.name: path.read_bytes() for path in root.iterdir()},
                before,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for corrupt in (False, True):
                with self.subTest(corrupt=corrupt):
                    case = root / str(corrupt)
                    case.mkdir()
                    trio.run(scenario, case, corrupt)

    def test_enabling_downloads_promotes_a_cache_miss_to_one_hydration(self) -> None:
        content = b"trusted original"

        async def scenario(root: Path) -> None:
            client = Client([content])
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                downloads_enabled=False,
            )
            with self.assertRaises(CacheError):
                await cache.hydrate(asset(content))

            cache.enable_downloads()

            self.assertEqual((await cache.hydrate(asset(content))).read_bytes(), content)
            self.assertEqual(client.calls, 1)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_describe_reports_local_state_without_hashing_or_touching(self) -> None:
        content = b"cached"
        item = asset(content)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / item.id
            original.write_bytes(content)
            os.utime(original, ns=(1, item.modified_ns))
            cache = ContentCache(
                root,
                Client([]),  # type: ignore[arg-type]
                pinned_ids={item.id},
            )
            cache.acquire(item.id)

            with (
                patch.object(
                    cache,
                    "_file_sha1",
                    side_effect=AssertionError("describe hashed cached bytes"),
                ),
                patch(
                    "immich_on_demand.content_cache.os.utime",
                    side_effect=AssertionError("describe touched cached bytes"),
                ),
            ):
                self.assertEqual(
                    cache.describe(item),
                    {"cached": True, "busy": True, "pinned": True},
                )

            cache.release(item.id)
            os.utime(original, ns=(1, item.modified_ns + 1))
            self.assertEqual(
                cache.describe(item),
                {"cached": False, "busy": False, "pinned": True},
            )
            original.write_bytes(content + b"!")
            os.utime(original, ns=(1, item.modified_ns))
            self.assertEqual(
                cache.describe(item),
                {"cached": False, "busy": False, "pinned": True},
            )

    def test_initialized_pin_blocks_manual_eviction_until_unpinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / ASSET_ID
            original.write_bytes(b"pinned")
            cache = ContentCache(
                root,
                Client([]),  # type: ignore[arg-type]
                pinned_ids={ASSET_ID},
            )

            with self.assertRaisesRegex(CacheError, "pinned"):
                cache.evict(ASSET_ID)
            self.assertTrue(original.exists())

            cache.unpin(ASSET_ID)
            self.assertTrue(original.exists())
            self.assertTrue(cache.evict(ASSET_ID))

    def test_pin_operation_excludes_an_original_from_policy_eviction(self) -> None:
        now = 1_000 * 1_000_000_000
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for asset_id in (ASSET_ID, OTHER_ID):
                path = root / asset_id
                path.write_bytes(b"old")
                os.utime(path, ns=(1, 1))
            cache = ContentCache(root, Client([]))  # type: ignore[arg-type]
            cache.pin(ASSET_ID)

            removed = cache.evict_to_limits(
                max_age_seconds=0,
                max_bytes=0,
                minimum_free_bytes=0,
                now_ns=now,
            )

            self.assertEqual(removed, [OTHER_ID])
            self.assertTrue((root / ASSET_ID).exists())

    def test_capacity_admission_never_reclaims_a_pinned_original(self) -> None:
        content = b"new!"

        async def scenario(root: Path) -> None:
            client = Client([content])
            pinned = root / ASSET_ID
            pinned.write_bytes(b"keep")
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                max_bytes=4,
                pinned_ids={ASSET_ID},
            )

            with self.assertRaisesRegex(CacheError, "cache capacity"):
                await cache.hydrate(asset(content, OTHER_ID))

            self.assertTrue(pinned.exists())
            self.assertEqual(client.calls, 0)

            cache.unpin(ASSET_ID)
            hydrated = await cache.hydrate(asset(content, OTHER_ID))
            self.assertEqual(hydrated.read_bytes(), content)
            self.assertFalse(pinned.exists())

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_pinned_hydration_bypasses_the_soft_size_target(self) -> None:
        content = b"large"

        async def scenario(root: Path) -> None:
            existing = root / ASSET_ID
            existing.write_bytes(b"keep")
            client = Client([content])
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                max_bytes=4,
                pinned_ids={ASSET_ID},
            )
            cache.pin(OTHER_ID)

            hydrated = await cache.hydrate(asset(content, OTHER_ID))

            self.assertEqual(hydrated.read_bytes(), content)
            self.assertTrue(existing.exists())
            self.assertEqual(client.calls, 1)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_pinned_hydration_still_respects_the_free_space_floor(self) -> None:
        content = b"pin!"

        async def scenario(root: Path) -> None:
            client = Client([content])
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                max_bytes=1,
                minimum_free_bytes=3,
            )
            cache.pin(ASSET_ID)

            with (
                patch(
                    "immich_on_demand.content_cache.shutil.disk_usage",
                    return_value=SimpleNamespace(free=6),
                ),
                self.assertRaisesRegex(CacheError, "cache capacity"),
            ):
                await cache.hydrate(asset(content))

            self.assertEqual(client.calls, 0)
            self.assertFalse((root / ASSET_ID).exists())

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_replacement_pin_hydrates_new_original_and_retires_old_pin(self) -> None:
        old_content = b"old original"
        replacement_content = b"replacement original"

        async def scenario(root: Path) -> None:
            old = asset(old_content)
            replacement = asset(replacement_content, OTHER_ID)
            old_path = root / old.id
            old_path.write_bytes(old_content)
            os.utime(old_path, ns=(1, old.modified_ns))
            client = Client([replacement_content])
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                pinned_ids={old.id},
            )

            await cache.transfer_pin(old.id, replacement, pinned=True)

            self.assertEqual(
                cache.describe(old),
                {"cached": True, "busy": False, "pinned": False},
            )
            self.assertEqual(
                cache.describe(replacement),
                {"cached": True, "busy": False, "pinned": True},
            )
            self.assertEqual((root / replacement.id).read_bytes(), replacement_content)
            self.assertEqual(client.calls, 1)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_failed_replacement_pin_keeps_new_pin_without_publishing_bad_bytes(
        self,
    ) -> None:
        replacement_content = b"replacement original"

        async def scenario(root: Path) -> None:
            old = asset(b"old original")
            replacement = asset(replacement_content, OTHER_ID)
            cache = ContentCache(
                root,
                Client([b"wrong replacement"]),  # type: ignore[arg-type]
                pinned_ids={old.id},
            )

            with self.assertRaises(CacheIntegrityError):
                await cache.transfer_pin(old.id, replacement, pinned=True)

            self.assertFalse(cache.describe(old)["pinned"])
            self.assertEqual(
                cache.describe(replacement),
                {"cached": False, "busy": False, "pinned": True},
            )
            self.assertFalse((root / replacement.id).exists())

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_rejects_a_symlink_cache_root_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target"
            target.mkdir(mode=0o755)
            victim = target / ASSET_ID
            victim.write_bytes(b"not cache data")
            root = base / "originals"
            root.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(PermissionError, "cache root"):
                ContentCache(root, Client([]))  # type: ignore[arg-type]

            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual(victim.read_bytes(), b"not cache data")

    def test_rejects_a_cache_root_not_owned_by_the_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "immich_on_demand.content_cache.os.getuid", return_value=os.getuid() + 1
            ):
                with self.assertRaisesRegex(PermissionError, "owned by this user"):
                    ContentCache(root, Client([]))  # type: ignore[arg-type]

    def test_rejects_unsafe_complete_candidates_without_touching_them(self) -> None:
        content = b"cached original"

        async def scenario(base: Path) -> None:
            target = base / "target"
            target.write_bytes(content)
            os.utime(target, ns=(1_000_000_000, 2_000_000_000))
            target_state = target.stat()
            symlink_root = base / "symlink-cache"
            symlink_cache = ContentCache(symlink_root, Client([content]))  # type: ignore[arg-type]
            (symlink_root / ASSET_ID).symlink_to(target)

            with self.assertRaisesRegex(PermissionError, "cached original"):
                await symlink_cache.read(asset(content), 0, len(content))

            self.assertTrue((symlink_root / ASSET_ID).is_symlink())
            self.assertEqual(target.stat().st_atime_ns, target_state.st_atime_ns)
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual(symlink_cache.client.calls, 0)

            directory_root = base / "directory-cache"
            directory_cache = ContentCache(directory_root, Client([content]))  # type: ignore[arg-type]
            (directory_root / ASSET_ID).mkdir()
            with self.assertRaisesRegex(PermissionError, "cached original"):
                await directory_cache.hydrate(asset(content))
            self.assertTrue((directory_root / ASSET_ID).is_dir())
            self.assertEqual(directory_cache.client.calls, 0)

            owner_root = base / "owner-cache"
            owner_cache = ContentCache(owner_root, Client([content]))  # type: ignore[arg-type]
            candidate = owner_root / ASSET_ID
            candidate.write_bytes(content)
            with patch(
                "immich_on_demand.content_cache.os.getuid", return_value=os.getuid() + 1
            ):
                with self.assertRaisesRegex(PermissionError, "cached original"):
                    await owner_cache.hydrate(asset(content))
                removed = owner_cache.evict_to_limits(
                    max_age_seconds=0,
                    max_bytes=0,
                    minimum_free_bytes=0,
                )
            self.assertEqual(removed, [])
            self.assertEqual(candidate.read_bytes(), content)
            self.assertEqual(owner_cache.client.calls, 0)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_initialization_removes_only_safe_stale_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / f".{ASSET_ID}.stale"
            stale.write_bytes(b"partial")
            unrelated = root / ".unrelated"
            unrelated.write_bytes(b"keep")
            target = root / "target"
            target.write_bytes(b"keep")
            unsafe = root / f".{OTHER_ID}.symlink"
            unsafe.symlink_to(target)

            ContentCache(root, Client([]))  # type: ignore[arg-type]

            self.assertFalse(stale.exists())
            self.assertEqual(unrelated.read_bytes(), b"keep")
            self.assertTrue(unsafe.is_symlink())
            self.assertEqual(target.read_bytes(), b"keep")

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

    def test_managed_asset_rehydrates_same_size_cache_corruption(self) -> None:
        content = b"trusted original"

        async def scenario(root: Path) -> None:
            client = Client([content])
            cache = ContentCache(root, client)  # type: ignore[arg-type]
            item = asset(content)

            path = await cache.hydrate(item)
            path.write_bytes(b"x" * len(content))

            self.assertEqual((await cache.hydrate(item)).read_bytes(), content)
            self.assertEqual(client.calls, 2)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_metadata_change_cannot_replace_bytes_held_by_an_open_handle(self) -> None:
        original = b"old bytes"
        changed = b"new bytes"

        async def scenario(root: Path) -> None:
            client = Client([original])
            cache = ContentCache(root, client)  # type: ignore[arg-type]
            old_asset = asset(original)
            path = await cache.hydrate(old_asset)
            cache.acquire(old_asset.id)
            client.chunks = [changed]
            new_asset = asset(changed)

            with self.assertRaises(CacheBusyError):
                await cache.hydrate(new_asset)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(await cache.read(old_asset, 0, len(original)), original)
            self.assertEqual(client.calls, 1)

            cache.release(old_asset.id)
            self.assertFalse(path.exists())
            self.assertEqual(
                await cache.read(new_asset, 0, len(changed)),
                changed,
            )
            self.assertEqual(client.calls, 2)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_old_handle_reads_during_new_metadata_validation(self) -> None:
        original = b"old bytes"
        changed = b"new bytes"

        async def scenario(root: Path) -> None:
            client = Client([original])
            cache = ContentCache(root, client)  # type: ignore[arg-type]
            old_asset = asset(original)
            await cache.hydrate(old_asset)
            cache.acquire(old_asset.id)
            started = trio.Event()
            proceed = trio.Event()
            results: list[bytes] = []
            errors: dict[str, BaseException] = {}

            async def blocked_hash(function, *args, **kwargs):
                started.set()
                await proceed.wait()
                return function(*args, **kwargs)

            async def hydrate_new() -> None:
                try:
                    await cache.hydrate(asset(changed))
                except BaseException as error:
                    errors["new"] = error

            async def read_old() -> None:
                try:
                    results.append(await cache.read(old_asset, 0, len(original)))
                except BaseException as error:
                    errors["old"] = error

            with patch(
                "immich_on_demand.content_cache.trio.to_thread.run_sync", blocked_hash
            ):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(hydrate_new)
                    await started.wait()
                    nursery.start_soon(read_old)
                    await trio.lowlevel.checkpoint()
                    proceed.set()

            self.assertIsInstance(errors.get("new"), CacheBusyError)
            self.assertNotIn("old", errors)
            self.assertEqual(results, [original])
            cache.release(old_asset.id)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_unchanged_managed_asset_is_not_rehashed_on_each_open(self) -> None:
        content = b"trusted original"

        async def scenario(root: Path) -> None:
            cache = ContentCache(root, Client([content]))  # type: ignore[arg-type]
            item = asset(content)
            cache.acquire(item.id)
            first = await cache.hydrate(item)
            cache.release(item.id)

            with patch.object(
                cache,
                "_file_sha1",
                side_effect=AssertionError("unchanged cache was rehashed"),
            ):
                cache.acquire(item.id)
                second = await cache.hydrate(item)
                cache.release(item.id)

            self.assertEqual(second, first)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_concurrent_cached_validation_is_shared_after_restart(self) -> None:
        content = b"trusted original"

        async def scenario(root: Path) -> None:
            await ContentCache(root, Client([content])).hydrate(  # type: ignore[arg-type]
                asset(content)
            )
            client = Client([content])
            cache = ContentCache(root, client)  # type: ignore[arg-type]
            hash_started = trio.Event()
            second_started = trio.Event()
            first_touched = trio.Event()
            hash_calls = 0
            original_touch = cache._touch

            async def ordered_hash(function, *args, **kwargs):
                nonlocal hash_calls
                hash_calls += 1
                if hash_calls == 1:
                    hash_started.set()
                    await second_started.wait()
                else:
                    await first_touched.wait()
                return function(*args, **kwargs)

            def noticed_touch(path: Path, item: Asset) -> None:
                original_touch(path, item)
                first_touched.set()

            results: list[Path] = []

            async def first() -> None:
                results.append(await cache.hydrate(asset(content)))

            async def second() -> None:
                second_started.set()
                results.append(await cache.hydrate(asset(content)))

            with (
                patch("immich_on_demand.content_cache.trio.to_thread.run_sync", ordered_hash),
                patch.object(cache, "_touch", noticed_touch),
            ):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(first)
                    await hash_started.wait()
                    nursery.start_soon(second)

            self.assertEqual(hash_calls, 1)
            self.assertEqual(client.calls, 0)
            self.assertEqual([path.read_bytes() for path in results], [content, content])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

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

    def test_parallel_hydrations_reserve_expected_bytes_before_streaming(self) -> None:
        first_content = b"one!"
        second_content = b"two!"

        class ParallelClient:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.first_started = trio.Event()
                self.release_first = trio.Event()

            @asynccontextmanager
            async def original(self, asset_id: str):
                self.calls.append(asset_id)
                if asset_id == ASSET_ID:
                    self.first_started.set()
                    yield Response([first_content], self.release_first)
                else:
                    yield Response([second_content])

        async def scenario(root: Path) -> None:
            client = ParallelClient()
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                max_bytes=6,
                minimum_free_bytes=0,
            )
            idle = root / THIRD_ID
            idle.write_bytes(b"ok")

            async with trio.open_nursery() as nursery:
                nursery.start_soon(cache.hydrate, asset(first_content))
                await client.first_started.wait()
                with self.assertRaisesRegex(CacheError, "cache capacity"):
                    await cache.hydrate(asset(second_content, OTHER_ID))
                self.assertEqual(client.calls, [ASSET_ID])
                self.assertTrue(idle.exists())
                client.release_first.set()

            self.assertEqual((root / ASSET_ID).read_bytes(), first_content)
            self.assertFalse((root / OTHER_ID).exists())

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_free_space_reservations_count_only_bytes_left_to_write(self) -> None:
        first_content = b"aabb"
        second_content = b"ccdd"

        class ProgressResponse(Response):
            async def aiter_bytes(self):
                yield self.chunks[0]
                progress.set()
                await finish.wait()
                yield self.chunks[1]

        class ParallelClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            @asynccontextmanager
            async def original(self, asset_id: str):
                self.calls.append(asset_id)
                if asset_id == ASSET_ID:
                    yield ProgressResponse([b"aa", b"bb"])
                else:
                    yield Response([second_content])

        async def scenario(root: Path) -> None:
            client = ParallelClient()
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                max_bytes=8,
                minimum_free_bytes=3,
            )

            with patch(
                "immich_on_demand.content_cache.shutil.disk_usage",
                return_value=SimpleNamespace(free=10),
            ):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(cache.hydrate, asset(first_content))
                    await progress.wait()
                    await cache.hydrate(asset(second_content, OTHER_ID))
                    finish.set()

            self.assertEqual(client.calls, [ASSET_ID, OTHER_ID])
            self.assertEqual((root / ASSET_ID).read_bytes(), first_content)
            self.assertEqual((root / OTHER_ID).read_bytes(), second_content)

        progress = trio.Event()
        finish = trio.Event()
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

    def test_stops_downloading_before_a_chunk_exceeds_expected_size(self) -> None:
        async def scenario(root: Path) -> None:
            client = Client([b"too large", b"must not be consumed"])
            cache = ContentCache(root, client)  # type: ignore[arg-type]
            item = replace(asset(b"x"), size=1)

            with self.assertRaisesRegex(CacheIntegrityError, "exceeds its expected"):
                await cache.hydrate(item)

            assert client.response is not None
            self.assertEqual(client.response.yielded, 1)
            self.assertEqual(list(root.iterdir()), [])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_aborts_before_a_chunk_crosses_the_free_space_floor(self) -> None:
        content = b"aabb"

        async def scenario(root: Path) -> None:
            client = Client([b"aa", b"bb"])
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                max_bytes=10,
                minimum_free_bytes=3,
            )

            with patch(
                "immich_on_demand.content_cache.shutil.disk_usage",
                side_effect=(
                    SimpleNamespace(free=10),
                    SimpleNamespace(free=5),
                    SimpleNamespace(free=4),
                ),
            ):
                with self.assertRaisesRegex(CacheError, "free-space floor"):
                    await cache.hydrate(asset(content))

            self.assertEqual(client.calls, 1)
            self.assertEqual(list(root.iterdir()), [])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_rejects_an_original_that_cannot_fit_before_streaming(self) -> None:
        content = b"too large"

        async def scenario(root: Path) -> None:
            client = Client([content])
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                max_bytes=len(content) - 1,
                minimum_free_bytes=0,
            )

            with self.assertRaisesRegex(CacheError, "cache capacity"):
                await cache.hydrate(asset(content))

            self.assertEqual(client.calls, 0)
            self.assertEqual(list(root.iterdir()), [])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_hydration_admission_evicts_complete_lru_to_fit_both_limits(self) -> None:
        content = b"new!"

        async def scenario(root: Path) -> None:
            client = Client([content])
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                max_bytes=6,
                minimum_free_bytes=3,
            )
            old = root / OTHER_ID
            old.write_bytes(b"old!")
            os.utime(old, ns=(1, 1))

            with patch(
                "immich_on_demand.content_cache.shutil.disk_usage",
                side_effect=(SimpleNamespace(free=5), SimpleNamespace(free=9)),
            ):
                hydrated = await cache.hydrate(asset(content))

            self.assertEqual(hydrated.read_bytes(), content)
            self.assertFalse(old.exists())
            self.assertEqual(client.calls, 1)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_failed_hydration_releases_its_capacity_reservation(self) -> None:
        content = b"good"

        async def scenario(root: Path) -> None:
            client = Client([b"bad!"])
            cache = ContentCache(
                root,
                client,  # type: ignore[arg-type]
                max_bytes=len(content),
                minimum_free_bytes=0,
            )

            with self.assertRaises(CacheIntegrityError):
                await cache.hydrate(asset(content))

            client.chunks = [content]
            hydrated = await cache.hydrate(asset(content, OTHER_ID))

            self.assertEqual(hydrated.read_bytes(), content)
            self.assertEqual(client.calls, 2)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_read_blocks_eviction_until_the_file_is_opened(self) -> None:
        content = b"cached"

        async def scenario(root: Path) -> None:
            item = asset(content)
            cache = ContentCache(root, Client([]))  # type: ignore[arg-type]
            (root / item.id).write_bytes(content)
            started = trio.Event()
            proceed = trio.Event()
            original_open = trio.open_file

            async def delayed_open(*args: object, **kwargs: object):
                started.set()
                await proceed.wait()
                return await original_open(*args, **kwargs)

            result: list[bytes] = []
            with patch("immich_on_demand.content_cache.trio.open_file", delayed_open):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(self._read_into, cache, item, result)
                    await started.wait()
                    with self.assertRaises(CacheBusyError):
                        cache.evict(item.id)
                    proceed.set()

            self.assertEqual(result, [content])
            self.assertTrue(cache.evict(item.id))

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

    def test_external_asset_rehydrates_after_same_size_original_change(self) -> None:
        original = b"external"
        changed = b"changed!"

        async def scenario(root: Path) -> None:
            client = Client([original])
            cache = ContentCache(root, client)  # type: ignore[arg-type]
            first = asset(original, library_id="library")

            path = await cache.hydrate(first)
            old_atime = 1_000_000_000
            os.utime(path, ns=(old_atime, path.stat().st_mtime_ns))
            client.chunks = [changed]
            updated = replace(
                first,
                updated_at="2026-08-25T12:01:00Z",
            )

            refreshed = await cache.hydrate(updated)

            self.assertEqual(refreshed.read_bytes(), changed)
            expected_token = int(
                datetime.fromisoformat(updated.updated_at.replace("Z", "+00:00")).timestamp()
                * 1_000_000_000
            )
            self.assertEqual(refreshed.stat().st_mtime_ns, expected_token)
            self.assertGreater(refreshed.stat().st_atime_ns, old_atime)
            self.assertEqual(client.calls, 2)

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

    @staticmethod
    async def _read_into(
        cache: ContentCache, item: Asset, result: list[bytes]
    ) -> None:
        result.append(await cache.read(item, 0, item.size or 0))


if __name__ == "__main__":
    unittest.main()
