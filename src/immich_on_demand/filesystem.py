from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import errno
from functools import partial
import logging
import os
from pathlib import Path
import stat
import time

import pyfuse3
import trio

from .catalog import Catalog, CatalogAsset, CatalogDirectory, CatalogFile
from .library import Library, LibraryError
from .model import safe_filename
from .uploads import (
    UploadErrorCode,
    UploadOperation,
    UploadQueue,
    UploadState,
    UploadStateError,
    UploadStatus,
    WritableUpload,
)


LOGGER = logging.getLogger(__name__)
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


@dataclass(slots=True)
class _StagedFile:
    inode: int
    parent_inode: int
    name: str
    draft: WritableUpload | None
    sealed: UploadStatus | None = None
    open_handles: int = 1
    closed: bool = False
    failure_errno: int | None = None
    lock: trio.Lock = field(default_factory=trio.Lock)

    @property
    def descriptor(self) -> int:
        if self.draft is None:
            raise RuntimeError("sealed upload has no writable descriptor")
        return self.draft.descriptor

    @property
    def path(self) -> Path:
        value = self.sealed if self.sealed is not None else self.draft
        assert value is not None
        return value.payload_path

    @property
    def job_id(self) -> str:
        value = self.sealed if self.sealed is not None else self.draft
        assert value is not None
        return value.id


@dataclass(frozen=True, slots=True)
class _StagedHandle:
    staged: _StagedFile
    descriptor: int
    readable: bool
    writable: bool
    append: bool
    owned_descriptor: bool = False


PendingCallback = Callable[[], None]


