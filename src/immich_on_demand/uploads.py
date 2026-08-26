from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import errno
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from typing import Self
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .model import safe_filename


_FORMAT_VERSION = 2
_MANIFEST_LIMIT = 4096
_MANIFEST = "manifest.json"
_MANIFEST_TEMP = "manifest.json.tmp"
_PAYLOAD = "payload"
_CLEANUP_PREFIX = ".cleanup-"
_ZERO_UUID = str(UUID(int=0))
_SHA1_HEX = re.compile(r"[0-9a-f]{40}")


class UploadQueueError(RuntimeError):
    pass


class UploadStateError(UploadQueueError):
    pass


class UploadState(StrEnum):
    WRITING = "writing"
    PENDING = "pending"
    ATTEMPTING = "attempting"
    REPLACING = "replacing"
    COMMITTED = "committed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class UploadErrorCode(StrEnum):
    INTERRUPTED_WRITE = "interrupted-write"
    LOCAL_WRITE_FAILED = "local-write-failed"
    UPLOAD_UNAVAILABLE = "upload-unavailable"
    UPLOAD_REJECTED = "upload-rejected"
    AMBIGUOUS_RESPONSE = "ambiguous-response"
    CANDIDATE_MISMATCH = "candidate-mismatch"
    PROFILE_MISMATCH = "profile-mismatch"
    PAYLOAD_INVALID = "payload-invalid"
    LOCAL_STATE_FAILED = "local-state-failed"


class UploadOperation(StrEnum):
    ORDINARY = "ordinary"
    REPLACEMENT = "replacement"


@dataclass(frozen=True, slots=True)
class WritableUpload:
    id: str
    requested_name: str
    descriptor: int
    payload_path: Path
    revision: int


@dataclass(frozen=True, slots=True)
class UploadStatus:
    id: str
    requested_name: str
    server_origin: str
    owner_id: str
    state: UploadState
    revision: int
    payload_path: Path
    size: int | None
    sha1: str | None
    created_ns: int | None
    modified_ns: int | None
    sealed_ns: int | None
    attempt_count: int
    next_attempt_ns: int | None
    error: UploadErrorCode | None
    candidate_asset_id: str | None
    candidate_verified: bool
    operation: UploadOperation
    old_asset_id: str | None
    old_inode: int | None
    old_name: str | None
    source_owner_id: str | None
    source_library_id: str | None
    source_checksum: str | None
    source_updated_at: str | None
    source_created_ns: int | None
    source_is_favorite: bool | None
    source_visibility: str | None
    source_album_ids: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _Manifest:
    format_version: int
    id: str
    server_origin: str
    owner_id: str
    requested_name: str
    state: str
    revision: int
    size: int | None
    sha1: str | None
    created_ns: int | None
    modified_ns: int | None
    sealed_ns: int | None
    attempt_count: int
    next_attempt_ns: int | None
    error: str | None
    candidate_asset_id: str | None
    candidate_verified: bool
    operation: str
    old_asset_id: str | None
    old_inode: int | None
    old_name: str | None
    source_owner_id: str | None
    source_library_id: str | None
    source_checksum: str | None
    source_updated_at: str | None
    source_created_ns: int | None
    source_is_favorite: bool | None
    source_visibility: str | None
    source_album_ids: list[str] | None


_V1_MANIFEST_FIELDS = frozenset(
    {
        "format_version",
        "id",
        "server_origin",
        "owner_id",
        "requested_name",
        "state",
        "revision",
        "size",
        "sha1",
        "created_ns",
        "modified_ns",
        "sealed_ns",
        "attempt_count",
        "next_attempt_ns",
        "error",
        "candidate_asset_id",
    }
)


