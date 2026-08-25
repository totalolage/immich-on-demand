from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import pyfuse3
import trio

from immich_on_demand.catalog import CatalogAsset
from immich_on_demand.filesystem import ImmichFilesystem
from immich_on_demand.model import Asset


ASSET_ID = "12345678-1234-4234-8234-123456789abc"


def catalog_entry() -> CatalogAsset:
    return CatalogAsset(
        Asset(
            ASSET_ID,
            "87654321-4321-4321-8321-cba987654321",
            "photo.jpg",
            "image/jpeg",
            5,
            1,
            2,
            "2026-08-25T12:00:00Z",
            "abc=",
            "timeline",
            False,
            False,
            None,
        ),
        2,
        "photo.jpg",
    )


class Cache:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.released: list[str] = []

    def acquire(self, asset_id: str) -> None:
        self.acquired.append(asset_id)

    def release(self, asset_id: str) -> None:
        self.released.append(asset_id)


class FakeLibrary:
    def __init__(self) -> None:
        self.entries = {"photo.jpg": catalog_entry()}
        self.content_cache = Cache()
        self.reads = 0
        self.uploads: list[tuple[str, bytes]] = []
        self.trashes: list[CatalogAsset] = []
        self.upload_error: Exception | None = None
        self.trash_error: Exception | None = None
        self.mutation_enabled = True

    def list(self) -> list[CatalogAsset]:
        return list(self.entries.values())

    def lookup(self, identity: str | int) -> CatalogAsset | None:
        if isinstance(identity, str):
            return self.entries.get(identity)
        return next((entry for entry in self.entries.values() if entry.inode == identity), None)

    async def read(self, entry: CatalogAsset, offset: int, size: int) -> bytes:
        self.reads += 1
        return b"hello"[offset : offset + size]

    def acquire(self, entry: CatalogAsset) -> None:
        self.content_cache.acquire(entry.asset.id)

    def release(self, entry: CatalogAsset) -> None:
        self.content_cache.release(entry.asset.id)

    async def upload_new(self, path: Path, requested_name: str) -> CatalogAsset:
        self.uploads.append((path.name, path.read_bytes()))
        if self.upload_error is not None:
            raise self.upload_error
        uploaded = catalog_entry()
        uploaded = CatalogAsset(uploaded.asset, 3, requested_name)
        self.entries[requested_name] = uploaded
        return uploaded

    async def remote_trash(self, entry: CatalogAsset) -> None:
        if self.trash_error is not None:
            raise self.trash_error
        self.trashes.append(entry)
        self.entries.pop(entry.name)