class ImmichFilesystem(pyfuse3.Operations):
    """pyfuse3 adapter over the catalog namespace and Library content."""

    def __init__(
        self,
        library: Library,
        catalog: Catalog,
        upload_queue: UploadQueue,
        server_origin: str,
        owner_id: str,
        *,
        on_pending: PendingCallback | None = None,
    ) -> None:
        super().__init__()
        self.library = library
        self.catalog = catalog
        self.upload_queue = upload_queue
        self.server_origin = server_origin
        self.owner_id = owner_id
        self._on_pending = on_pending
        self._started_ns = time.time_ns()
        self._next_handle = 1
        self._next_staged_inode = 1 << 63
        self._reads: dict[int, CatalogAsset] = {}
        self._directories: dict[int, tuple[tuple[bytes, pyfuse3.EntryAttributes], ...]] = {}
        self._staged_handles: dict[int, _StagedHandle] = {}
        self._staged_inodes: dict[int, _StagedFile] = {}
        self._staged_names: dict[str, _StagedFile] = {}
        self._staged_jobs: dict[str, _StagedFile] = {}
        self._namespace_lock = trio.Lock()
        for job in upload_queue.list():
            if (
                job.server_origin == server_origin
                and job.owner_id == owner_id
                and job.state
                in {
                    UploadState.PENDING,
                    UploadState.ATTEMPTING,
                    UploadState.REPLACING,
                    UploadState.BLOCKED,
                }
                and job.size is not None
                and job.sha1 is not None
                and job.created_ns is not None
                and job.modified_ns is not None
            ):
                name = (
                    job.old_name
                    if job.operation is UploadOperation.REPLACEMENT
                    else job.requested_name
                )
                if name is None:
                    continue
                if name in self._staged_names:
                    raise ValueError("queued uploads contain duplicate mounted names")
                inode = self._next_staged_inode
                self._next_staged_inode += 1
                staged = _StagedFile(
                    inode,
                    self._all_inode(),
                    name,
                    None,
                    sealed=job,
                    open_handles=0,
                )
                self._staged_inodes[inode] = staged
                self._staged_names[staged.name] = staged
                self._staged_jobs[job.id] = staged

    def _all_inode(self) -> int:
        node = self.catalog.lookup(pyfuse3.ROOT_INODE, "All")
        if not isinstance(node, CatalogDirectory) or not node.mutation_root:
            raise ValueError("catalog All View is unavailable")
        return node.inode

    def _handle(self) -> int:
        handle = self._next_handle
        self._next_handle += 1
        return handle

    @staticmethod
    def _access(flags: int) -> tuple[bool, bool]:
        access = flags & os.O_ACCMODE
        return access != os.O_WRONLY, access != os.O_RDONLY

    def _open_remote(self, entry: CatalogAsset, flags: int) -> pyfuse3.FileInfo:
        if flags & os.O_ACCMODE != os.O_RDONLY or flags & (os.O_APPEND | os.O_TRUNC):
            raise pyfuse3.FUSEError(errno.EROFS)
        # ponytail: Reference-system GLib uses O_NOATIME for MIME sniffing; add caller-aware
        # policy if legitimate O_NOATIME readers need support.
        if flags & os.O_NOATIME:
            raise pyfuse3.FUSEError(errno.EOPNOTSUPP)
        try:
            self.library.acquire(entry)
        except Exception as error:
            raise pyfuse3.FUSEError(errno.EIO) from error
        handle = self._handle()
        self._reads[handle] = entry
        return pyfuse3.FileInfo(fh=handle, keep_cache=False)

    @staticmethod
    def _name(value: bytes) -> str:
        try:
            name = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise pyfuse3.FUSEError(errno.EILSEQ) from error
        if safe_filename(name, _ZERO_UUID) != name:
            raise pyfuse3.FUSEError(errno.EINVAL)
        return name

    def _node(self, inode: int) -> CatalogDirectory | CatalogFile:
        try:
            node = self.catalog.node(inode)
        except Exception as error:
            raise pyfuse3.FUSEError(errno.EIO) from error
        if node is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return node

    def _remote(self, inode: int) -> CatalogFile:
        node = self._node(inode)
        if isinstance(node, CatalogDirectory):
            raise pyfuse3.FUSEError(errno.EISDIR)
        return node

    @staticmethod
    def _library_entry(node: CatalogFile) -> CatalogAsset:
        return CatalogAsset(node.asset, node.inode, node.name)

    def _attributes(self, inode: int) -> pyfuse3.EntryAttributes:
        staged = self._staged_inodes.get(inode)
        if staged is not None:
            if staged.closed and not staged.open_handles:
                raise pyfuse3.FUSEError(errno.ENOENT)
            if staged.sealed is not None:
                if (
                    staged.sealed.size is None
                    or staged.sealed.modified_ns is None
                    or staged.sealed.created_ns is None
                ):
                    raise pyfuse3.FUSEError(errno.EIO)
                return self._stat(
                    inode,
                    stat.S_IFREG | 0o600,
                    staged.sealed.size,
                    staged.sealed.modified_ns,
                    staged.sealed.created_ns,
                    1,
                )
            value = os.fstat(staged.descriptor)
            return self._stat(
                inode,
                stat.S_IFREG | 0o600,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
                1,
            )
        return self._node_attributes(self._node(inode))

    def _node_attributes(
        self, node: CatalogDirectory | CatalogFile
    ) -> pyfuse3.EntryAttributes:
        if isinstance(node, CatalogDirectory):
            permissions = 0o700 if node.mutation_root else 0o500
            return self._stat(
                node.inode,
                stat.S_IFDIR | permissions,
                0,
                self._started_ns,
                self._started_ns,
                node.nlink,
            )
        assert node.asset.size is not None
        return self._stat(
            node.inode,
            stat.S_IFREG | 0o400,
            node.asset.size,
            node.asset.modified_ns,
            node.asset.created_ns,
            node.nlink,
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
        parent = self._node(parent_inode)
        if isinstance(parent, CatalogFile):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        decoded = name.decode("ascii") if name in {b".", b".."} else self._name(name)
        staged = self._staged_names.get(decoded) if parent.mutation_root else None
        if staged is not None:
            return self._attributes(staged.inode)
        try:
            node = self.catalog.lookup(parent_inode, decoded)
        except Exception as error:
            raise pyfuse3.FUSEError(errno.EIO) from error
        if node is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._node_attributes(node)

    async def opendir(self, inode: int, ctx: pyfuse3.RequestContext) -> int:
        node = self._node(inode)
        if isinstance(node, CatalogFile):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        try:
            children = self.catalog.children(inode)
        except Exception as error:
            raise pyfuse3.FUSEError(errno.EIO) from error
        entries = [
            (entry.name.encode("utf-8"), self._node_attributes(entry.node))
            for entry in children
            if not node.mutation_root or entry.name not in self._staged_names
        ]
        if node.mutation_root:
            entries.extend(
                (staged.name.encode("utf-8"), self._attributes(staged.inode))
                for staged in self._staged_names.values()
            )
        handle = self._handle()
        self._directories[handle] = tuple(
            sorted(entries, key=lambda entry: entry[0])
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
        staged = self._staged_inodes.get(inode)
        if staged is None:
            entry = self._library_entry(self._remote(inode))
            return self._open_remote(entry, flags)

        readable, writable = self._access(flags)
        async with staged.lock:
            if staged.closed:
                raise pyfuse3.FUSEError(errno.ENOENT)
            if staged.sealed is not None:
                if writable or flags & (os.O_APPEND | os.O_TRUNC):
                    raise pyfuse3.FUSEError(errno.EROFS)
                if flags & os.O_NOATIME:
                    raise pyfuse3.FUSEError(errno.EOPNOTSUPP)
                try:
                    descriptor = await trio.to_thread.run_sync(
                        self.upload_queue.open_local, staged.job_id
                    )
                except Exception as error:
                    raise pyfuse3.FUSEError(errno.EIO) from error
                handle = self._handle()
                staged.open_handles += 1
                self._staged_handles[handle] = _StagedHandle(
                    staged,
                    descriptor,
                    True,
                    False,
                    False,
                    True,
                )
                return pyfuse3.FileInfo(fh=handle, direct_io=True)
            else:
                if flags & os.O_TRUNC:
                    if not writable:
                        raise pyfuse3.FUSEError(errno.EACCES)
                    try:
                        await trio.to_thread.run_sync(
                            self.upload_queue.truncate, staged.draft, 0
                        )
                    except OSError as error:
                        staged.failure_errno = error.errno or errno.EIO
                        raise pyfuse3.FUSEError(staged.failure_errno) from error
                    except Exception as error:
                        staged.failure_errno = errno.EIO
                        raise pyfuse3.FUSEError(errno.EIO) from error
                handle = self._handle()
                staged.open_handles += 1
                self._staged_handles[handle] = _StagedHandle(
                    staged,
                    staged.descriptor,
                    readable,
                    writable,
                    bool(flags & os.O_APPEND),
                )
                return pyfuse3.FileInfo(fh=handle, direct_io=True)

    async def read(self, fh: int, off: int, size: int) -> bytes:
        staged_handle = self._staged_handles.get(fh)
        if staged_handle is not None:
            if not staged_handle.readable:
                raise pyfuse3.FUSEError(errno.EBADF)
            try:
                return os.pread(staged_handle.descriptor, size, off)
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
        staged_handle = self._staged_handles.get(fh)
        if staged_handle is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        staged = staged_handle.staged
        pending = False
        async with staged.lock:
            if self._staged_handles.pop(fh, None) is None:
                raise pyfuse3.FUSEError(errno.EBADF)
            staged.open_handles -= 1
            if staged_handle.owned_descriptor:
                try:
                    os.close(staged_handle.descriptor)
                except OSError as error:
                    LOGGER.warning(
                        "could not close queued upload descriptor: %s",
                        type(error).__name__,
                    )
            if staged.sealed is not None:
                if staged.closed and not staged.open_handles:
                    self._staged_inodes.pop(staged.inode, None)
                return
            if staged.open_handles:
                return
            if staged.failure_errno is None:
                try:
                    assert staged.draft is not None
                    sealed = await trio.to_thread.run_sync(
                        self.upload_queue.seal, staged.draft
                    )
                    if sealed.operation is UploadOperation.ORDINARY:
                        # ponytail: one second covers close-then-rename editor saves;
                        # add an explicit publish syscall only if delayed renames matter.
                        sealed = await trio.to_thread.run_sync(
                            partial(
                                self.upload_queue.retry,
                                sealed.id,
                                at_ns=time.time_ns() + 1_000_000_000,
                                revision=sealed.revision,
                            )
                        )
                    staged.sealed = sealed
                    self._staged_jobs[sealed.id] = staged
                    pending = True
                except Exception as error:
                    staged.failure_errno = (
                        error.errno if isinstance(error, OSError) and error.errno else errno.EIO
                    )
            if not pending:
                try:
                    await trio.to_thread.run_sync(
                        self.upload_queue.block_writing,
                        staged.draft,
                        UploadErrorCode.LOCAL_WRITE_FAILED,
                    )
                except Exception as error:
                    LOGGER.error("could not record failed local upload %s: %s", staged.name, error)
            try:
                os.close(staged.descriptor)
            except OSError as error:
                LOGGER.warning("could not close recovery file %s: %s", staged.path, error)
            if pending:
                staged.draft = None
            else:
                staged.closed = True
                self._staged_names.pop(staged.name, None)
        if not pending:
            try:
                await trio.to_thread.run_sync(
                    pyfuse3.invalidate_entry,
                    staged.parent_inode,
                    staged.name.encode("utf-8"),
                    staged.inode,
                )
            except OSError as error:
                if error.errno != errno.ENOENT:
                    LOGGER.warning(
                        "could not invalidate promoted name %s: %s", staged.name, error
                    )
            self._staged_inodes.pop(staged.inode, None)
        if pending:
            if self._on_pending is not None:
                try:
                    self._on_pending()
                except Exception as error:
                    LOGGER.error("could not wake upload worker for %s: %s", staged.name, error)
        else:
            LOGGER.error("upload failed; recovery retained at %s", staged.path)

    async def create(
        self,
        parent_inode: int,
        name: bytes,
        mode: int,
        flags: int,
        ctx: pyfuse3.RequestContext,
    ) -> tuple[pyfuse3.FileInfo, pyfuse3.EntryAttributes]:
        parent = self._node(parent_inode)
        if isinstance(parent, CatalogFile):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if not parent.mutation_root:
            raise pyfuse3.FUSEError(errno.EROFS)
        if not self.library.mutation_enabled:
            raise pyfuse3.FUSEError(errno.EROFS)
        decoded = self._name(name)
        async with self._namespace_lock:
            try:
                queued_name = any(
                    job.requested_name == decoded
                    for job in await trio.to_thread.run_sync(self.upload_queue.list)
                )
            except Exception as error:
                raise pyfuse3.FUSEError(errno.EIO) from error
            try:
                existing = self.catalog.lookup(parent_inode, decoded)
            except Exception as error:
                raise pyfuse3.FUSEError(errno.EIO) from error
            if decoded in self._staged_names or queued_name or existing is not None:
                raise pyfuse3.FUSEError(errno.EEXIST)
            try:
                draft = await trio.to_thread.run_sync(
                    self.upload_queue.begin,
                    decoded,
                    self.server_origin,
                    self.owner_id,
                )
            except OSError as error:
                raise pyfuse3.FUSEError(error.errno or errno.EIO) from error
            except Exception as error:
                raise pyfuse3.FUSEError(errno.EIO) from error
            inode = self._next_staged_inode
            self._next_staged_inode += 1
            staged = _StagedFile(inode, parent_inode, decoded, draft)
            handle = self._handle()
            readable, writable = self._access(flags)
            self._staged_handles[handle] = _StagedHandle(
                staged,
                staged.descriptor,
                readable,
                writable,
                bool(flags & os.O_APPEND),
            )
            self._staged_inodes[inode] = staged
            self._staged_names[decoded] = staged
            return pyfuse3.FileInfo(fh=handle, direct_io=True), self._attributes(inode)

    async def write(self, fh: int, off: int, buf: bytes) -> int:
        staged_handle = self._staged_handles.get(fh)
        if staged_handle is None or not staged_handle.writable:
            raise pyfuse3.FUSEError(errno.EBADF)
        staged = staged_handle.staged
        async with staged.lock:
            try:
                offset = os.fstat(staged.descriptor).st_size if staged_handle.append else off
                written = await trio.to_thread.run_sync(
                    self.upload_queue.write, staged.draft, offset, buf
                )
            except OSError as error:
                staged.failure_errno = error.errno or errno.EIO
                raise pyfuse3.FUSEError(staged.failure_errno) from error
            except ValueError as error:
                staged.failure_errno = errno.EINVAL
                raise pyfuse3.FUSEError(errno.EINVAL) from error
            except Exception as error:
                staged.failure_errno = errno.EIO
                raise pyfuse3.FUSEError(errno.EIO) from error
            if written != len(buf):
                staged.failure_errno = errno.EIO
                raise pyfuse3.FUSEError(errno.EIO)
            return written

    async def flush(self, fh: int) -> None:
        if fh in self._reads:
            return
        staged_handle = self._staged_handles.get(fh)
        if staged_handle is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        staged = staged_handle.staged
        async with staged.lock:
            if staged.failure_errno is not None:
                raise pyfuse3.FUSEError(staged.failure_errno)
            if staged.sealed is not None:
                return
            try:
                await trio.to_thread.run_sync(self.upload_queue.sync, staged.draft)
            except OSError as error:
                staged.failure_errno = error.errno or errno.EIO
                raise pyfuse3.FUSEError(staged.failure_errno) from error
            except Exception as error:
                staged.failure_errno = errno.EIO
                raise pyfuse3.FUSEError(errno.EIO) from error

    async def fsync(self, fh: int, datasync: bool) -> None:
        if fh in self._reads:
            return
        staged_handle = self._staged_handles.get(fh)
        if staged_handle is None:
            raise pyfuse3.FUSEError(errno.EBADF)
        staged = staged_handle.staged
        async with staged.lock:
            if staged.failure_errno is not None:
                raise pyfuse3.FUSEError(staged.failure_errno)
            if staged.sealed is not None:
                return
            try:
                await trio.to_thread.run_sync(
                    self.upload_queue.sync, staged.draft, datasync
                )
            except OSError as error:
                staged.failure_errno = error.errno or errno.EIO
                raise pyfuse3.FUSEError(staged.failure_errno) from error
            except Exception as error:
                staged.failure_errno = errno.EIO
                raise pyfuse3.FUSEError(errno.EIO) from error

    async def statfs(self, ctx: pyfuse3.RequestContext) -> pyfuse3.StatvfsData:
        try:
            source = os.statvfs(self.upload_queue.root)
        except OSError as error:
            raise pyfuse3.FUSEError(error.errno or errno.EIO) from error
        result = pyfuse3.StatvfsData()
        result.f_bsize = source.f_bsize
        result.f_frsize = source.f_frsize
        result.f_blocks = source.f_blocks
        result.f_bfree = source.f_bfree
        result.f_bavail = source.f_bavail
        result.f_files = source.f_files
        result.f_ffree = source.f_ffree
        result.f_favail = source.f_favail
        result.f_namemax = min(source.f_namemax, 255)
        return result

    async def unlink(
        self, parent_inode: int, name: bytes, ctx: pyfuse3.RequestContext
    ) -> None:
        parent = self._node(parent_inode)
        if isinstance(parent, CatalogFile):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if not parent.mutation_root:
            raise pyfuse3.FUSEError(errno.EROFS)
        decoded = self._name(name)
        if decoded in self._staged_names:
            raise pyfuse3.FUSEError(errno.EBUSY)
        try:
            entry = self.catalog.lookup(parent_inode, decoded)
        except Exception as error:
            raise pyfuse3.FUSEError(errno.EIO) from error
        if entry is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if isinstance(entry, CatalogDirectory):
            raise pyfuse3.FUSEError(errno.EISDIR)
        try:
            await self.library.remote_trash(self._library_entry(entry))
        except (LibraryError, PermissionError) as error:
            raise pyfuse3.FUSEError(errno.EPERM) from error
        except Exception as error:
            raise pyfuse3.FUSEError(errno.EIO) from error

    async def upload_finished(self, job_id: str) -> None:
        staged = self._staged_jobs.pop(job_id, None)
        if staged is None:
            return
        async with staged.lock:
            staged.closed = True
            if self._staged_names.get(staged.name) is staged:
                self._staged_names.pop(staged.name)
            if not staged.open_handles:
                self._staged_inodes.pop(staged.inode, None)
        try:
            await trio.to_thread.run_sync(
                pyfuse3.invalidate_entry,
                staged.parent_inode,
                staged.name.encode("utf-8"),
                staged.inode,
            )
        except (OSError, RuntimeError) as error:
            if not isinstance(error, OSError) or error.errno != errno.ENOENT:
                LOGGER.warning(
                    "could not invalidate completed upload: %s", type(error).__name__
                )

    async def setattr(
        self,
        inode: int,
        attr: pyfuse3.EntryAttributes,
        fields: pyfuse3.SetattrFields,
        fh: int | None,
        ctx: pyfuse3.RequestContext,
    ) -> pyfuse3.EntryAttributes:
        staged_handle = self._staged_handles.get(fh) if fh is not None else None
        if fh is not None and staged_handle is None:
            self._remote(inode)
            raise pyfuse3.FUSEError(errno.EROFS)
        staged = (
            staged_handle.staged
            if staged_handle is not None
            else self._staged_inodes.get(inode)
        )
        if staged is None:
            self._remote(inode)
            raise pyfuse3.FUSEError(errno.EROFS)
        if staged.sealed is not None:
            raise pyfuse3.FUSEError(errno.EROFS)
        if fields.update_uid and attr.st_uid != os.getuid():
            staged.failure_errno = errno.EPERM
            raise pyfuse3.FUSEError(errno.EPERM)
        if fields.update_gid and attr.st_gid != os.getgid():
            staged.failure_errno = errno.EPERM
            raise pyfuse3.FUSEError(errno.EPERM)
        if fields.update_size and staged_handle is not None and not staged_handle.writable:
            staged.failure_errno = errno.EBADF
            raise pyfuse3.FUSEError(errno.EBADF)

        async with staged.lock:
            try:
                if fields.update_size:
                    await trio.to_thread.run_sync(
                        self.upload_queue.truncate, staged.draft, attr.st_size
                    )
                if fields.update_atime or fields.update_mtime:
                    current = os.fstat(staged.descriptor)
                    os.utime(
                        staged.descriptor,
                        ns=(
                            attr.st_atime_ns if fields.update_atime else current.st_atime_ns,
                            attr.st_mtime_ns if fields.update_mtime else current.st_mtime_ns,
                        ),
                    )
                if fields.update_mode:
                    os.fchmod(staged.descriptor, 0o600)
            except OSError as error:
                staged.failure_errno = error.errno or errno.EIO
                raise pyfuse3.FUSEError(staged.failure_errno) from error
            except Exception as error:
                staged.failure_errno = errno.EIO
                raise pyfuse3.FUSEError(errno.EIO) from error
            return self._attributes(staged.inode)

    async def rename(
        self,
        parent_inode_old: int,
        name_old: bytes,
        parent_inode_new: int,
        name_new: bytes,
        flags: int,
        ctx: pyfuse3.RequestContext,
    ) -> None:
        if flags:
            raise pyfuse3.FUSEError(errno.EINVAL)
        old_parent = self._node(parent_inode_old)
        new_parent = self._node(parent_inode_new)
        if isinstance(old_parent, CatalogFile) or isinstance(new_parent, CatalogFile):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if (
            parent_inode_old != parent_inode_new
            or not old_parent.mutation_root
            or not new_parent.mutation_root
            or not self.library.replacement_enabled
        ):
            raise pyfuse3.FUSEError(errno.EROFS)
        old_name = self._name(name_old)
        new_name = self._name(name_new)
        async with self._namespace_lock:
            staged = self._staged_names.get(old_name)
            if staged is None:
                raise pyfuse3.FUSEError(errno.EROFS)
            if new_name in self._staged_names:
                raise pyfuse3.FUSEError(errno.EBUSY)
            try:
                target = self.catalog.lookup(parent_inode_new, new_name)
            except Exception as error:
                raise pyfuse3.FUSEError(errno.EIO) from error
            if target is None:
                raise pyfuse3.FUSEError(errno.EROFS)
            if isinstance(target, CatalogDirectory):
                raise pyfuse3.FUSEError(errno.EISDIR)
            asset = target.asset
            if (
                not asset.visible
                or asset.owner_id != self.owner_id
                or asset.library_id is not None
            ):
                raise pyfuse3.FUSEError(errno.EPERM)
            if asset.live_photo_video_id is not None:
                raise pyfuse3.FUSEError(errno.EOPNOTSUPP)
            try:
                album_ids = self.catalog.album_ids(asset.id)
            except Exception as error:
                raise pyfuse3.FUSEError(errno.EIO) from error

            async with staged.lock:
                value = staged.sealed if staged.sealed is not None else staged.draft
                assert value is not None
                try:
                    with trio.CancelScope(shield=True):
                        marked = await trio.to_thread.run_sync(
                            partial(
                                self.upload_queue.mark_replacement,
                                value.id,
                                revision=value.revision,
                                old_asset_id=asset.id,
                                old_inode=target.inode,
                                old_name=new_name,
                                source_owner_id=asset.owner_id,
                                source_library_id=asset.library_id,
                                source_checksum=asset.checksum,
                                source_updated_at=asset.updated_at,
                                source_created_ns=asset.created_ns,
                                source_is_favorite=asset.is_favorite,
                                source_visibility=asset.visibility,
                                source_album_ids=album_ids,
                            )
                        )
                        if staged.draft is not None:
                            staged.draft = replace(staged.draft, requested_name=new_name)
                        if staged.sealed is not None:
                            staged.sealed = marked
                        self._staged_names.pop(old_name)
                        staged.name = new_name
                        self._staged_names[new_name] = staged
                except UploadStateError as error:
                    raise pyfuse3.FUSEError(errno.EBUSY) from error
                except (TypeError, ValueError) as error:
                    raise pyfuse3.FUSEError(errno.EINVAL) from error
                except Exception as error:
                    raise pyfuse3.FUSEError(errno.EIO) from error

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
