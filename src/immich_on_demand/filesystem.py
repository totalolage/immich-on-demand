from __future__ import annotations

from dataclasses import dataclass, field
import errno
import logging
import os
from pathlib import Path
import stat
import tempfile
import time

import pyfuse3
import trio

from .catalog import CatalogAsset
from .library import Library, LibraryError
from .model import safe_filename


LOGGER = logging.getLogger(__name__)
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


@dataclass(slots=True)
class _StagedFile:
    inode: int
    name: str
    directory: Path
    path: Path
    descriptor: int
    uploaded: bool = False
    failure_errno: int | None = None
    lock: trio.Lock = field(default_factory=trio.Lock)


class ImmichFilesystem(pyfuse3.Operations):
    """Flat pyfuse3 adapter over a Library."""

    def __init__(self, library: Library, staging_root: Path) -> None:
        super().__init__()
        self.library = library
        self.staging_root = staging_root
        staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(staging_root)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError("upload recovery root must be a directory owned by this user")
        os.chmod(staging_root, 0o700)
        self._started_ns = time.time_ns()
        self._next_handle = 1
        self._next_staged_inode = 1 << 63
        self._reads: dict[int, CatalogAsset] = {}
        self._directories: dict[int, tuple[tuple[bytes, pyfuse3.EntryAttributes], ...]] = {}
        self._staged_handles: dict[int, _StagedFile] = {}
        self._staged_inodes: dict[int, _StagedFile] = {}
        self._staged_names: dict[str, _StagedFile] = {}

    def _handle(self) -> int:
        handle = self._next_handle
        self._next_handle += 1
        return handle

    @staticmethod
    def _name(value: bytes) -> str:
        try:
            name = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise pyfuse3.FUSEError(errno.EILSEQ) from error
        if safe_filename(name, _ZERO_UUID) != name:
            raise pyfuse3.FUSEError(errno.EINVAL)
        return name

    def _remote(self, identity: str | int) -> CatalogAsset:
        entry = self.library.lookup(identity)
        if entry is None or not entry.asset.visible:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return entry

    def _attributes(self, inode: int) -> pyfuse3.EntryAttributes:
        if inode == pyfuse3.ROOT_INODE:
            return self._stat(inode, stat.S_IFDIR | 0o700, 0, self._started_ns, self._started_ns, 2)
        staged = self._staged_inodes.get(inode)
        if staged is not None:
            value = os.fstat(staged.descriptor)
            return self._stat(
                inode,
                stat.S_IFREG | 0o600,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
                1,
            )
        entry = self._remote(inode)
        assert entry.asset.size is not None
        return self._stat(
            entry.inode,
            stat.S_IFREG | 0o400,
            entry.asset.size,
            entry.asset.modified_ns,
            entry.asset.created_ns,
            1,
        )

    @staticmethod
    def _stat(
        inode: int, mode: int, size: int, modified_ns: int, created_ns: int, links: int
    ) -> pyfuse3.EntryAttributes:
        value = pyfuse3.EntryAttributes()
        value.st_ino = inode
        value.generation = 0
        value.entry_timeout = 1
        value.attr_timeout = 1
        value.st_mode = mode
        value.st_nlink = links
        value.st_uid = os.getuid()
        value.st_gid = os.getgid()
        value.st_rdev = 0
        value.st_size = size
        value.st_blksize = 4096
        value.st_blocks = (size + 511) // 512
        value.st_atime_ns = modified_ns
        value.st_mtime_ns = modified_ns
        value.st_ctime_ns = created_ns
        value.st_birthtime_ns = created_ns
        return value

    async def getattr(self, inode: int, ctx: pyfuse3.RequestContext) -> pyfuse3.EntryAttributes:
        return self._attributes(inode)

    async def lookup(
        self, parent_inode: int, name: bytes, ctx: pyfuse3.RequestContext
    ) -> pyfuse3.EntryAttributes:
        if parent_inode != pyfuse3.ROOT_INODE:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if name in {b".", b".."}:
            return self._attributes(pyfuse3.ROOT_INODE)
        decoded = self._name(name)
        staged = self._staged_names.get(decoded)
        return self._attributes(staged.inode if staged is not None else self._remote(decoded).inode)

    async def opendir(self, inode: int, ctx: pyfuse3.RequestContext) -> int:
        if inode != pyfuse3.ROOT_INODE:
            self._remote(inode)
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        staged_names = set(self._staged_names)
        entries = [(item.name, item.inode) for item in self._staged_names.values()]
        entries.extend(
            (entry.name, entry.inode) for entry in self.library.list() if entry.name not in staged_names
        )
        handle = self._handle()
        self._directories[handle] = tuple(
            (name.encode("utf-8"), self._attributes(entry_inode))
            for name, entry_inode in sorted(entries)
        )
        return handle

    async def readdir(self, fh: int, start_id: int, token: pyfuse3.ReaddirToken) -> None:
        entries = self._directories.get(fh)
        if entries is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        for index, (name, attributes) in enumerate(entries, 1):
            if index <= start_id:
                continue
            if not pyfuse3.readdir_reply(token, name, attributes, index):
                return

    async def releasedir(self, fh: int) -> None:
        if self._directories.pop(fh, None) is None:
            raise pyfuse3.FUSEError(errno.EBADF)

    async def open(
        self, inode: int, flags: int, ctx: pyfuse3.RequestContext
    ) -> pyfuse3.FileInfo:
        if inode == pyfuse3.ROOT_INODE:
            raise pyfuse3.FUSEError(errno.EISDIR)
        if flags & os.O_ACCMODE != os.O_RDONLY or flags & (os.O_APPEND | os.O_TRUNC):
            raise pyfuse3.FUSEError(errno.EROFS)
        entry = self._remote(inode)
        try:
            self.library.acquire(entry)
        except Exception as error:
            raise pyfuse3.FUSEError(errno.EIO) from error
        handle = self._handle()
        self._reads[handle] = entry
        return pyfuse3.FileInfo(fh=handle, keep_cache=True)

    async def read(self, fh: int, off: int, size: int) -> bytes:
        staged = self._staged_handles.get(fh)
        if staged is not None:
            try:
                return os.pread(staged.descriptor, size, off)
            except OSError as error:
                raise pyfuse3.FUSEError(error.errno or errno.EIO) from error
        entry = self._reads.get(fh)
        if entry is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        try:
            return await self.library.read(entry, off, size)
        except ValueError as error:
            raise pyfuse3.FUSEError(errno.EINVAL) from error
        except Exception as error:
            raise pyfuse3.FUSEError(errno.EIO) from error

    async def release(self, fh: int) -> None:
        entry = self._reads.pop(fh, None)
        if entry is not None:
            try:
                self.library.release(entry)
            except Exception as error:
                raise pyfuse3.FUSEError(errno.EIO) from error
            return
        staged = self._staged_handles.get(fh)
        if staged is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        error: pyfuse3.FUSEError | None = None
        async with staged.lock:
            if not staged.uploaded and staged.failure_errno is None:
                try:
                    await self._upload(staged)
                except pyfuse3.FUSEError as caught:
                    error = caught
            os.close(staged.descriptor)
            self._staged_handles.pop(fh, None)
            self._staged_inodes.pop(staged.inode, None)
            self._staged_names.pop(staged.name, None)
        if error is not None:
            raise error

    async def create(
        self,
        parent_inode: int,
        name: bytes,
        mode: int,
        flags: int,
        ctx: pyfuse3.RequestContext,
    ) -> tuple[pyfuse3.FileInfo, pyfuse3.EntryAttributes]:
        if parent_inode != pyfuse3.ROOT_INODE:
            self._remote(parent_inode)
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if not self.library.mutation_enabled:
            raise pyfuse3.FUSEError(errno.EROFS)
        decoded = self._name(name)
        if decoded in self._staged_names or self.library.lookup(decoded) is not None:
            raise pyfuse3.FUSEError(errno.EEXIST)
        directory = Path(tempfile.mkdtemp(prefix=".upload-", dir=self.staging_root))
        os.chmod(directory, 0o700)
        path = directory / decoded
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except BaseException:
            directory.rmdir()
            raise
        inode = self._next_staged_inode
        self._next_staged_inode += 1
        staged = _StagedFile(inode, decoded, directory, path, descriptor)
        handle = self._handle()
        self._staged_handles[handle] = staged
        self._staged_inodes[inode] = staged
        self._staged_names[decoded] = staged
        return pyfuse3.FileInfo(fh=handle, direct_io=True), self._attributes(inode)

    async def write(self, fh: int, off: int, buf: bytes) -> int:
        staged = self._staged_handles.get(fh)
        if staged is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        async with staged.lock:
            if staged.uploaded:
                raise pyfuse3.FUSEError(errno.EROFS)
            try:
                written = os.pwrite(staged.descriptor, buf, off)
            except OSError as error:
                raise pyfuse3.FUSEError(error.errno or errno.EIO) from error
            if written != len(buf):
                raise pyfuse3.FUSEError(errno.EIO)
            staged.failure_errno = None
            return written

    async def flush(self, fh: int) -> None:
        if fh in self._reads:
            return
        staged = self._staged_handles.get(fh)
        if staged is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        async with staged.lock:
            if staged.failure_errno is not None:
                raise pyfuse3.FUSEError(staged.failure_errno)
            if not staged.uploaded:
                await self._upload(staged)

    async def fsync(self, fh: int, datasync: bool) -> None:
        if fh in self._reads:
            return
        staged = self._staged_handles.get(fh)
        if staged is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        async with staged.lock:
            try:
                os.fsync(staged.descriptor)
            except OSError as error:
                raise pyfuse3.FUSEError(error.errno or errno.EIO) from error

    async def _upload(self, staged: _StagedFile) -> None:
        try:
            os.fsync(staged.descriptor)
            await self.library.upload_new(staged.path, staged.name)
        except (ValueError, FileExistsError) as error:
            code = errno.EEXIST if isinstance(error, FileExistsError) else errno.EINVAL
            staged.failure_errno = code
            raise pyfuse3.FUSEError(code) from error
        except Exception as error:
            staged.failure_errno = errno.EIO
            raise pyfuse3.FUSEError(errno.EIO) from error
        staged.uploaded = True
        try:
            staged.path.unlink()
            staged.directory.rmdir()
        except OSError:
            LOGGER.warning("uploaded %s but could not remove its recovery file", staged.name)

    async def unlink(
        self, parent_inode: int, name: bytes, ctx: pyfuse3.RequestContext
    ) -> None:
        if parent_inode != pyfuse3.ROOT_INODE:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        decoded = self._name(name)
        if decoded in self._staged_names:
            raise pyfuse3.FUSEError(errno.EBUSY)
        entry = self._remote(decoded)
        try:
            await self.library.remote_trash(entry)
        except (LibraryError, PermissionError) as error:
            raise pyfuse3.FUSEError(errno.EPERM) from error
        except Exception as error:
            raise pyfuse3.FUSEError(errno.EIO) from error

    async def setattr(self, *args: object) -> pyfuse3.EntryAttributes:
        raise pyfuse3.FUSEError(errno.EROFS)

    async def rename(self, *args: object) -> None:
        raise pyfuse3.FUSEError(errno.EROFS)

    async def symlink(self, *args: object) -> pyfuse3.EntryAttributes:
        raise pyfuse3.FUSEError(errno.EROFS)

    async def link(self, *args: object) -> pyfuse3.EntryAttributes:
        raise pyfuse3.FUSEError(errno.EROFS)

    async def mkdir(self, *args: object) -> pyfuse3.EntryAttributes:
        raise pyfuse3.FUSEError(errno.EROFS)

    async def rmdir(self, *args: object) -> None:
        raise pyfuse3.FUSEError(errno.EROFS)

    async def mknod(self, *args: object) -> pyfuse3.EntryAttributes:
        raise pyfuse3.FUSEError(errno.EROFS)