class FilesystemTest(unittest.TestCase):
    def test_rejects_a_symlinked_recovery_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o755)
            recovery = root / "recovery"
            recovery.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(PermissionError, "recovery root"):
                ImmichFilesystem(FakeLibrary(), recovery)  # type: ignore[arg-type]

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_metadata_lookup_and_listing_do_not_hydrate(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            filesystem = ImmichFilesystem(library, root / "recovery")  # type: ignore[arg-type]

            root_attr = await filesystem.getattr(pyfuse3.ROOT_INODE, None)  # type: ignore[arg-type]
            file_attr = await filesystem.lookup(
                pyfuse3.ROOT_INODE, b"photo.jpg", None  # type: ignore[arg-type]
            )
            replies: list[tuple[bytes, int]] = []

            def reply(token: object, name: bytes, attr: pyfuse3.EntryAttributes, next_id: int) -> bool:
                replies.append((name, attr.st_ino))
                return True

            with patch("immich_on_demand.filesystem.pyfuse3.readdir_reply", side_effect=reply):
                handle = await filesystem.opendir(
                    pyfuse3.ROOT_INODE, None  # type: ignore[arg-type]
                )
                await filesystem.readdir(handle, 0, object())  # type: ignore[arg-type]
                await filesystem.releasedir(handle)

            self.assertTrue(stat.S_ISDIR(root_attr.st_mode))
            self.assertEqual(file_attr.st_size, 5)
            self.assertEqual(replies, [(b"photo.jpg", 2)])
            self.assertEqual(library.reads, 0)
            self.assertEqual(library.content_cache.acquired, [])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_remote_read_acquires_and_releases_cache_lifecycle(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            filesystem = ImmichFilesystem(library, root / "recovery")  # type: ignore[arg-type]
            info = await filesystem.open(2, os.O_RDONLY, None)  # type: ignore[arg-type]

            self.assertEqual(library.content_cache.acquired, [ASSET_ID])
            self.assertEqual(await filesystem.read(info.fh, 1, 3), b"ell")
            await filesystem.release(info.fh)
            self.assertEqual(library.content_cache.released, [ASSET_ID])
            with self.assertRaises(pyfuse3.FUSEError) as denied:
                await filesystem.open(2, os.O_WRONLY, None)  # type: ignore[arg-type]
            self.assertEqual(denied.exception.errno, errno.EROFS)
            with self.assertRaises(pyfuse3.FUSEError) as existing:
                await filesystem.create(
                    pyfuse3.ROOT_INODE,
                    b"photo.jpg",
                    0o600,
                    os.O_WRONLY,
                    None,  # type: ignore[arg-type]
                )
            self.assertEqual(existing.exception.errno, errno.EEXIST)
            library.mutation_enabled = False
            with self.assertRaises(pyfuse3.FUSEError) as read_only:
                await filesystem.create(
                    pyfuse3.ROOT_INODE,
                    b"new.jpg",
                    0o600,
                    os.O_WRONLY,
                    None,  # type: ignore[arg-type]
                )
            self.assertEqual(read_only.exception.errno, errno.EROFS)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_staged_write_uploads_exact_name_once(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            recovery = root / "recovery"
            filesystem = ImmichFilesystem(library, recovery)  # type: ignore[arg-type]
            info, attributes = await filesystem.create(
                pyfuse3.ROOT_INODE, b"new.jpg", 0o644, os.O_WRONLY, None  # type: ignore[arg-type]
            )

            self.assertEqual(await filesystem.write(info.fh, 0, b"hello"), 5)
            await filesystem.fsync(info.fh, False)
            staged = next(recovery.rglob("new.jpg"))
            self.assertEqual(staged.read_bytes(), b"hello")
            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(staged.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(recovery.stat().st_mode), 0o700)
            await filesystem.flush(info.fh)
            await filesystem.flush(info.fh)
            self.assertEqual(library.uploads, [("new.jpg", b"hello")])
            self.assertFalse(staged.exists())
            self.assertEqual(attributes.st_size, 0)
            await filesystem.release(info.fh)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_upload_failure_retains_private_recovery_bytes(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            library.upload_error = OSError("offline")
            recovery = root / "recovery"
            filesystem = ImmichFilesystem(library, recovery)  # type: ignore[arg-type]
            info, _ = await filesystem.create(
                pyfuse3.ROOT_INODE, b"recover.jpg", 0o600, os.O_WRONLY, None  # type: ignore[arg-type]
            )
            await filesystem.write(info.fh, 0, b"recover me")

            with self.assertRaises(pyfuse3.FUSEError) as failed:
                await filesystem.flush(info.fh)
            self.assertEqual(failed.exception.errno, errno.EIO)
            await filesystem.release(info.fh)
            staged = next(recovery.rglob("recover.jpg"))
            self.assertEqual(staged.read_bytes(), b"recover me")
            self.assertEqual(library.uploads, [("recover.jpg", b"recover me")])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_unlink_uses_remote_trash_guard_and_rejects_other_mutations(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            library.trash_error = PermissionError("remote deletion disabled")
            filesystem = ImmichFilesystem(library, root / "recovery")  # type: ignore[arg-type]

            with self.assertRaises(pyfuse3.FUSEError) as guarded:
                await filesystem.unlink(
                    pyfuse3.ROOT_INODE, b"photo.jpg", None  # type: ignore[arg-type]
                )
            self.assertEqual(guarded.exception.errno, errno.EPERM)
            library.trash_error = None
            await filesystem.unlink(pyfuse3.ROOT_INODE, b"photo.jpg", None)  # type: ignore[arg-type]
            self.assertEqual([entry.name for entry in library.trashes], ["photo.jpg"])

            for operation in (
                filesystem.rename(1, b"a", 1, b"b", 0, None),  # type: ignore[arg-type]
                filesystem.symlink(1, b"a", b"target", None),  # type: ignore[arg-type]
                filesystem.setattr(2, None, None, None, None),  # type: ignore[arg-type]
            ):
                with self.assertRaises(pyfuse3.FUSEError) as rejected:
                    await operation
                self.assertEqual(rejected.exception.errno, errno.EROFS)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))


if __name__ == "__main__":
    unittest.main()
