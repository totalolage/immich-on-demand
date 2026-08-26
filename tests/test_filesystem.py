from __future__ import annotations

from collections.abc import Callable
import errno
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pyfuse3
import trio

from immich_on_demand.catalog import CatalogAsset
from immich_on_demand.filesystem import ImmichFilesystem
from immich_on_demand.model import Asset
from immich_on_demand.uploads import UploadErrorCode, UploadQueue, UploadState


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
OWNER_ID = "87654321-4321-4321-8321-cba987654321"
ORIGIN = "https://photos.example.test"


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
        self.trashes: list[CatalogAsset] = []
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

    async def remote_trash(self, entry: CatalogAsset) -> None:
        if self.trash_error is not None:
            raise self.trash_error
        self.trashes.append(entry)
        self.entries.pop(entry.name)


class FilesystemTest(unittest.TestCase):
    def filesystem(
        self,
        library: FakeLibrary,
        root: Path,
        *,
        on_pending: Callable[[], None] | None = None,
        minimum_free_bytes: int = 0,
    ) -> tuple[ImmichFilesystem, UploadQueue]:
        queue = UploadQueue(root, minimum_free_bytes=minimum_free_bytes)
        self.addCleanup(queue.close)
        filesystem = ImmichFilesystem(
            library,  # type: ignore[arg-type]
            queue,
            ORIGIN,
            OWNER_ID,
            on_pending=on_pending,
        )
        return filesystem, queue

    def test_metadata_lookup_and_listing_do_not_hydrate(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            filesystem, _ = self.filesystem(library, root / "recovery")

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
            filesystem, _ = self.filesystem(library, root / "recovery")
            info = await filesystem.open(2, os.O_RDONLY, None)  # type: ignore[arg-type]

            self.assertEqual(library.content_cache.acquired, [ASSET_ID])
            self.assertFalse(info.keep_cache)
            self.assertEqual(await filesystem.read(info.fh, 1, 3), b"ell")
            await filesystem.release(info.fh)
            self.assertEqual(library.content_cache.released, [ASSET_ID])
            reopened = await filesystem.open(2, os.O_RDONLY, None)  # type: ignore[arg-type]
            self.assertFalse(reopened.keep_cache)
            await filesystem.release(reopened.fh)
            self.assertEqual(library.content_cache.acquired, [ASSET_ID, ASSET_ID])
            self.assertEqual(library.content_cache.released, [ASSET_ID, ASSET_ID])
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

    def test_remote_open_rejects_noatime_without_hydrating(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            filesystem, _ = self.filesystem(library, root / "recovery")

            with self.assertRaises(pyfuse3.FUSEError) as rejected:
                await filesystem.open(
                    2, os.O_RDONLY | os.O_NOATIME, None  # type: ignore[arg-type]
                )

            self.assertEqual(rejected.exception.errno, errno.EOPNOTSUPP)
            self.assertEqual(library.content_cache.acquired, [])
            self.assertEqual(library.reads, 0)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_final_close_seals_pending_without_upload_and_wakes_worker(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            recovery = root / "recovery"
            wakes: list[None] = []
            filesystem, queue = self.filesystem(
                library, recovery, on_pending=lambda: wakes.append(None)
            )
            info, attributes = await filesystem.create(
                pyfuse3.ROOT_INODE, b"new.jpg", 0o644, os.O_WRONLY, None  # type: ignore[arg-type]
            )

            self.assertEqual(await filesystem.write(info.fh, 0, b"hello"), 5)
            await filesystem.fsync(info.fh, False)
            writing = queue.list()
            self.assertEqual(len(writing), 1)
            self.assertEqual(writing[0].state, UploadState.WRITING)
            self.assertEqual(writing[0].payload_path.read_bytes(), b"hello")
            self.assertEqual(stat.S_IMODE(writing[0].payload_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(writing[0].payload_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(recovery.stat().st_mode), 0o700)
            await filesystem.flush(info.fh)
            await filesystem.flush(info.fh)
            self.assertEqual(attributes.st_size, 0)
            with patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"):
                await filesystem.release(info.fh)
            self.assertEqual(wakes, [None])
            with self.assertRaises(pyfuse3.FUSEError) as missing:
                await filesystem.lookup(
                    pyfuse3.ROOT_INODE, b"new.jpg", None  # type: ignore[arg-type]
                )
            self.assertEqual(missing.exception.errno, errno.ENOENT)

            pending = queue.list()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].state, UploadState.PENDING)
            self.assertEqual(pending[0].requested_name, "new.jpg")
            self.assertEqual(pending[0].server_origin, ORIGIN)
            self.assertEqual(pending[0].owner_id, OWNER_ID)
            self.assertEqual(pending[0].payload_path.read_bytes(), b"hello")
            queue.close()
            with UploadQueue(recovery) as recovered:
                self.assertEqual(recovered.list(), pending)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_reopened_staged_file_seals_after_final_release(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            filesystem, queue = self.filesystem(library, root / "recovery")
            creator, attributes = await filesystem.create(
                pyfuse3.ROOT_INODE,
                b"shared.jpg",
                0o600,
                os.O_RDWR,
                None,  # type: ignore[arg-type]
            )
            await filesystem.write(creator.fh, 0, b"first")
            reopened = await filesystem.open(
                attributes.st_ino, os.O_RDWR, None  # type: ignore[arg-type]
            )

            self.assertEqual(await filesystem.read(reopened.fh, 0, 5), b"first")
            await filesystem.flush(creator.fh)
            await filesystem.release(creator.fh)
            self.assertEqual(queue.list()[0].state, UploadState.WRITING)
            await filesystem.write(reopened.fh, 5, b"-second")
            with patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"):
                await filesystem.release(reopened.fh)

            self.assertEqual(queue.list()[0].state, UploadState.PENDING)
            self.assertEqual(queue.list()[0].payload_path.read_bytes(), b"first-second")

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_existing_queue_job_reserves_its_requested_name(self) -> None:
        async def scenario(root: Path) -> None:
            filesystem, queue = self.filesystem(FakeLibrary(), root / "recovery")
            first, _ = await filesystem.create(
                pyfuse3.ROOT_INODE,
                b"reserved.jpg",
                0o600,
                os.O_WRONLY,
                None,  # type: ignore[arg-type]
            )
            await filesystem.write(first.fh, 0, b"first")
            with patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"):
                await filesystem.release(first.fh)

            with self.assertRaises(pyfuse3.FUSEError) as duplicate:
                await filesystem.create(
                    pyfuse3.ROOT_INODE,
                    b"reserved.jpg",
                    0o600,
                    os.O_WRONLY,
                    None,  # type: ignore[arg-type]
                )

            self.assertEqual(duplicate.exception.errno, errno.EEXIST)
            self.assertEqual(len(queue.list()), 1)
            self.assertEqual(queue.list()[0].state, UploadState.PENDING)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_staged_reopen_honors_truncate_and_append(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            filesystem, queue = self.filesystem(library, root / "recovery")
            creator, attributes = await filesystem.create(
                pyfuse3.ROOT_INODE,
                b"flags.jpg",
                0o600,
                os.O_RDWR,
                None,  # type: ignore[arg-type]
            )
            await filesystem.write(creator.fh, 0, b"abcdef")
            truncating = await filesystem.open(
                attributes.st_ino,
                os.O_WRONLY | os.O_TRUNC,
                None,  # type: ignore[arg-type]
            )
            await filesystem.write(truncating.fh, 0, b"X")
            appending = await filesystem.open(
                attributes.st_ino,
                os.O_WRONLY | os.O_APPEND | os.O_NOATIME,
                None,  # type: ignore[arg-type]
            )
            await filesystem.write(appending.fh, 0, b"-A")
            self.assertEqual(await filesystem.read(creator.fh, 0, 16), b"X-A")

            await filesystem.release(truncating.fh)
            await filesystem.release(appending.fh)
            with patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"):
                await filesystem.release(creator.fh)
            self.assertEqual(queue.list()[0].state, UploadState.PENDING)
            self.assertEqual(queue.list()[0].payload_path.read_bytes(), b"X-A")

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_staged_setattr_is_private_and_remote_setattr_is_read_only(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            filesystem, queue = self.filesystem(library, root / "recovery")
            info, attributes = await filesystem.create(
                pyfuse3.ROOT_INODE,
                b"metadata.jpg",
                0o644,
                os.O_RDWR,
                None,  # type: ignore[arg-type]
            )
            await filesystem.write(info.fh, 0, b"abcdef")
            requested = pyfuse3.EntryAttributes()
            requested.st_size = 3
            requested.st_mode = stat.S_IFREG | 0o644
            requested.st_uid = os.getuid()
            requested.st_gid = os.getgid()
            requested.st_atime_ns = 1_700_000_000_000_000_000
            requested.st_mtime_ns = 1_700_000_001_000_000_000
            fields = SimpleNamespace(
                update_size=True,
                update_mode=True,
                update_uid=True,
                update_gid=True,
                update_atime=True,
                update_mtime=True,
                update_ctime=False,
            )

            result = await filesystem.setattr(
                attributes.st_ino, requested, fields, info.fh, None  # type: ignore[arg-type]
            )
            staged = queue.list()[0].payload_path
            self.assertEqual(staged.read_bytes(), b"abc")
            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(result.st_mode), 0o600)
            self.assertEqual(result.st_mtime_ns, requested.st_mtime_ns)

            requested.st_uid = os.getuid() + 1
            with self.assertRaises(pyfuse3.FUSEError) as foreign:
                await filesystem.setattr(
                    attributes.st_ino,
                    requested,
                    fields,
                    info.fh,
                    None,  # type: ignore[arg-type]
                )
            self.assertEqual(foreign.exception.errno, errno.EPERM)

            remote_fields = SimpleNamespace(
                update_size=False,
                update_mode=False,
                update_uid=False,
                update_gid=False,
                update_atime=False,
                update_mtime=True,
                update_ctime=False,
            )
            with self.assertRaises(pyfuse3.FUSEError) as remote:
                await filesystem.setattr(
                    2, requested, remote_fields, None, None  # type: ignore[arg-type]
                )
            self.assertEqual(remote.exception.errno, errno.EROFS)

            with self.assertLogs("immich_on_demand.filesystem", level="ERROR"):
                with patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"):
                    await filesystem.release(info.fh)
            self.assertTrue(staged.exists())
            self.assertEqual(queue.list()[0].state, UploadState.BLOCKED)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_statfs_reports_private_staging_filesystem_capacity(self) -> None:
        async def scenario(root: Path) -> None:
            recovery = root / "recovery"
            filesystem, _ = self.filesystem(FakeLibrary(), recovery)
            expected = os.statvfs(recovery)

            result = await filesystem.statfs(None)  # type: ignore[arg-type]

            self.assertEqual(result.f_bsize, expected.f_bsize)
            self.assertEqual(result.f_frsize, expected.f_frsize)
            self.assertEqual(result.f_blocks, expected.f_blocks)
            self.assertEqual(result.f_bavail, expected.f_bavail)
            self.assertEqual(result.f_namemax, min(expected.f_namemax, 255))

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_callback_failure_keeps_the_durable_pending_upload(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()

            def broken_callback() -> None:
                raise OSError("worker wake failed")

            recovery = root / "recovery"
            filesystem, queue = self.filesystem(
                library, recovery, on_pending=broken_callback
            )
            info, _ = await filesystem.create(
                pyfuse3.ROOT_INODE,
                b"committed.jpg",
                0o600,
                os.O_WRONLY,
                None,  # type: ignore[arg-type]
            )
            await filesystem.write(info.fh, 0, b"hello")
            with (
                self.assertLogs("immich_on_demand.filesystem", level="ERROR") as logs,
                patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"),
            ):
                await filesystem.release(info.fh)

            pending = queue.list()[0]
            self.assertEqual(pending.state, UploadState.PENDING)
            self.assertEqual(pending.payload_path.read_bytes(), b"hello")
            self.assertIn("could not wake upload worker", "\n".join(logs.output))

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_failed_writes_stay_failed_and_retain_recovery_bytes(self) -> None:
        real_pwrite = os.pwrite

        def no_space(descriptor: int, data: bytes, offset: int) -> int:
            raise OSError(errno.ENOSPC, "no space")

        def short_write(descriptor: int, data: bytes, offset: int) -> int:
            return real_pwrite(descriptor, data[:1], offset)

        async def scenario(root: Path) -> None:
            cases = (
                ("exception.jpg", no_space, errno.ENOSPC, 3, b"abcXYZ"),
                ("short.jpg", short_write, errno.EIO, 4, b"abcdXYZ"),
            )
            for name, failure, expected_errno, next_offset, expected_bytes in cases:
                with self.subTest(name=name):
                    library = FakeLibrary()
                    recovery = root / name
                    filesystem, queue = self.filesystem(library, recovery)
                    info, _ = await filesystem.create(
                        pyfuse3.ROOT_INODE,
                        name.encode(),
                        0o600,
                        os.O_WRONLY,
                        None,  # type: ignore[arg-type]
                    )
                    await filesystem.write(info.fh, 0, b"abc")

                    with patch(
                        "immich_on_demand.uploads.os.pwrite", side_effect=failure
                    ):
                        with self.assertRaises(pyfuse3.FUSEError) as failed:
                            await filesystem.write(info.fh, 3, b"def")
                    self.assertEqual(failed.exception.errno, expected_errno)

                    await filesystem.write(info.fh, next_offset, b"XYZ")
                    with self.assertRaises(pyfuse3.FUSEError) as sticky:
                        await filesystem.flush(info.fh)
                    self.assertEqual(sticky.exception.errno, expected_errno)

                    with self.assertLogs(
                        "immich_on_demand.filesystem", level="ERROR"
                    ):
                        with patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"):
                            await filesystem.release(info.fh)
                    blocked = queue.list()[0]
                    self.assertEqual(blocked.state, UploadState.BLOCKED)
                    self.assertEqual(blocked.error, UploadErrorCode.LOCAL_WRITE_FAILED)
                    self.assertEqual(blocked.payload_path.read_bytes(), expected_bytes)
                    queue.close()
                    with UploadQueue(recovery) as recovered:
                        self.assertEqual(recovered.status(blocked.id), blocked)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_fsync_reports_an_earlier_write_failure(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            filesystem, queue = self.filesystem(library, root / "recovery")
            info, _ = await filesystem.create(
                pyfuse3.ROOT_INODE,
                b"failed.jpg",
                0o600,
                os.O_WRONLY,
                None,  # type: ignore[arg-type]
            )

            with patch(
                "immich_on_demand.uploads.os.pwrite",
                side_effect=OSError(errno.ENOSPC, "no space"),
            ):
                with self.assertRaises(pyfuse3.FUSEError):
                    await filesystem.write(info.fh, 0, b"data")

            with patch.object(queue, "sync", wraps=queue.sync) as sync:
                with self.assertRaises(pyfuse3.FUSEError) as sticky:
                    await filesystem.fsync(info.fh, False)
            self.assertEqual(sticky.exception.errno, errno.ENOSPC)
            sync.assert_not_called()

            with self.assertLogs("immich_on_demand.filesystem", level="ERROR"):
                with patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"):
                    await filesystem.release(info.fh)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_failed_open_truncate_prevents_upload(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            recovery = root / "recovery"
            filesystem, queue = self.filesystem(library, recovery)
            info, attributes = await filesystem.create(
                pyfuse3.ROOT_INODE,
                b"truncate.jpg",
                0o600,
                os.O_WRONLY,
                None,  # type: ignore[arg-type]
            )
            await filesystem.write(info.fh, 0, b"complete")

            with patch.object(
                queue, "truncate", side_effect=OSError(errno.ENOSPC, "no space")
            ):
                with self.assertRaises(pyfuse3.FUSEError) as failed:
                    await filesystem.open(
                        attributes.st_ino,
                        os.O_WRONLY | os.O_TRUNC,
                        None,  # type: ignore[arg-type]
                    )
            self.assertEqual(failed.exception.errno, errno.ENOSPC)
            with self.assertRaises(pyfuse3.FUSEError) as sticky:
                await filesystem.flush(info.fh)
            self.assertEqual(sticky.exception.errno, errno.ENOSPC)

            with self.assertLogs("immich_on_demand.filesystem", level="ERROR"):
                with patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"):
                    await filesystem.release(info.fh)

            blocked = queue.list()[0]
            self.assertEqual(blocked.state, UploadState.BLOCKED)
            self.assertEqual(blocked.error, UploadErrorCode.LOCAL_WRITE_FAILED)
            self.assertEqual(blocked.payload_path.read_bytes(), b"complete")

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_failed_setattr_prevents_upload(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            recovery = root / "recovery"
            filesystem, queue = self.filesystem(library, recovery)
            info, attributes = await filesystem.create(
                pyfuse3.ROOT_INODE,
                b"metadata-failure.jpg",
                0o600,
                os.O_WRONLY,
                None,  # type: ignore[arg-type]
            )
            await filesystem.write(info.fh, 0, b"complete")
            requested = pyfuse3.EntryAttributes()
            requested.st_size = 0
            fields = SimpleNamespace(
                update_size=True,
                update_mode=False,
                update_uid=False,
                update_gid=False,
                update_atime=False,
                update_mtime=False,
                update_ctime=False,
            )

            with patch.object(
                queue, "truncate", side_effect=OSError(errno.ENOSPC, "no space")
            ):
                with self.assertRaises(pyfuse3.FUSEError) as failed:
                    await filesystem.setattr(
                        attributes.st_ino,
                        requested,
                        fields,
                        info.fh,
                        None,  # type: ignore[arg-type]
                    )
            self.assertEqual(failed.exception.errno, errno.ENOSPC)
            with self.assertRaises(pyfuse3.FUSEError) as sticky:
                await filesystem.flush(info.fh)
            self.assertEqual(sticky.exception.errno, errno.ENOSPC)

            with self.assertLogs("immich_on_demand.filesystem", level="ERROR"):
                with patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"):
                    await filesystem.release(info.fh)

            blocked = queue.list()[0]
            self.assertEqual(blocked.state, UploadState.BLOCKED)
            self.assertEqual(blocked.error, UploadErrorCode.LOCAL_WRITE_FAILED)
            self.assertEqual(blocked.payload_path.read_bytes(), b"complete")

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_successful_truncate_does_not_clear_an_earlier_write_failure(self) -> None:
        async def scenario(root: Path, via_setattr: bool) -> None:
            library = FakeLibrary()
            recovery = root / ("setattr" if via_setattr else "open")
            filesystem, queue = self.filesystem(library, recovery)
            info, attributes = await filesystem.create(
                pyfuse3.ROOT_INODE,
                b"sticky.jpg",
                0o600,
                os.O_WRONLY,
                None,  # type: ignore[arg-type]
            )
            await filesystem.write(info.fh, 0, b"complete")
            with patch(
                "immich_on_demand.uploads.os.pwrite",
                side_effect=OSError(errno.ENOSPC, "no space"),
            ):
                with self.assertRaises(pyfuse3.FUSEError):
                    await filesystem.write(info.fh, 8, b"missing")

            if via_setattr:
                requested = pyfuse3.EntryAttributes()
                requested.st_size = 0
                fields = SimpleNamespace(
                    update_size=True,
                    update_mode=False,
                    update_uid=False,
                    update_gid=False,
                    update_atime=False,
                    update_mtime=False,
                    update_ctime=False,
                )
                await filesystem.setattr(
                    attributes.st_ino,
                    requested,
                    fields,
                    info.fh,
                    None,  # type: ignore[arg-type]
                )
            else:
                truncating = await filesystem.open(
                    attributes.st_ino,
                    os.O_WRONLY | os.O_TRUNC,
                    None,  # type: ignore[arg-type]
                )
                await filesystem.release(truncating.fh)

            with self.assertRaises(pyfuse3.FUSEError) as sticky:
                await filesystem.flush(info.fh)
            self.assertEqual(sticky.exception.errno, errno.ENOSPC)
            with self.assertLogs("immich_on_demand.filesystem", level="ERROR"):
                with patch("immich_on_demand.filesystem.pyfuse3.invalidate_entry"):
                    await filesystem.release(info.fh)

            blocked = queue.list()[0]
            self.assertEqual(blocked.state, UploadState.BLOCKED)
            self.assertEqual(blocked.error, UploadErrorCode.LOCAL_WRITE_FAILED)
            self.assertEqual(blocked.payload_path.read_bytes(), b"")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for via_setattr in (False, True):
                with self.subTest(via_setattr=via_setattr):
                    trio.run(scenario, root, via_setattr)

    def test_unlink_uses_remote_trash_guard_and_rejects_other_mutations(self) -> None:
        async def scenario(root: Path) -> None:
            library = FakeLibrary()
            library.trash_error = PermissionError("remote deletion disabled")
            filesystem, _ = self.filesystem(library, root / "recovery")

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
            ):
                with self.assertRaises(pyfuse3.FUSEError) as rejected:
                    await operation
                self.assertEqual(rejected.exception.errno, errno.EROFS)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))


if __name__ == "__main__":
    unittest.main()