def _canonical_uuid(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a UUID string")
    try:
        canonical = str(UUID(value))
    except ValueError as error:
        raise ValueError(f"{label} must be a UUID") from error
    if canonical != value:
        raise ValueError(f"{label} must be a canonical UUID")
    return canonical


def _canonical_origin(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("upload server origin must be a string")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("upload server origin must be a canonical HTTPS origin") from error
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(ord(character) <= 32 or ord(character) == 127 for character in parsed.netloc)
    ):
        raise ValueError("upload server origin must be a canonical HTTPS origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}{f':{port}' if port not in {None, 443} else ''}"


def _replacement_text(value: object, label: str, maximum_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum_bytes
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"replacement {label} is invalid")
    return value


def _open_flags(flags: int) -> int:
    return flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _require_directory(info: os.stat_result, message: str) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise PermissionError(message)


def _require_file(info: os.stat_result, message: str) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise PermissionError(message)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("upload manifest contains a duplicate field")
        result[key] = value
    return result


def _strict_json(data: bytes) -> object:
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("upload manifest contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise UploadQueueError("upload queue contains invalid state") from error


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


class UploadQueue:
    def __init__(self, root: Path, *, minimum_free_bytes: int = 0) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("upload queue root must be an absolute path")
        if type(minimum_free_bytes) is not int or minimum_free_bytes < 0:
            raise ValueError("upload queue free-space floor must be nonnegative")
        self.root = root
        self.minimum_free_bytes = minimum_free_bytes
        # ponytail: one lock; shard by job only if parallel upload throughput matters.
        self._lock = threading.RLock()
        self._valid_ids: set[str] = set()
        self._quarantined_names: set[str] = set()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_directory(
            os.lstat(root), "upload queue root must be owned by this user"
        )
        try:
            self._root_descriptor = os.open(
                root, _open_flags(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            )
        except OSError as error:
            raise PermissionError(
                "upload queue root must be owned by this user"
            ) from error
        try:
            _require_directory(
                os.fstat(self._root_descriptor),
                "upload queue root must be owned by this user",
            )
            try:
                fcntl.flock(
                    self._root_descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as error:
                raise UploadQueueError("upload queue is already in use") from error
            os.fsync(self._root_descriptor)
            self._recover_startup()
        except BaseException:
            os.close(self._root_descriptor)
            self._root_descriptor = -1
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @_serialized
    def close(self) -> None:
        if self._root_descriptor >= 0:
            os.close(self._root_descriptor)
            self._root_descriptor = -1

    @property
    @_serialized
    def quarantined_count(self) -> int:
        return len(self._quarantined_names)

    @_serialized
    def begin(
        self, requested_name: str, server_origin: str, owner_id: str
    ) -> WritableUpload:
        if (
            not isinstance(requested_name, str)
            or safe_filename(requested_name, _ZERO_UUID) != requested_name
            or len(requested_name.encode("utf-8")) > 255
        ):
            raise ValueError("requested upload name is not safe")
        origin = _canonical_origin(server_origin)
        owner = _canonical_uuid(owner_id, "upload owner")
        job_id = str(uuid4())
        os.mkdir(job_id, 0o700, dir_fd=self._root_descriptor)
        job_descriptor = self._open_job(job_id)
        payload_descriptor = -1
        try:
            payload_descriptor = os.open(
                _PAYLOAD,
                _open_flags(os.O_RDWR | os.O_CREAT | os.O_EXCL),
                0o600,
                dir_fd=job_descriptor,
            )
            _require_file(
                os.fstat(payload_descriptor),
                "upload payload must be an owned private regular file",
            )
            os.fsync(payload_descriptor)
            manifest = _Manifest(
                format_version=_FORMAT_VERSION,
                id=job_id,
                server_origin=origin,
                owner_id=owner,
                requested_name=requested_name,
                state=UploadState.WRITING.value,
                revision=1,
                size=None,
                sha1=None,
                created_ns=None,
                modified_ns=None,
                sealed_ns=None,
                attempt_count=0,
                next_attempt_ns=None,
                error=None,
                candidate_asset_id=None,
                candidate_verified=False,
                operation=UploadOperation.ORDINARY.value,
                old_asset_id=None,
                old_inode=None,
                old_name=None,
                source_owner_id=None,
                source_library_id=None,
                source_checksum=None,
                source_updated_at=None,
                source_created_ns=None,
                source_is_favorite=None,
                source_visibility=None,
                source_album_ids=None,
            )
            self._write_manifest(job_descriptor, manifest)
            os.fsync(self._root_descriptor)
            self._valid_ids.add(job_id)
            return WritableUpload(
                id=job_id,
                requested_name=requested_name,
                descriptor=payload_descriptor,
                payload_path=self.root / job_id / _PAYLOAD,
                revision=manifest.revision,
            )
        except BaseException:
            if payload_descriptor >= 0:
                os.close(payload_descriptor)
            raise
        finally:
            os.close(job_descriptor)

    @_serialized
    def write(self, draft: WritableUpload, offset: int, data: bytes) -> int:
        if type(offset) is not int or offset < 0:
            raise ValueError("upload write offset must be nonnegative")
        if not isinstance(data, bytes):
            raise TypeError("upload write data must be bytes")
        self._require_draft(draft)
        info = os.fstat(draft.descriptor)
        self._require_space(info.st_size, offset + len(data))
        return os.pwrite(draft.descriptor, data, offset)

    @_serialized
    def truncate(self, draft: WritableUpload, size: int) -> None:
        if type(size) is not int or size < 0:
            raise ValueError("upload size must be nonnegative")
        self._require_draft(draft)
        self._require_space(os.fstat(draft.descriptor).st_size, size)
        os.ftruncate(draft.descriptor, size)

    @_serialized
    def sync(self, draft: WritableUpload, datasync: bool = False) -> None:
        self._require_draft(draft)
        if datasync and hasattr(os, "fdatasync"):
            os.fdatasync(draft.descriptor)
        else:
            os.fsync(draft.descriptor)

    def seal(self, draft: WritableUpload) -> UploadStatus:
        with self._lock:
            manifest = self._require_draft(draft)
        os.fsync(draft.descriptor)
        info = os.fstat(draft.descriptor)
        digest = hashlib.sha1(usedforsecurity=False)
        offset = 0
        while offset < info.st_size:
            chunk = os.pread(draft.descriptor, min(1024 * 1024, info.st_size - offset), offset)
            if not chunk:
                raise UploadQueueError("upload payload changed while being sealed")
            digest.update(chunk)
            offset += len(chunk)
        with self._lock:
            if self._require_draft(draft) != manifest:
                raise UploadQueueError("upload payload changed while being sealed")
            current = os.fstat(draft.descriptor)
            if (
                current.st_size != info.st_size
                or current.st_ctime_ns != info.st_ctime_ns
                or current.st_mtime_ns != info.st_mtime_ns
            ):
                raise UploadQueueError("upload payload changed while being sealed")
            sealed_ns = time.time_ns()
            updated = replace(
                manifest,
                state=UploadState.PENDING.value,
                revision=manifest.revision + 1,
                size=info.st_size,
                sha1=digest.hexdigest(),
                created_ns=info.st_ctime_ns,
                modified_ns=info.st_mtime_ns,
                sealed_ns=sealed_ns,
                next_attempt_ns=sealed_ns,
            )
            with self._job(draft.id) as job_descriptor:
                self._write_manifest(job_descriptor, updated)
            os.fsync(self._root_descriptor)
            return self._status(updated)

    @_serialized
    def mark_replacement(
        self,
        job_id: str,
        *,
        revision: int,
        old_asset_id: str,
        old_inode: int,
        old_name: str,
        source_owner_id: str,
        source_library_id: str | None,
        source_checksum: str,
        source_updated_at: str,
        source_created_ns: int,
        source_is_favorite: bool,
        source_visibility: str,
        source_album_ids: tuple[str, ...],
    ) -> UploadStatus:
        job_id = self._require_known(job_id)
        if type(revision) is not int or revision < 1:
            raise ValueError("upload revision must be positive")
        if not isinstance(source_album_ids, tuple):
            raise TypeError("replacement album IDs must be a tuple")
        with self._job(job_id) as job_descriptor:
            manifest = self._read_manifest(job_descriptor, job_id)
            if manifest.revision != revision:
                raise UploadStateError("upload changed before replacement")
            if (
                manifest.operation != UploadOperation.ORDINARY.value
                or manifest.state
                not in {UploadState.WRITING.value, UploadState.PENDING.value}
            ):
                raise UploadStateError("upload cannot become a replacement")
            updated = replace(
                manifest,
                revision=(
                    manifest.revision
                    if manifest.state == UploadState.WRITING.value
                    else manifest.revision + 1
                ),
                requested_name=old_name,
                operation=UploadOperation.REPLACEMENT.value,
                old_asset_id=old_asset_id,
                old_inode=old_inode,
                old_name=old_name,
                source_owner_id=source_owner_id,
                source_library_id=source_library_id,
                source_checksum=source_checksum,
                source_updated_at=source_updated_at,
                source_created_ns=source_created_ns,
                source_is_favorite=source_is_favorite,
                source_visibility=source_visibility,
                source_album_ids=list(source_album_ids),
            )
            self._write_manifest(job_descriptor, updated)
            return self._status(updated)

    @_serialized
    def block_writing(
        self, draft: WritableUpload, error: UploadErrorCode
    ) -> UploadStatus:
        if not isinstance(error, UploadErrorCode):
            raise TypeError("upload error must be a fixed error code")
        manifest = self._require_draft(draft)
        updated = replace(
            manifest,
            state=UploadState.BLOCKED.value,
            revision=manifest.revision + 1,
            error=error.value,
        )
        with self._job(draft.id) as job_descriptor:
            self._write_manifest(job_descriptor, updated)
        os.fsync(self._root_descriptor)
        return self._status(updated)

    @_serialized
    def status(self, job_id: str) -> UploadStatus | None:
        job_id = _canonical_uuid(job_id, "upload job")
        if job_id not in self._valid_ids:
            return None
        try:
            with self._job(job_id) as job_descriptor:
                return self._status(self._read_manifest(job_descriptor, job_id))
        except BaseException as error:
            if not self._is_quarantinable(error):
                raise
            self._valid_ids.discard(job_id)
            self._quarantined_names.add(job_id)
            return None

    @_serialized
    def list(self) -> tuple[UploadStatus, ...]:
        jobs: list[UploadStatus] = []
        for job_id in tuple(self._valid_ids):
            job = self.status(job_id)
            if job is not None:
                jobs.append(job)
        return tuple(
            sorted(
                jobs,
                key=lambda job: (
                    job.sealed_ns is None,
                    job.sealed_ns if job.sealed_ns is not None else 0,
                    job.id,
                ),
            )
        )

    @_serialized
    def next_due(self, now_ns: int | None = None) -> UploadStatus | None:
        if now_ns is None:
            now_ns = time.time_ns()
        if type(now_ns) is not int or now_ns < 0:
            raise ValueError("upload queue time must be nonnegative")
        for job in self.list():
            if (
                job.state
                in {
                    UploadState.PENDING,
                    UploadState.ATTEMPTING,
                    UploadState.REPLACING,
                }
                and (
                    job.next_attempt_ns is None
                    or job.next_attempt_ns <= now_ns
                )
            ):
                return job
        return None

    def begin_attempt(self, job_id: str) -> UploadStatus:
        status, descriptor = self._begin_attempt(job_id)
        os.close(descriptor)
        return status

    def open_attempt(
        self, job_id: str, *, revision: int | None = None
    ) -> tuple[UploadStatus, int]:
        return self._begin_attempt(job_id, revision=revision)

    @_serialized
    def open_local(self, job_id: str) -> int:
        job_id = self._require_known(job_id)
        with self._job(job_id) as job_descriptor:
            manifest = self._read_manifest(job_descriptor, job_id)
            if manifest.state not in {
                UploadState.PENDING.value,
                UploadState.ATTEMPTING.value,
                UploadState.REPLACING.value,
                UploadState.BLOCKED.value,
            }:
                raise UploadStateError("upload payload is not locally readable")
            descriptor = self._open_payload(job_descriptor)
        try:
            self._verify_sealed_payload(descriptor, manifest)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _begin_attempt(
        self, job_id: str, *, revision: int | None = None
    ) -> tuple[UploadStatus, int]:
        descriptor = -1
        provisional: _Manifest | None = None
        try:
            with self._lock:
                job_id = self._require_known(job_id)
                with self._job(job_id) as job_descriptor:
                    manifest = self._read_manifest(job_descriptor, job_id)
                    if revision is not None and manifest.revision != revision:
                        raise UploadStateError("upload changed before attempt")
                    if manifest.state not in {
                        UploadState.PENDING.value,
                        UploadState.ATTEMPTING.value,
                    }:
                        raise UploadStateError("upload is not ready for an attempt")
                    descriptor = self._open_payload(job_descriptor)
                    provisional = replace(
                        manifest,
                        state=UploadState.ATTEMPTING.value,
                        revision=manifest.revision + 1,
                        next_attempt_ns=None,
                        error=None,
                    )
                    self._write_manifest(job_descriptor, provisional)
            self._verify_sealed_payload(descriptor, provisional)
            with self._lock, self._job(job_id) as job_descriptor:
                if self._read_manifest(job_descriptor, job_id) != provisional:
                    raise UploadStateError("upload changed during attempt admission")
                admitted = replace(
                    provisional,
                    revision=provisional.revision + 1,
                    attempt_count=provisional.attempt_count + 1,
                )
                self._write_manifest(job_descriptor, admitted)
            return self._status(admitted), descriptor
        except UploadQueueError:
            try:
                if provisional is not None:
                    with self._lock, self._job(job_id) as job_descriptor:
                        current = self._read_manifest(job_descriptor, job_id)
                        if current == provisional:
                            self._write_manifest(
                                job_descriptor,
                                replace(
                                    provisional,
                                    state=UploadState.BLOCKED.value,
                                    revision=provisional.revision + 1,
                                    next_attempt_ns=None,
                                    error=UploadErrorCode.PAYLOAD_INVALID.value,
                                ),
                            )
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            raise
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise

    @_serialized
    def record_candidate(self, job_id: str, asset_id: str) -> UploadStatus:
        job_id = self._require_known(job_id)
        candidate = _canonical_uuid(asset_id, "upload candidate")
        with self._job(job_id) as job_descriptor:
            manifest = self._read_manifest(job_descriptor, job_id)
            if manifest.state != UploadState.ATTEMPTING.value:
                raise UploadStateError("upload is not being attempted")
            if manifest.candidate_asset_id is not None:
                if manifest.candidate_asset_id == candidate:
                    return self._status(manifest)
                raise UploadStateError("upload already has a different candidate")
            updated = replace(
                manifest,
                revision=manifest.revision + 1,
                candidate_asset_id=candidate,
            )
            self._write_manifest(job_descriptor, updated)
            return self._status(updated)

    @_serialized
    def begin_replacing(self, job_id: str) -> UploadStatus:
        job_id = self._require_known(job_id)
        with self._job(job_id) as job_descriptor:
            manifest = self._read_manifest(job_descriptor, job_id)
            if manifest.operation != UploadOperation.REPLACEMENT.value:
                raise UploadStateError("upload is not a replacement")
            if manifest.state == UploadState.REPLACING.value:
                return self._status(manifest)
            if (
                manifest.state != UploadState.ATTEMPTING.value
                or manifest.candidate_asset_id is None
            ):
                raise UploadStateError("replacement has no verified candidate")
            updated = replace(
                manifest,
                state=UploadState.REPLACING.value,
                revision=manifest.revision + 1,
                next_attempt_ns=None,
                error=None,
                candidate_verified=True,
            )
            self._write_manifest(job_descriptor, updated)
            return self._status(updated)

    @_serialized
    def retry(
        self,
        job_id: str,
        *,
        at_ns: int | None = None,
        error: UploadErrorCode | None = None,
        revision: int | None = None,
    ) -> UploadStatus:
        job_id = self._require_known(job_id)
        if at_ns is None:
            at_ns = time.time_ns()
        if type(at_ns) is not int or at_ns < 0:
            raise ValueError("upload retry time must be nonnegative")
        if error is not None and not isinstance(error, UploadErrorCode):
            raise TypeError("upload error must be a fixed error code")
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("upload revision must be nonnegative")
        with self._job(job_id) as job_descriptor:
            manifest = self._read_manifest(job_descriptor, job_id)
            if revision is not None and manifest.revision != revision:
                raise UploadStateError("upload changed before retry")
            if revision is not None and manifest.state not in {
                UploadState.PENDING.value,
                UploadState.BLOCKED.value,
            }:
                raise UploadStateError("upload is already being attempted")
            if manifest.state not in {
                UploadState.PENDING.value,
                UploadState.ATTEMPTING.value,
                UploadState.REPLACING.value,
                UploadState.COMMITTED.value,
                UploadState.BLOCKED.value,
            }:
                raise UploadStateError("upload cannot be retried")
            if manifest.sha1 is None:
                raise UploadStateError(
                    "incomplete upload recovery cannot be retried"
                )
            if (
                manifest.operation == UploadOperation.REPLACEMENT.value
                and manifest.candidate_verified
            ):
                state = UploadState.REPLACING.value
            elif manifest.candidate_asset_id is not None:
                state = UploadState.ATTEMPTING.value
            else:
                state = UploadState.PENDING.value
            updated = replace(
                manifest,
                state=state,
                revision=manifest.revision + 1,
                next_attempt_ns=at_ns,
                error=error.value if error is not None else None,
            )
            self._write_manifest(job_descriptor, updated)
            return self._status(updated)

    @_serialized
    def block(self, job_id: str, error: UploadErrorCode) -> UploadStatus:
        job_id = self._require_known(job_id)
        if not isinstance(error, UploadErrorCode):
            raise TypeError("upload error must be a fixed error code")
        with self._job(job_id) as job_descriptor:
            manifest = self._read_manifest(job_descriptor, job_id)
            if manifest.state not in {
                UploadState.PENDING.value,
                UploadState.ATTEMPTING.value,
                UploadState.REPLACING.value,
                UploadState.COMMITTED.value,
                UploadState.BLOCKED.value,
            }:
                raise UploadStateError("upload cannot be blocked")
            updated = replace(
                manifest,
                state=UploadState.BLOCKED.value,
                revision=manifest.revision + 1,
                next_attempt_ns=None,
                error=error.value,
            )
            self._write_manifest(job_descriptor, updated)
            return self._status(updated)

    @_serialized
    def commit(self, job_id: str) -> UploadStatus:
        job_id = self._require_known(job_id)
        with self._job(job_id) as job_descriptor:
            manifest = self._read_manifest(job_descriptor, job_id)
            if (
                manifest.operation == UploadOperation.REPLACEMENT.value
                and manifest.state != UploadState.REPLACING.value
            ):
                raise UploadStateError("replacement has not entered replacing")
            if manifest.candidate_asset_id is None or manifest.state not in {
                UploadState.ATTEMPTING.value,
                UploadState.REPLACING.value,
            }:
                raise UploadStateError("upload has no verified candidate")
            updated = replace(
                manifest,
                state=UploadState.COMMITTED.value,
                revision=manifest.revision + 1,
                next_attempt_ns=None,
                error=None,
            )
            self._write_manifest(job_descriptor, updated)
            return self._status(updated)

    @_serialized
    def cancel(
        self, job_id: str, *, requested_name: str, revision: int
    ) -> None:
        job_id = self._require_known(job_id)
        with self._job(job_id) as job_descriptor:
            manifest = self._read_manifest(job_descriptor, job_id)
            if (
                not isinstance(requested_name, str)
                or type(revision) is not int
                or manifest.requested_name != requested_name
                or manifest.revision != revision
            ):
                raise ValueError("upload cancellation confirmation does not match")
            if manifest.state not in {
                UploadState.PENDING.value,
                UploadState.BLOCKED.value,
            }:
                raise UploadStateError("upload cannot be cancelled")
            safely_rejected = (
                manifest.error == UploadErrorCode.UPLOAD_REJECTED.value
            )
            if manifest.candidate_asset_id is not None or (
                manifest.attempt_count > 0 and not safely_rejected
            ):
                raise UploadStateError("upload may already exist remotely")
            self._write_manifest(
                job_descriptor,
                replace(
                    manifest,
                    state=UploadState.CANCELLED.value,
                    revision=manifest.revision + 1,
                    next_attempt_ns=None,
                    error=None,
                ),
            )
        self._cleanup_job(job_id, {UploadState.CANCELLED})

    @_serialized
    def remove(self, job_id: str) -> None:
        self._require_known(job_id)
        self._cleanup_job(job_id, {UploadState.COMMITTED})

    def _require_known(self, job_id: str) -> str:
        job_id = _canonical_uuid(job_id, "upload job")
        if job_id not in self._valid_ids:
            raise UploadStateError("upload is not available")
        return job_id

    def _require_draft(self, draft: WritableUpload) -> _Manifest:
        if not isinstance(draft, WritableUpload):
            raise TypeError("upload draft is invalid")
        with self._job(draft.id) as job_descriptor:
            manifest = self._read_manifest(job_descriptor, draft.id)
            payload = self._open_payload(job_descriptor)
            try:
                live = os.fstat(draft.descriptor)
                stored = os.fstat(payload)
            finally:
                os.close(payload)
        if (
            manifest.state != UploadState.WRITING.value
            or manifest.revision != draft.revision
            or (
                manifest.requested_name != draft.requested_name
                and not (
                    manifest.operation == UploadOperation.REPLACEMENT.value
                    and manifest.requested_name == manifest.old_name
                )
            )
            or live.st_dev != stored.st_dev
            or live.st_ino != stored.st_ino
        ):
            raise UploadStateError("upload draft is no longer writable")
        _require_file(live, "upload payload must be an owned private regular file")
        return manifest

    def _require_space(self, current_size: int, new_end: int) -> None:
        growth = max(0, new_end - current_size)
        if (
            growth
            and shutil.disk_usage(self.root).free - growth
            < self.minimum_free_bytes
        ):
            raise OSError(errno.ENOSPC, "upload queue storage is full")

    def _status(self, manifest: _Manifest) -> UploadStatus:
        return UploadStatus(
            id=manifest.id,
            requested_name=manifest.requested_name,
            server_origin=manifest.server_origin,
            owner_id=manifest.owner_id,
            state=UploadState(manifest.state),
            revision=manifest.revision,
            payload_path=self.root / manifest.id / _PAYLOAD,
            size=manifest.size,
            sha1=manifest.sha1,
            created_ns=manifest.created_ns,
            modified_ns=manifest.modified_ns,
            sealed_ns=manifest.sealed_ns,
            attempt_count=manifest.attempt_count,
            next_attempt_ns=manifest.next_attempt_ns,
            error=UploadErrorCode(manifest.error) if manifest.error is not None else None,
            candidate_asset_id=manifest.candidate_asset_id,
            candidate_verified=manifest.candidate_verified,
            operation=UploadOperation(manifest.operation),
            old_asset_id=manifest.old_asset_id,
            old_inode=manifest.old_inode,
            old_name=manifest.old_name,
            source_owner_id=manifest.source_owner_id,
            source_library_id=manifest.source_library_id,
            source_checksum=manifest.source_checksum,
            source_updated_at=manifest.source_updated_at,
            source_created_ns=manifest.source_created_ns,
            source_is_favorite=manifest.source_is_favorite,
            source_visibility=manifest.source_visibility,
            source_album_ids=(
                tuple(manifest.source_album_ids)
                if manifest.source_album_ids is not None
                else None
            ),
        )

    def _open_job(self, job_id: str) -> int:
        descriptor = os.open(
            job_id,
            _open_flags(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)),
            dir_fd=self._root_descriptor,
        )
        try:
            _require_directory(
                os.fstat(descriptor),
                "upload job must be an owned private directory",
            )
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @contextmanager
    def _job(self, job_id: str) -> Iterator[int]:
        descriptor = self._open_job(job_id)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def _open_payload(self, job_descriptor: int) -> int:
        descriptor = os.open(_PAYLOAD, _open_flags(os.O_RDONLY), dir_fd=job_descriptor)
        try:
            _require_file(
                os.fstat(descriptor),
                "upload payload must be an owned private regular file",
            )
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _verify_sealed_payload(
        self, descriptor: int, manifest: _Manifest
    ) -> None:
        info = os.fstat(descriptor)
        if (
            manifest.size is None
            or manifest.sha1 is None
            or manifest.created_ns is None
            or manifest.modified_ns is None
            or info.st_size != manifest.size
            or info.st_ctime_ns != manifest.created_ns
            or info.st_mtime_ns != manifest.modified_ns
        ):
            raise UploadQueueError("upload payload is invalid")
        digest = hashlib.sha1(usedforsecurity=False)
        offset = 0
        while offset < info.st_size:
            chunk = os.pread(
                descriptor, min(1024 * 1024, info.st_size - offset), offset
            )
            if not chunk:
                raise UploadQueueError("upload payload is invalid")
            digest.update(chunk)
            offset += len(chunk)
        if digest.hexdigest() != manifest.sha1:
            raise UploadQueueError("upload payload is invalid")

    def _cleanup_job(
        self, job_id: str, allowed_states: set[UploadState]
    ) -> None:
        job_id = _canonical_uuid(job_id, "upload job")
        tombstone = f"{_CLEANUP_PREFIX}{job_id}"
        try:
            os.stat(
                tombstone,
                dir_fd=self._root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise UploadQueueError("upload queue contains unsafe state")
        with self._job(job_id) as job_descriptor:
            manifest = self._read_manifest(job_descriptor, job_id)
            if UploadState(manifest.state) not in allowed_states:
                raise UploadStateError("upload cannot be removed")
            entries = set(os.listdir(job_descriptor))
            if entries != {_PAYLOAD, _MANIFEST}:
                raise UploadQueueError("upload queue contains unsafe state")
            payload = self._open_payload(job_descriptor)
            os.close(payload)
            os.rename(
                _MANIFEST,
                tombstone,
                src_dir_fd=job_descriptor,
                dst_dir_fd=self._root_descriptor,
            )
            self._valid_ids.discard(job_id)
            os.fsync(self._root_descriptor)
            os.unlink(_PAYLOAD, dir_fd=job_descriptor)
            os.fsync(job_descriptor)
        os.rmdir(job_id, dir_fd=self._root_descriptor)
        os.fsync(self._root_descriptor)
        self._require_cleanup_marker(tombstone, job_id, allowed_states)
        os.unlink(tombstone, dir_fd=self._root_descriptor)
        os.fsync(self._root_descriptor)

    def _require_cleanup_marker(
        self, name: str, job_id: str, allowed_states: set[UploadState]
    ) -> _Manifest:
        manifest = self._read_manifest_file(
            self._root_descriptor, name, job_id
        )
        if UploadState(manifest.state) not in allowed_states:
            raise UploadQueueError("upload queue contains unsafe state")
        return manifest

    def _finish_cleanup_marker(self, name: str, job_id: str) -> None:
        self._require_cleanup_marker(
            name, job_id, {UploadState.CANCELLED, UploadState.COMMITTED}
        )
        try:
            job_descriptor = self._open_job(job_id)
        except FileNotFoundError:
            job_descriptor = None
        if job_descriptor is not None:
            try:
                entries = set(os.listdir(job_descriptor))
                if entries - {_PAYLOAD}:
                    raise UploadQueueError("upload queue contains unsafe state")
                if _PAYLOAD in entries:
                    payload = self._open_payload(job_descriptor)
                    os.close(payload)
                    os.unlink(_PAYLOAD, dir_fd=job_descriptor)
                    os.fsync(job_descriptor)
            finally:
                os.close(job_descriptor)
            os.rmdir(job_id, dir_fd=self._root_descriptor)
            os.fsync(self._root_descriptor)
        os.unlink(name, dir_fd=self._root_descriptor)
        os.fsync(self._root_descriptor)

    def _read_manifest(self, job_descriptor: int, job_id: str) -> _Manifest:
        return self._read_manifest_file(job_descriptor, _MANIFEST, job_id)

    def _read_manifest_file(
        self, directory_descriptor: int, name: str, job_id: str
    ) -> _Manifest:
        descriptor = os.open(name, _open_flags(os.O_RDONLY), dir_fd=directory_descriptor)
        try:
            info = os.fstat(descriptor)
            _require_file(
                info, "upload manifest must be an owned private regular file"
            )
            if info.st_size > _MANIFEST_LIMIT:
                raise UploadQueueError("upload queue contains invalid state")
            data = os.read(descriptor, _MANIFEST_LIMIT + 1)
        finally:
            os.close(descriptor)
        value = _strict_json(data)
        if (
            isinstance(value, dict)
            and type(value.get("format_version")) is int
            and value.get("format_version") == 1
            and set(value) == _V1_MANIFEST_FIELDS
        ):
            value = {
                **value,
                "format_version": _FORMAT_VERSION,
                "candidate_verified": False,
                "operation": UploadOperation.ORDINARY.value,
                "old_asset_id": None,
                "old_inode": None,
                "old_name": None,
                "source_owner_id": None,
                "source_library_id": None,
                "source_checksum": None,
                "source_updated_at": None,
                "source_created_ns": None,
                "source_is_favorite": None,
                "source_visibility": None,
                "source_album_ids": None,
            }
        if not isinstance(value, dict) or set(value) != set(_Manifest.__dataclass_fields__):
            raise UploadQueueError("upload queue contains invalid state")
        try:
            manifest = _Manifest(**value)  # type: ignore[arg-type]
            self._validate_manifest(manifest, job_id)
        except (TypeError, ValueError) as error:
            raise UploadQueueError("upload queue contains invalid state") from error
        return manifest

    def _write_manifest(self, job_descriptor: int, manifest: _Manifest) -> None:
        self._validate_manifest(manifest, manifest.id)
        data = json.dumps(asdict(manifest), separators=(",", ":"), sort_keys=True).encode()
        if len(data) > _MANIFEST_LIMIT:
            raise UploadQueueError("upload manifest is too large")
        descriptor = os.open(
            _MANIFEST_TEMP,
            _open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
            dir_fd=job_descriptor,
        )
        try:
            _require_file(
                os.fstat(descriptor),
                "upload manifest must be an owned private regular file",
            )
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        else:
            os.close(descriptor)
        os.replace(
            _MANIFEST_TEMP,
            _MANIFEST,
            src_dir_fd=job_descriptor,
            dst_dir_fd=job_descriptor,
        )
        os.fsync(job_descriptor)

    def _validate_manifest(self, manifest: _Manifest, job_id: str) -> None:
        if (
            type(manifest.format_version) is not int
            or manifest.format_version != _FORMAT_VERSION
        ):
            raise ValueError("invalid format")
        if _canonical_uuid(manifest.id, "upload job") != job_id:
            raise ValueError("wrong job")
        if _canonical_origin(manifest.server_origin) != manifest.server_origin:
            raise ValueError("noncanonical origin")
        _canonical_uuid(manifest.owner_id, "upload owner")
        if (
            not isinstance(manifest.requested_name, str)
            or safe_filename(manifest.requested_name, _ZERO_UUID)
            != manifest.requested_name
            or len(manifest.requested_name.encode()) > 255
        ):
            raise ValueError("unsafe name")
        state = UploadState(manifest.state)
        operation = UploadOperation(manifest.operation)
        if type(manifest.candidate_verified) is not bool:
            raise ValueError("candidate verification state is invalid")
        if manifest.candidate_verified and (
            operation is not UploadOperation.REPLACEMENT
            or manifest.candidate_asset_id is None
        ):
            raise ValueError("candidate verification state is invalid")
        source_fields = (
            manifest.old_asset_id,
            manifest.old_inode,
            manifest.old_name,
            manifest.source_owner_id,
            manifest.source_library_id,
            manifest.source_checksum,
            manifest.source_updated_at,
            manifest.source_created_ns,
            manifest.source_is_favorite,
            manifest.source_visibility,
            manifest.source_album_ids,
        )
        if operation is UploadOperation.ORDINARY:
            if any(value is not None for value in source_fields):
                raise ValueError("ordinary upload has replacement metadata")
        else:
            if not isinstance(manifest.old_asset_id, str):
                raise ValueError("replacement source asset is invalid")
            _canonical_uuid(manifest.old_asset_id, "replacement source asset")
            if type(manifest.old_inode) is not int or manifest.old_inode < 1:
                raise ValueError("replacement source inode is invalid")
            if (
                not isinstance(manifest.old_name, str)
                or safe_filename(manifest.old_name, _ZERO_UUID)
                != manifest.old_name
                or len(manifest.old_name.encode("utf-8")) > 255
            ):
                raise ValueError("replacement source name is unsafe")
            if not isinstance(manifest.source_owner_id, str):
                raise ValueError("replacement source owner is invalid")
            _canonical_uuid(manifest.source_owner_id, "replacement source owner")
            if manifest.source_owner_id != manifest.owner_id:
                raise ValueError("replacement source owner does not match upload")
            if manifest.source_library_id is not None:
                _canonical_uuid(
                    manifest.source_library_id,
                    "replacement source library",
                )
            _replacement_text(manifest.source_checksum, "source checksum", 1024)
            _replacement_text(manifest.source_updated_at, "source updated time", 128)
            if type(manifest.source_created_ns) is not int or manifest.source_created_ns < 0:
                raise ValueError("replacement source creation time is invalid")
            if type(manifest.source_is_favorite) is not bool:
                raise ValueError("replacement favorite state is invalid")
            _replacement_text(manifest.source_visibility, "source visibility", 128)
            if not isinstance(manifest.source_album_ids, list):
                raise ValueError("replacement album IDs must be a list")
            albums = [
                _canonical_uuid(album_id, "replacement album")
                for album_id in manifest.source_album_ids
            ]
            if albums != sorted(set(albums)):
                raise ValueError("replacement album IDs must be sorted and unique")
        if (
            state is UploadState.REPLACING
            and operation is not UploadOperation.REPLACEMENT
        ):
            raise ValueError("ordinary upload cannot be replacing")
        if type(manifest.revision) is not int or manifest.revision < 1:
            raise ValueError("invalid revision")
        for value in (
            manifest.size,
            manifest.created_ns,
            manifest.modified_ns,
            manifest.sealed_ns,
            manifest.next_attempt_ns,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid integer")
        if type(manifest.attempt_count) is not int or manifest.attempt_count < 0:
            raise ValueError("invalid attempts")
        if manifest.sha1 is not None and (
            not isinstance(manifest.sha1, str)
            or _SHA1_HEX.fullmatch(manifest.sha1) is None
        ):
            raise ValueError("invalid hash")
        if manifest.error is not None:
            UploadErrorCode(manifest.error)
        if manifest.candidate_asset_id is not None:
            _canonical_uuid(manifest.candidate_asset_id, "upload candidate")
        sealed = (
            manifest.size,
            manifest.sha1,
            manifest.created_ns,
            manifest.modified_ns,
            manifest.sealed_ns,
        )
        if state is UploadState.WRITING:
            if (
                any(value is not None for value in sealed)
                or manifest.attempt_count != 0
                or manifest.next_attempt_ns is not None
                or manifest.error is not None
                or manifest.candidate_asset_id is not None
                or manifest.candidate_verified
            ):
                raise ValueError("writing upload is already sealed")
        elif state is UploadState.PENDING:
            if (
                any(value is None for value in sealed)
                or manifest.next_attempt_ns is None
                or manifest.candidate_asset_id is not None
                or manifest.candidate_verified
            ):
                raise ValueError("pending upload state is invalid")
        elif state is UploadState.ATTEMPTING:
            if any(value is None for value in sealed) or (
                manifest.candidate_asset_id is not None
                and manifest.attempt_count < 1
            ) or manifest.candidate_verified:
                raise ValueError("attempting upload state is invalid")
        elif state is UploadState.REPLACING:
            if (
                any(value is None for value in sealed)
                or manifest.attempt_count < 1
                or manifest.candidate_asset_id is None
                or not manifest.candidate_verified
            ):
                raise ValueError("replacing upload state is invalid")
        elif state is UploadState.COMMITTED:
            if (
                any(value is None for value in sealed)
                or manifest.attempt_count < 1
                or manifest.next_attempt_ns is not None
                or manifest.error is not None
                or manifest.candidate_asset_id is None
                or (
                    operation is UploadOperation.REPLACEMENT
                    and not manifest.candidate_verified
                )
            ):
                raise ValueError("committed upload state is invalid")
        elif state is UploadState.BLOCKED:
            if (
                manifest.error is None
                or manifest.next_attempt_ns is not None
                or not (
                    all(value is None for value in sealed)
                    or all(value is not None for value in sealed)
                )
                or (
                    all(value is None for value in sealed)
                    and (
                        manifest.attempt_count != 0
                        or manifest.candidate_asset_id is not None
                    )
                )
            ):
                raise ValueError("blocked upload metadata is incomplete")
        elif state is UploadState.CANCELLED and (
            manifest.error is not None
            or manifest.next_attempt_ns is not None
            or manifest.candidate_asset_id is not None
            or manifest.candidate_verified
            or not (
                all(value is None for value in sealed)
                or all(value is not None for value in sealed)
            )
        ):
            raise ValueError("cancelled upload state is invalid")

    def _recover_startup(self) -> None:
        names = os.listdir(self._root_descriptor)
        for name in names:
            if name.startswith(_CLEANUP_PREFIX):
                try:
                    job_id = _canonical_uuid(
                        name.removeprefix(_CLEANUP_PREFIX), "upload job"
                    )
                    self._finish_cleanup_marker(name, job_id)
                except BaseException as error:
                    if not self._is_quarantinable(error):
                        raise
                    self._quarantined_names.add(name)
        cleanup: list[tuple[str, UploadState]] = []
        for name in os.listdir(self._root_descriptor):
            if name.startswith(_CLEANUP_PREFIX):
                continue
            try:
                job_id = _canonical_uuid(name, "upload job")
                with self._job(job_id) as job_descriptor:
                    entries = set(os.listdir(job_descriptor))
                    if entries - {_PAYLOAD, _MANIFEST, _MANIFEST_TEMP}:
                        raise UploadQueueError("upload queue contains unsafe state")
                    if _MANIFEST_TEMP in entries:
                        temp = os.open(
                            _MANIFEST_TEMP,
                            _open_flags(os.O_RDONLY),
                            dir_fd=job_descriptor,
                        )
                        try:
                            _require_file(
                                os.fstat(temp),
                                "upload manifest must be an owned private regular file",
                            )
                        finally:
                            os.close(temp)
                        raise UploadQueueError(
                            "upload queue contains unsafe state"
                        )
                    manifest = self._read_manifest(job_descriptor, job_id)
                    payload = self._open_payload(job_descriptor)
                    os.close(payload)
                    if manifest.state == UploadState.WRITING.value:
                        self._write_manifest(
                            job_descriptor,
                            replace(
                                manifest,
                                state=UploadState.BLOCKED.value,
                                revision=manifest.revision + 1,
                                error=UploadErrorCode.INTERRUPTED_WRITE.value,
                            ),
                        )
                    elif manifest.state in {
                        UploadState.CANCELLED.value,
                        UploadState.COMMITTED.value,
                    }:
                        cleanup.append((job_id, UploadState(manifest.state)))
                        continue
                    self._valid_ids.add(job_id)
            except BaseException as error:
                if not self._is_quarantinable(error):
                    raise
                self._quarantined_names.add(name)
        for job_id, state in cleanup:
            try:
                self._valid_ids.add(job_id)
                self._cleanup_job(job_id, {state})
            except BaseException as error:
                if not self._is_quarantinable(error):
                    raise
                self._valid_ids.discard(job_id)
                self._quarantined_names.add(job_id)

    @staticmethod
    def _is_quarantinable(error: BaseException) -> bool:
        if isinstance(
            error,
            (
                UploadQueueError,
                PermissionError,
                FileNotFoundError,
                NotADirectoryError,
                TypeError,
                ValueError,
            ),
        ):
            return True
        return isinstance(error, OSError) and error.errno in {
            errno.EACCES,
            errno.ELOOP,
            errno.ENOENT,
            errno.ENOTDIR,
        }
