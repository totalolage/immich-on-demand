from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

import trio

from immich_on_demand.catalog import Catalog
from immich_on_demand.immich import ServerSession, UploadResult
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


class ReadClient:
    def __init__(self, uploaded: Asset, error: Exception | None = None) -> None:
        self.uploaded = uploaded
        self.error = error
        self.on_asset = lambda: None

    async def asset(self, asset_id: str) -> Asset:
        self.on_asset()
        if self.error is not None:
            raise self.error
        return self.uploaded


class MutationClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.uploads = 0
        self.trashes: list[tuple[str, bool]] = []
        self.on_upload = lambda: None

    async def upload(self, path: Path, media_types: frozenset[str]) -> UploadResult:
        self.uploads += 1
        self.on_upload()
        if self.error is not None:
            raise self.error
        return UploadResult(ASSET_ID, True)

    async def trash(self, asset_id: str, *, trash_enabled: bool) -> None:
        if self.error is not None:
            raise self.error
        self.trashes.append((asset_id, trash_enabled))


class Cache:
    async def read(self, item: Asset, offset: int, size: int) -> bytes:
        return b"hello"[offset : offset + size]


def library(
    catalog: Catalog,
    root: Path,
    *,
    read: ReadClient | None = None,
    mutation: MutationClient | None = None,
    mutation_session: ServerSession | None = None,
    remote_delete: bool = False,
) -> Library:
    return Library(
        catalog,
        read or ReadClient(asset()),  # type: ignore[arg-type]
        Cache(),  # type: ignore[arg-type]
        settings(root, remote_delete=remote_delete),
        mutation_client=mutation,  # type: ignore[arg-type]
        mutation_session=mutation_session,
    )


class LibraryTest(unittest.TestCase):
    def test_lists_looks_up_and_reads_immutable_assets(self) -> None:
        async def scenario(root: Path) -> None:
            with Catalog(root / "catalog.db") as catalog:
                entry = catalog.add_uploaded(asset(), "photo.jpg")
                mounted = library(catalog, root)

                self.assertEqual(mounted.list(), [entry])
                self.assertEqual(mounted.lookup("photo.jpg"), entry)
                self.assertEqual(mounted.lookup(entry.inode), entry)
                self.assertEqual(await mounted.read(entry, 1, 3), b"ell")
                self.assertFalse(hasattr(mounted, "overwrite"))
                self.assertFalse(hasattr(mounted, "rename"))

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_lookup_never_exposes_non_visible_catalog_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Catalog(root / "catalog.db") as catalog:
                entries = (
                    catalog.add_uploaded(replace(asset(), is_trashed=True), "trashed.jpg"),
                    catalog.add_uploaded(replace(asset(OTHER_ID), is_offline=True), "offline.jpg"),
                    catalog.add_uploaded(replace(asset(THIRD_ID), visibility="hidden"), "hidden.jpg"),
                    catalog.add_uploaded(replace(asset(FOURTH_ID), size=None), "unknown-size.jpg"),
                )
                mounted = library(catalog, root)

                self.assertEqual(mounted.list(), [])
                for entry in entries:
                    self.assertIsNone(mounted.lookup(entry.name))
                    self.assertIsNone(mounted.lookup(entry.inode))

    def test_upload_commits_only_after_upload_and_authoritative_fetch(self) -> None:
        async def scenario(root: Path) -> None:
            staged = root / "staged"
            staged.write_bytes(b"hello")
            with Catalog(root / "catalog.db") as catalog:
                mutation = MutationClient()
                read = ReadClient(asset())
                mutation.on_upload = lambda: self.assertEqual(catalog.list_visible(), [])
                read.on_asset = lambda: self.assertEqual(catalog.list_visible(), [])
                mounted = library(
                    catalog, root, read=read, mutation=mutation, mutation_session=session()
                )

                entry = await mounted.upload_new(staged, "new.jpg")

                self.assertEqual(entry.name, "new.jpg")
                self.assertEqual(catalog.by_name("new.jpg"), entry)
                self.assertEqual(staged.read_bytes(), b"hello")

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_upload_failures_leave_recovery_bytes_and_catalog_untouched(self) -> None:
        async def scenario(root: Path) -> None:
            staged = root / "staged"
            staged.write_bytes(b"recover me")
            for read, mutation in (
                (ReadClient(asset()), MutationClient(OSError("upload failed"))),
                (ReadClient(asset(), OSError("fetch failed")), MutationClient()),
            ):
                with Catalog(root / f"catalog-{mutation.error is None}.db") as catalog:
                    mounted = library(
                        catalog, root, read=read, mutation=mutation, mutation_session=session()
                    )
                    with self.assertRaises(OSError):
                        await mounted.upload_new(staged, "new.jpg")
                    self.assertEqual(catalog.list_visible(), [])
                    self.assertEqual(staged.read_bytes(), b"recover me")

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_upload_refuses_existing_name_or_missing_mutation_access(self) -> None:
        async def scenario(root: Path) -> None:
            staged = root / "staged"
            staged.write_bytes(b"hello")
            with Catalog(root / "catalog.db") as catalog:
                catalog.add_uploaded(asset(OTHER_ID), "new.jpg")
                mutation = MutationClient()
                with self.assertRaises(FileExistsError):
                    await library(
                        catalog, root, mutation=mutation, mutation_session=session()
                    ).upload_new(staged, "new.jpg")
                self.assertEqual(mutation.uploads, 0)
                with self.assertRaises(LibraryError):
                    await library(catalog, root).upload_new(staged, "free.jpg")

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

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
                self.assertEqual(mutation.trashes, [(ASSET_ID, True)])
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


if __name__ == "__main__":
    unittest.main()
