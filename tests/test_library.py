from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

import trio
from trio.testing import wait_all_tasks_blocked

from immich_on_demand.app import refresh_catalog
from immich_on_demand.catalog import Catalog
from immich_on_demand.immich import ServerSession
from immich_on_demand.library import Library, LibraryError
from immich_on_demand.model import Asset
from immich_on_demand.settings import Settings


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
OTHER_ID = "22345678-1234-4234-8234-123456789abc"
THIRD_ID = "32345678-1234-4234-8234-123456789abc"
FOURTH_ID = "42345678-1234-4234-8234-123456789abc"
OWNER_ID = "87654321-4321-4321-8321-cba987654321"


def asset(asset_id: str = ASSET_ID, owner_id: str = OWNER_ID) -> Asset:
    return Asset(
        id=asset_id,
        owner_id=owner_id,
        original_name="photo.jpg",
        mime_type="image/jpeg",
        size=5,
        created_ns=1,
        modified_ns=2,
        updated_at="2026-08-25T12:00:00Z",
        checksum="abc=",
        visibility="timeline",
        is_trashed=False,
        is_offline=False,
        library_id=None,
    )


def session(*, owner_id: str = OWNER_ID, trash_enabled: bool = True) -> ServerSession:
    return ServerSession(owner_id, "3.0.3", frozenset({".jpg"}), trash_enabled)


def settings(root: Path, *, remote_delete: bool = False) -> Settings:
    return Settings("https://photos.example.test", root / "mount", remote_delete=remote_delete)


class MutationClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.trashes: list[str] = []
        self.restores: list[str] = []
        self.on_restore = lambda: None

    async def trash(self, asset_id: str) -> None:
        if self.error is not None:
            raise self.error
        self.trashes.append(asset_id)

    async def restore(self, asset_id: str) -> None:
        self.on_restore()
        self.restores.append(asset_id)
        if self.error is not None:
            raise self.error


class Cache:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.released: list[str] = []

    def acquire(self, asset_id: str) -> None:
        self.acquired.append(asset_id)

    def release(self, asset_id: str) -> None:
        self.released.append(asset_id)

    async def read(self, item: Asset, offset: int, size: int) -> bytes:
        return b"hello"[offset : offset + size]


def library(
    catalog: Catalog,
    root: Path,
    *,
    mutation: MutationClient | None = None,
    mutation_session: ServerSession | None = None,
    remote_delete: bool = False,
    catalog_lock: trio.Lock | None = None,
) -> Library:
    return Library(
        catalog,
        Cache(),  # type: ignore[arg-type]
        settings(root, remote_delete=remote_delete),
        mutation_client=mutation,  # type: ignore[arg-type]
        mutation_session=mutation_session,
        catalog_lock=catalog_lock if catalog_lock is not None else trio.Lock(),
    )


class LibraryTest(unittest.TestCase):
    def test_enabling_mutations_promotes_every_remote_route(self) -> None:
        async def scenario(root: Path) -> None:
            mutation = MutationClient()
            with Catalog(root / "catalog.db") as catalog:
                entry = catalog.add_uploaded(asset(), "photo.jpg")
                mounted = library(
                    catalog,
                    root,
                    remote_delete=True,
                )
                self.assertFalse(mounted.mutation_enabled)
                self.assertFalse(mounted.replacement_enabled)

                mounted.enable_mutations(mutation, session())  # type: ignore[arg-type]

                self.assertTrue(mounted.mutation_enabled)
                self.assertTrue(mounted.replacement_enabled)
                self.assertEqual(mounted.upload_access(), (mutation, session()))
                await mounted.remote_trash(entry)
                await mounted.remote_restore(entry.asset.id)
                self.assertEqual(mutation.trashes, [ASSET_ID])
                self.assertEqual(mutation.restores, [ASSET_ID])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_lists_and_reads_immutable_assets(self) -> None:
        async def scenario(root: Path) -> None:
            with Catalog(root / "catalog.db") as catalog:
                entry = catalog.add_uploaded(asset(), "photo.jpg")
                mounted = library(catalog, root)

                self.assertFalse(mounted.mutation_enabled)
                self.assertFalse(mounted.replacement_enabled)
                self.assertEqual(mounted.list(), [entry])
                self.assertEqual(await mounted.read(entry, 1, 3), b"ell")
                mounted.acquire(entry)
                mounted.release(entry)
                self.assertEqual(mounted._content_cache.acquired, [ASSET_ID])
                self.assertEqual(mounted._content_cache.released, [ASSET_ID])
                self.assertFalse(hasattr(mounted, "overwrite"))
                self.assertFalse(hasattr(mounted, "rename"))

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_list_never_exposes_non_visible_catalog_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Catalog(root / "catalog.db") as catalog:
                catalog.add_uploaded(replace(asset(), is_trashed=True), "trashed.jpg")
                catalog.add_uploaded(replace(asset(OTHER_ID), is_offline=True), "offline.jpg")
                catalog.add_uploaded(replace(asset(THIRD_ID), visibility="hidden"), "hidden.jpg")
                catalog.add_uploaded(replace(asset(FOURTH_ID), size=None), "unknown-size.jpg")
                mounted = library(catalog, root)

                self.assertEqual(mounted.list(), [])

    def test_remote_trash_requires_every_guard_and_commits_after_success(self) -> None:
        async def scenario(root: Path) -> None:
            guards = (
                (False, MutationClient(), session()),
                (True, None, session()),
                (True, MutationClient(), None),
                (True, MutationClient(), session(owner_id=OTHER_ID)),
                (True, MutationClient(), session(trash_enabled=False)),
            )
            for index, (enabled, mutation, mutation_session) in enumerate(guards):
                with Catalog(root / f"guard-{index}.db") as catalog:
                    entry = catalog.add_uploaded(asset(), "photo.jpg")
                    with self.assertRaises((LibraryError, PermissionError)):
                        await library(
                            catalog,
                            root,
                            mutation=mutation,
                            mutation_session=mutation_session,
                            remote_delete=enabled,
                        ).remote_trash(entry)
                    self.assertEqual(mutation.trashes if mutation else [], [])
                    self.assertEqual(catalog.list_visible(), [entry])

            with Catalog(root / "success.db") as catalog:
                entry = catalog.add_uploaded(asset(), "photo.jpg")
                mutation = MutationClient()
                mounted = library(
                    catalog,
                    root,
                    mutation=mutation,
                    mutation_session=session(),
                    remote_delete=True,
                )
                await mounted.remote_trash(entry)
                self.assertEqual(mutation.trashes, [ASSET_ID])
                self.assertEqual(catalog.list_visible(), [])

            with Catalog(root / "failure.db") as catalog:
                entry = catalog.add_uploaded(asset(), "photo.jpg")
                mutation = MutationClient(OSError("trash failed"))
                with self.assertRaises(OSError):
                    await library(
                        catalog,
                        root,
                        mutation=mutation,
                        mutation_session=session(),
                        remote_delete=True,
                    ).remote_trash(entry)
                self.assertEqual(catalog.list_visible(), [entry])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_remote_restore_rejects_every_failed_guard_without_network(self) -> None:
        async def scenario(root: Path) -> None:
            cases = (
                (False, MutationClient(), session(), True, ASSET_ID),
                (True, None, session(), True, ASSET_ID),
                (True, MutationClient(), None, True, ASSET_ID),
                (True, MutationClient(), session(trash_enabled=False), True, ASSET_ID),
                (True, MutationClient(), session(), True, OTHER_ID),
                (True, MutationClient(), session(), False, ASSET_ID),
                (True, MutationClient(), session(owner_id=OTHER_ID), True, ASSET_ID),
            )
            for index, (
                enabled,
                mutation,
                mutation_session,
                trashed,
                requested_id,
            ) in enumerate(cases):
                with self.subTest(index=index), Catalog(
                    root / f"restore-guard-{index}.db"
                ) as catalog:
                    entry = catalog.add_uploaded(
                        replace(asset(), is_trashed=trashed), "photo.jpg"
                    )
                    with self.assertRaises((LibraryError, PermissionError)):
                        await library(
                            catalog,
                            root,
                            mutation=mutation,
                            mutation_session=mutation_session,
                            remote_delete=enabled,
                        ).remote_restore(requested_id)

                    self.assertEqual(mutation.restores if mutation else [], [])
                    current = catalog.by_id(entry.asset.id)
                    self.assertEqual(
                        current and current.asset.is_trashed, entry.asset.is_trashed
                    )

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_remote_restore_commits_locally_only_after_remote_success(self) -> None:
        async def scenario(root: Path) -> None:
            with Catalog(root / "restore-success.db") as catalog:
                entry = catalog.add_uploaded(
                    replace(asset(), is_trashed=True), "stable-photo.jpg"
                )
                mutation = MutationClient()

                def observe_remote_call() -> None:
                    current = catalog.by_id(entry.asset.id)
                    self.assertTrue(current and current.asset.is_trashed)

                mutation.on_restore = observe_remote_call
                returned = await library(
                    catalog,
                    root,
                    mutation=mutation,
                    mutation_session=session(),
                    remote_delete=True,
                ).remote_restore(entry.asset.id)

                restored = catalog.by_id(entry.asset.id)
                assert restored is not None
                self.assertEqual(mutation.restores, [entry.asset.id])
                self.assertFalse(restored.asset.is_trashed)
                self.assertEqual(
                    (restored.inode, restored.name), (entry.inode, entry.name)
                )
                self.assertEqual(returned, restored)
                self.assertEqual(catalog.list_visible(), [returned])

            with Catalog(root / "restore-failure.db") as catalog:
                entry = catalog.add_uploaded(
                    replace(asset(), is_trashed=True), "photo.jpg"
                )
                mutation = MutationClient(OSError("restore failed"))
                with self.assertRaisesRegex(OSError, "restore failed"):
                    await library(
                        catalog,
                        root,
                        mutation=mutation,
                        mutation_session=session(),
                        remote_delete=True,
                    ).remote_restore(entry.asset.id)

                current = catalog.by_id(entry.asset.id)
                self.assertTrue(current and current.asset.is_trashed)
                self.assertEqual(mutation.restores, [entry.asset.id])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_refresh_cannot_resurrect_a_concurrent_trash(self) -> None:
        async def scenario(root: Path) -> None:
            refresh_paused = trio.Event()
            finish_refresh = trio.Event()
            trash_done = trio.Event()

            class RefreshClient:
                async def asset_pages(self, owner_id: str):
                    if owner_id != OWNER_ID:
                        raise AssertionError(owner_id)
                    yield [asset()]
                    refresh_paused.set()
                    await finish_refresh.wait()

            with Catalog(root / "catalog.db") as catalog:
                entry = catalog.add_uploaded(asset(), "photo.jpg")
                lock = trio.Lock()
                mutation = MutationClient()
                mounted = library(
                    catalog,
                    root,
                    mutation=mutation,
                    mutation_session=session(),
                    remote_delete=True,
                    catalog_lock=lock,
                )

                async def trash() -> None:
                    await mounted.remote_trash(entry)
                    trash_done.set()

                async with trio.open_nursery() as nursery:
                    nursery.start_soon(refresh_catalog, catalog, RefreshClient(), session(), lock)
                    await refresh_paused.wait()
                    nursery.start_soon(trash)
                    await wait_all_tasks_blocked()
                    self.assertEqual(mutation.trashes, [])
                    finish_refresh.set()
                    await trash_done.wait()

                self.assertEqual(catalog.list_visible(), [])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_refresh_serializes_with_remote_restore(self) -> None:
        async def scenario(root: Path) -> None:
            refresh_paused = trio.Event()
            finish_refresh = trio.Event()
            restore_done = trio.Event()

            class RefreshClient:
                async def asset_pages(self, owner_id: str):
                    if owner_id != OWNER_ID:
                        raise AssertionError(owner_id)
                    yield [replace(asset(), is_trashed=True)]
                    refresh_paused.set()
                    await finish_refresh.wait()

            with Catalog(root / "catalog.db") as catalog:
                entry = catalog.add_uploaded(
                    replace(asset(), is_trashed=True), "stable-photo.jpg"
                )
                lock = trio.Lock()
                mutation = MutationClient()
                mounted = library(
                    catalog,
                    root,
                    mutation=mutation,
                    mutation_session=session(),
                    remote_delete=True,
                    catalog_lock=lock,
                )

                async def restore() -> None:
                    await mounted.remote_restore(entry.asset.id)
                    restore_done.set()

                async with trio.open_nursery() as nursery:
                    nursery.start_soon(
                        refresh_catalog, catalog, RefreshClient(), session(), lock
                    )
                    await refresh_paused.wait()
                    nursery.start_soon(restore)
                    await wait_all_tasks_blocked()
                    self.assertEqual(mutation.restores, [])
                    finish_refresh.set()
                    await restore_done.wait()

                restored = catalog.by_id(entry.asset.id)
                assert restored is not None
                self.assertFalse(restored.asset.is_trashed)
                self.assertEqual(
                    (restored.inode, restored.name), (entry.inode, entry.name)
                )

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))


if __name__ == "__main__":
    unittest.main()
