from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
import base64
from functools import partial
import hashlib
import hmac
import logging
import os
from pathlib import Path
import stat
import time
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit
from uuid import UUID

import pyfuse3
import trio

from .app import FullRefreshRequired, refresh_catalog, refresh_catalog_incremental
from .catalog import Catalog, CatalogAsset, TrustedProfile
from .content_cache import ContentCache
from .control import serve_control
from .filesystem import ImmichFilesystem
from .immich import (
    ImmichClient,
    ImmichError,
    ImmichPageLimitError,
    ImmichResponseError,
    ImmichRetryableError,
    ImmichUnavailableError,
    MUTATION_PERMISSIONS,
    READ_PERMISSIONS,
    ServerSession,
    UPLOAD_PERMISSIONS,
)
from .library import Library
from .model import Asset
from .previewer import populate_previews
from .settings import Settings, cache_path, data_path, load_api_key, runtime_path, state_path
from .thumbnails import prepare_thumbnail_cache
from .uploads import UploadErrorCode, UploadQueue, UploadQueueError, UploadState, UploadStatus


LOGGER = logging.getLogger(__name__)
FULL_REFRESH_SECONDS = 24 * 60 * 60
OFFLINE_RETRY_DELAYS = (5, 10, 20, 40, 60)
UPLOAD_RETRY_DELAYS = (5, 10, 20, 40, 60)


class _PreviewSuppressionError(OSError):
    pass


class _RestoreJob:
    __slots__ = ("asset_id", "done", "failed")

    def __init__(self, asset_id: str) -> None:
        self.asset_id = asset_id
        self.done = trio.Event()
        self.failed = False


async def _queue_call(
    function: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    return await trio.to_thread.run_sync(partial(function, *args, **kwargs))


def _check_mountpoint(path: Path) -> None:
    info = path.stat(follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        raise PermissionError(f"refusing symlinked mountpoint: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(path)
    if info.st_uid != os.getuid():
        raise PermissionError(f"mountpoint is not owned by this user: {path}")
    with os.scandir(path) as entries:
        if next(entries, None) is not None:
            raise OSError(f"mountpoint is not empty: {path}")


def _prepare_mountpoint(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    _check_mountpoint(path)


def _prepare_cache_root(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("cache root must be a directory owned by this user")
    os.chmod(path, 0o700)


def _evict_to_limits(content_cache: ContentCache, settings: Settings) -> list[str]:
    return content_cache.evict_to_limits(
        max_age_seconds=settings.cache_max_age_seconds,
        max_bytes=settings.cache_max_bytes,
        minimum_free_bytes=settings.minimum_free_bytes,
    )


async def _refresh_loop(
    catalog: Catalog,
    library: Library,
    read_client: ImmichClient,
    read_session: ServerSession,
    trusted_profile: TrustedProfile,
    catalog_lock: trio.Lock,
    content_cache: ContentCache,
    settings: Settings,
    mount_ready: trio.Event,
    requests: trio.MemoryReceiveChannel[bool],
    full_requested: list[bool],
    fatal_errors: list[str],
) -> None:
    async for force_full in requests:
        force_full = force_full or full_requested[0]
        full_requested[0] = False
        try:
            if force_full:
                await refresh_catalog(
                    catalog,
                    read_client,
                    read_session,
                    catalog_lock,
                    trusted_profile=trusted_profile,
                )
            else:
                await refresh_catalog_incremental(
                    catalog,
                    read_client,
                    read_session,
                    catalog_lock,
                    refresh_seconds=settings.refresh_seconds,
                )
        except (FullRefreshRequired, ImmichPageLimitError, ImmichResponseError):
            try:
                await refresh_catalog(
                    catalog,
                    read_client,
                    read_session,
                    catalog_lock,
                    trusted_profile=trusted_profile,
                )
            except Exception as error:
                LOGGER.warning("background full refresh failed: %s", error)
                continue
        except Exception as error:
            LOGGER.warning("background refresh failed: %s", error)
            continue
        try:
            _evict_to_limits(content_cache, settings)
        except Exception as error:
            LOGGER.warning("background cache eviction failed: %s", error)
        try:
            # Keep this as the next Trio checkpoint: populate_previews installs every
            # failure record synchronously before it starts any network work.
            await populate_previews(
                library.list(),
                read_client,
                settings.mount_path,
                mount_ready=mount_ready,
            )
        except Exception as error:
            LOGGER.error("preview suppression failed; terminating mount: %s", error)
            fatal_errors.append("preview suppression failed; mount terminated")
            pyfuse3.terminate()
            return


async def _refresh_worker(
    catalog: Catalog,
    library: Library,
    read_client: ImmichClient,
    read_session: ServerSession,
    trusted_profile: TrustedProfile,
    catalog_lock: trio.Lock,
    content_cache: ContentCache,
    settings: Settings,
    mount_ready: trio.Event,
    initial_entries: list[CatalogAsset],
    requests: trio.MemoryReceiveChannel[bool],
    full_requested: list[bool],
    fatal_errors: list[str],
    *,
    task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
) -> None:
    await populate_previews(
        initial_entries,
        read_client,
        settings.mount_path,
        mount_ready=mount_ready,
        task_status=task_status,
    )
    await _refresh_loop(
        catalog,
        library,
        read_client,
        read_session,
        trusted_profile,
        catalog_lock,
        content_cache,
        settings,
        mount_ready,
        requests,
        full_requested,
        fatal_errors,
    )


async def _periodic_refresh(
    requests: trio.MemorySendChannel[bool],
    interval: int,
    force_full: bool,
    full_requested: list[bool],
    online: list[bool],
) -> None:
    while True:
        await trio.sleep(interval)
        if not online[0]:
            continue
        if force_full:
            full_requested[0] = True
        try:
            requests.send_nowait(force_full)
        except trio.WouldBlock:
            pass


async def _offline_retries(
    requests: trio.MemorySendChannel[bool], online: list[bool]
) -> None:
    index = 0
    while not online[0]:
        await trio.sleep(OFFLINE_RETRY_DELAYS[index])
        if online[0]:
            return
        try:
            requests.send_nowait(True)
        except trio.WouldBlock:
            pass
        index = min(index + 1, len(OFFLINE_RETRY_DELAYS) - 1)


async def _pin_worker(
    catalog: Catalog,
    content_cache: ContentCache,
    notifications: trio.MemoryReceiveChannel[bool],
    pending: dict[str, CatalogAsset],
    inflight: set[str],
) -> None:
    async for _ in notifications:
        while pending:
            asset_id, entry = pending.popitem()
            if asset_id not in catalog.pinned_ids():
                continue
            inflight.add(asset_id)
            try:
                await content_cache.hydrate(entry.asset)
            except Exception as error:
                LOGGER.warning("pin hydration failed: %s", type(error).__name__)
            finally:
                inflight.discard(asset_id)


def _upload_retry_at(attempt_count: int) -> int:
    delay = UPLOAD_RETRY_DELAYS[
        min(max(attempt_count, 1) - 1, len(UPLOAD_RETRY_DELAYS) - 1)
    ]
    return time.time_ns() + delay * 1_000_000_000


def _upload_matches(job: UploadStatus, asset: Asset, marker: str) -> bool:
    try:
        checksum = base64.b64decode(asset.checksum, validate=True).hex()
    except (ValueError, TypeError):
        return False
    return (
        marker == job.id
        and asset.visible
        and asset.owner_id == job.owner_id
        and asset.library_id is None
        and asset.size == job.size
        and job.sha1 is not None
        and hmac.compare_digest(checksum, job.sha1)
    )


async def _process_upload(
    queue: UploadQueue,
    catalog: Catalog,
    catalog_lock: trio.Lock,
    library: Library,
    read_client: ImmichClient,
    settings: Settings,
    job: UploadStatus,
    on_uploaded: Callable[[CatalogAsset], Awaitable[None]],
) -> None:
    if job.server_origin != settings.server_origin:
        await _queue_call(queue.block, job.id, UploadErrorCode.PROFILE_MISMATCH)
        return
    if not library.mutation_enabled:
        return
    mutation, session = library.upload_access()
    if session.owner_id != job.owner_id:
        await _queue_call(queue.block, job.id, UploadErrorCode.PROFILE_MISMATCH)
        return

    current = job
    try:
        current, descriptor = await _queue_call(queue.open_attempt, current.id)
        try:
            result = (
                await mutation.upload(
                    descriptor,
                    current.requested_name,
                    session.media_types,
                    current.id,
                )
                if current.candidate_asset_id is None
                else None
            )
        finally:
            os.close(descriptor)
        if result is not None:
            current = await _queue_call(
                queue.record_candidate, current.id, result.asset_id
            )

        assert current.candidate_asset_id is not None
        asset = await read_client.asset(current.candidate_asset_id)
        marker = await read_client.asset_metadata(current.candidate_asset_id)
        if not _upload_matches(current, asset, marker):
            await _queue_call(
                queue.block, current.id, UploadErrorCode.CANDIDATE_MISMATCH
            )
            return

        async with catalog_lock:
            entry = catalog.add_uploaded(asset, current.requested_name)
            await on_uploaded(entry)
        await _queue_call(queue.commit, current.id)
    except (ImmichUnavailableError, ImmichRetryableError):
        await _queue_call(
            queue.retry,
            current.id,
            at_ns=_upload_retry_at(current.attempt_count),
            error=UploadErrorCode.UPLOAD_UNAVAILABLE,
        )
    except ImmichResponseError:
        if current.candidate_asset_id is None:
            await _queue_call(
                queue.retry,
                current.id,
                at_ns=_upload_retry_at(current.attempt_count),
                error=UploadErrorCode.AMBIGUOUS_RESPONSE,
            )
        else:
            await _queue_call(
                queue.block, current.id, UploadErrorCode.CANDIDATE_MISMATCH
            )
    except ImmichError:
        await _queue_call(queue.block, current.id, UploadErrorCode.UPLOAD_REJECTED)
    except UploadQueueError as error:
        LOGGER.warning("upload queue rejected a job: %s", type(error).__name__)
    except _PreviewSuppressionError:
        await _queue_call(queue.block, current.id, UploadErrorCode.LOCAL_STATE_FAILED)
        raise
    except Exception as error:
        await _queue_call(queue.block, current.id, UploadErrorCode.LOCAL_STATE_FAILED)
        LOGGER.warning("upload job was blocked: %s", type(error).__name__)
    else:
        try:
            await _queue_call(queue.remove, current.id)
        except Exception as error:
            LOGGER.warning(
                "could not remove committed upload job: %s", type(error).__name__
            )


def _next_upload_delay(queue: UploadQueue) -> float | None:
    now = time.time_ns()
    due = [
        job.next_attempt_ns
        for job in queue.list()
        if job.state in {UploadState.PENDING, UploadState.ATTEMPTING}
        and job.next_attempt_ns is not None
        and job.next_attempt_ns > now
    ]
    return max(0.0, (min(due) - now) / 1_000_000_000) if due else None


async def _upload_worker(
    queue: UploadQueue,
    catalog: Catalog,
    catalog_lock: trio.Lock,
    library: Library,
    read_client: ImmichClient,
    settings: Settings,
    notifications: trio.MemoryReceiveChannel[bool],
    online: list[bool],
    on_uploaded: Callable[[CatalogAsset], Awaitable[None]],
) -> None:
    while True:
        ready = online[0] and library.mutation_enabled
        job = await _queue_call(queue.next_due) if ready else None
        if job is not None:
            await _process_upload(
                queue,
                catalog,
                catalog_lock,
                library,
                read_client,
                settings,
                job,
                on_uploaded,
            )
            continue
        delay = await _queue_call(_next_upload_delay, queue) if ready else None
        try:
            if delay is None:
                await notifications.receive()
            else:
                with trio.move_on_after(delay):
                    await notifications.receive()
        except trio.EndOfChannel:
            return


async def _restore_worker(
    library: Library,
    notifications: trio.MemoryReceiveChannel[bool],
    pending: list[_RestoreJob],
    jobs: dict[str, _RestoreJob],
    refreshes: trio.MemorySendChannel[bool],
) -> None:
    async for _ in notifications:
        while pending:
            job = pending.pop()
            try:
                await library.remote_restore(job.asset_id)
            except Exception as error:
                job.failed = True
                LOGGER.warning("restore failed: %s", type(error).__name__)
            finally:
                try:
                    refreshes.send_nowait(False)
                except trio.WouldBlock:
                    pass
                if job.failed and jobs.get(job.asset_id) is job:
                    del jobs[job.asset_id]
                job.done.set()


def _missing_mutation_key(error: RuntimeError) -> bool:
    return "expected one mutation API key" in str(error) and str(error).endswith("found 0")


def _trusted_profile(
    settings: Settings, read_session: ServerSession, read_key: str
) -> TrustedProfile:
    return TrustedProfile(
        server_origin=settings.server_origin,
        owner_id=read_session.owner_id,
        server_version=read_session.version,
        read_permissions=READ_PERMISSIONS,
        read_key_sha256=hashlib.sha256(read_key.encode("utf-8")).hexdigest(),
    )


async def _validate_access(
    settings: Settings, read_client: ImmichClient
) -> tuple[ServerSession, ImmichClient | None, ServerSession | None]:
    read_session = await read_client.validate()
    try:
        mutation_key = load_api_key(settings, purpose="mutation")
    except RuntimeError as error:
        if not _missing_mutation_key(error):
            raise
        return read_session, None, None

    mutation_client = ImmichClient(settings.server_url, mutation_key)
    try:
        permissions = MUTATION_PERMISSIONS if settings.remote_delete else UPLOAD_PERMISSIONS
        mutation_session = await mutation_client.validate(permissions)
        if mutation_session.owner_id != read_session.owner_id:
            raise RuntimeError(
                "read-only and mutation keys belong to different Immich users"
            )
        return read_session, mutation_client, mutation_session
    except BaseException:
        await mutation_client.close()
        raise


async def _offline_worker(
    catalog: Catalog,
    library: Library,
    read_client: ImmichClient,
    read_key: str,
    trusted_profile: TrustedProfile,
    catalog_lock: trio.Lock,
    content_cache: ContentCache,
    settings: Settings,
    mount_ready: trio.Event,
    initial_entries: list[CatalogAsset],
    requests: trio.MemoryReceiveChannel[bool],
    full_requested: list[bool],
    fatal_errors: list[str],
    mutation_clients: list[ImmichClient],
    online: list[bool],
    pin_requests: trio.MemorySendChannel[bool],
    pin_pending: dict[str, CatalogAsset],
    upload_requests: trio.MemorySendChannel[bool],
    *,
    task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
) -> None:
    await populate_previews(
        initial_entries,
        read_client,
        settings.mount_path,
        downloads_enabled=False,
        task_status=task_status,
    )
    async for _ in requests:
        mutation_client: ImmichClient | None = None
        try:
            read_session, mutation_client, mutation_session = await _validate_access(
                settings, read_client
            )
            if mutation_client is not None:
                mutation_clients.append(mutation_client)
            candidate = _trusted_profile(settings, read_session, read_key)
            if (
                candidate.server_origin != trusted_profile.server_origin
                or candidate.owner_id != trusted_profile.owner_id
            ):
                raise RuntimeError("validated server identity changed while offline")
            await refresh_catalog(
                catalog,
                read_client,
                read_session,
                catalog_lock,
                trusted_profile=candidate,
            )
        except ImmichUnavailableError as error:
            LOGGER.warning("offline revalidation remains unavailable: %s", type(error).__name__)
            if mutation_client is not None:
                mutation_clients.remove(mutation_client)
                await mutation_client.close()
            continue
        except Exception as error:
            LOGGER.error("offline revalidation failed: %s", type(error).__name__)
            if mutation_client is not None:
                mutation_clients.remove(mutation_client)
                await mutation_client.close()
            fatal_errors.append("offline revalidation failed; mount terminated")
            pyfuse3.terminate()
            return

        content_cache.enable_downloads()
        if mutation_client is not None:
            assert mutation_session is not None
            library.enable_mutations(mutation_client, mutation_session)
        online[0] = True
        try:
            upload_requests.send_nowait(True)
        except trio.WouldBlock:
            pass
        full_requested[0] = False
        persisted_pins = catalog.pinned_ids()
        for entry in library.list():
            if (
                entry.asset.id in persisted_pins
                and not content_cache.describe(entry.asset)["cached"]
            ):
                pin_pending[entry.asset.id] = entry
        if pin_pending:
            try:
                pin_requests.send_nowait(True)
            except trio.WouldBlock:
                pass
        try:
            _evict_to_limits(content_cache, settings)
        except Exception as error:
            LOGGER.warning("reconnected cache eviction failed: %s", error)
        try:
            await populate_previews(
                library.list(),
                read_client,
                settings.mount_path,
                mount_ready=mount_ready,
            )
        except Exception as error:
            LOGGER.error("preview suppression failed; terminating mount: %s", error)
            fatal_errors.append("preview suppression failed; mount terminated")
            pyfuse3.terminate()
            return
        await _refresh_loop(
            catalog,
            library,
            read_client,
            read_session,
            candidate,
            catalog_lock,
            content_cache,
            settings,
            mount_ready,
            requests,
            full_requested,
            fatal_errors,
        )
        return


def _library_name_from_uri(uri: object, mount_path: Path) -> str:
    if not isinstance(uri, str):
        raise ValueError("evict URI must identify a mounted file")
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("evict URI must identify a mounted file")
    try:
        candidate = Path(unquote_to_bytes(parsed.path).decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ValueError("evict URI must identify a mounted file") from error
    if candidate.parent != mount_path or candidate.name in {"", ".", ".."}:
        raise ValueError("evict URI must identify a mounted file")
    return candidate.name


async def run_service(settings: Settings) -> None:
    _prepare_mountpoint(settings.mount_path)
    cache_root = cache_path()
    _prepare_cache_root(cache_root)
    read_key = load_api_key(settings, purpose="read-only")
    read_client = ImmichClient(settings.server_url, read_key)
    mutation_clients: list[ImmichClient] = []
    try:
        state_root = state_path()
        with (
            Catalog(state_root / "catalog.db") as catalog,
            UploadQueue(
                data_path() / "uploads",
                minimum_free_bytes=settings.minimum_free_bytes,
            ) as upload_queue,
        ):
            catalog_lock = trio.Lock()
            stored_profile = catalog.trusted_profile()
            if (
                stored_profile is not None
                and stored_profile.server_origin != settings.server_origin
            ):
                raise RuntimeError("configured server does not match trusted catalog state")

            mutation_client: ImmichClient | None = None
            mutation_session: ServerSession | None = None
            try:
                read_session, mutation_client, mutation_session = await _validate_access(
                    settings, read_client
                )
                if mutation_client is not None:
                    mutation_clients.append(mutation_client)
                trusted_profile = _trusted_profile(settings, read_session, read_key)
                if (
                    stored_profile is not None
                    and stored_profile.owner_id != trusted_profile.owner_id
                ):
                    raise RuntimeError(
                        "validated user does not match trusted catalog state"
                    )
                await refresh_catalog(
                    catalog,
                    read_client,
                    read_session,
                    catalog_lock,
                    trusted_profile=trusted_profile,
                )
                online = [True]
            except ImmichUnavailableError:
                if mutation_client is not None:
                    mutation_clients.remove(mutation_client)
                    await mutation_client.close()
                if stored_profile is None:
                    raise RuntimeError(
                        "trusted offline catalog state is unavailable"
                    ) from None
                trusted_profile = TrustedProfile(
                    server_origin=settings.server_origin,
                    owner_id=stored_profile.owner_id,
                    server_version="3.0.3",
                    read_permissions=READ_PERMISSIONS,
                    read_key_sha256=hashlib.sha256(
                        read_key.encode("utf-8")
                    ).hexdigest(),
                )
                catalog.require_offline_profile(trusted_profile)
                read_session = None
                mutation_client = None
                mutation_session = None
                online = [False]

            content_cache = ContentCache(
                cache_root / "originals",
                read_client,
                max_bytes=settings.cache_max_bytes,
                minimum_free_bytes=settings.minimum_free_bytes,
                pinned_ids=catalog.pinned_ids(),
                downloads_enabled=online[0],
            )
            library = Library(
                catalog,
                content_cache,
                settings,
                mutation_client=mutation_client,
                mutation_session=mutation_session,
                catalog_lock=catalog_lock,
            )

            requests, refreshes = trio.open_memory_channel[bool](1)
            pin_requests, pins = trio.open_memory_channel[bool](1)
            restore_requests, restores = trio.open_memory_channel[bool](1)
            upload_requests, uploads = trio.open_memory_channel[bool](1)
            pin_pending: dict[str, CatalogAsset] = {}
            pin_inflight: set[str] = set()
            restore_pending: list[_RestoreJob] = []
            # ponytail: one terminal success per asset; replace it when the row is trashed again.
            restore_jobs: dict[str, _RestoreJob] = {}
            persisted_pins = catalog.pinned_ids()
            if online[0]:
                for entry in library.list():
                    if (
                        entry.asset.id in persisted_pins
                        and not content_cache.describe(entry.asset)["cached"]
                    ):
                        pin_pending[entry.asset.id] = entry
            if pin_pending:
                pin_requests.send_nowait(True)
            if online[0] and await _queue_call(upload_queue.next_due) is not None:
                upload_requests.send_nowait(True)
            full_requested = [False]
            mount_ready = trio.Event()
            fatal_errors: list[str] = []

            async def on_uploaded(entry: CatalogAsset) -> None:
                try:
                    if entry.asset.size is not None:
                        source_path = settings.mount_path / entry.name
                        mtime = entry.asset.modified_ns // 1_000_000_000
                        prepare_thumbnail_cache(
                            source_path,
                            mtime,
                            entry.asset.size,
                            retain_size=None,
                        )
                except Exception:
                    fatal_errors.append("preview suppression failed; mount terminated")
                    raise _PreviewSuppressionError(
                        "preview suppression failed"
                    ) from None
                try:
                    await trio.to_thread.run_sync(
                        pyfuse3.invalidate_entry,
                        pyfuse3.ROOT_INODE,
                        entry.name.encode("utf-8"),
                        0,
                    )
                except (OSError, RuntimeError) as error:
                    LOGGER.warning(
                        "could not invalidate uploaded name: %s", type(error).__name__
                    )
                try:
                    requests.send_nowait(False)
                except trio.WouldBlock:
                    pass

            def on_pending() -> None:
                try:
                    upload_requests.send_nowait(True)
                except trio.WouldBlock:
                    pass

            async def current_uploads() -> tuple[UploadStatus, ...]:
                return tuple(
                    job
                    for job in await _queue_call(upload_queue.list)
                    if job.server_origin == settings.server_origin
                    and job.owner_id == trusted_profile.owner_id
                )

            filesystem = ImmichFilesystem(
                library,
                upload_queue,
                settings.server_origin,
                trusted_profile.owner_id,
                on_pending=on_pending,
            )

            async def status(params: dict[str, Any]) -> dict[str, Any]:
                if params:
                    raise ValueError("status takes no parameters")
                result = asdict(catalog.stats())
                result["online"] = online[0]
                result["mutation_enabled"] = library.mutation_enabled
                result["pending_uploads"] = len(await current_uploads())
                result["upload_quarantined"] = await trio.to_thread.run_sync(
                    lambda: upload_queue.quarantined_count
                )
                return result

            async def list_uploads(params: dict[str, Any]) -> dict[str, object]:
                if set(params) != {"after", "limit"}:
                    raise ValueError("uploads requires a cursor and limit")
                after = params["after"]
                limit = params["limit"]
                if type(limit) is not int or not 1 <= limit <= 32:
                    raise ValueError("uploads limit must be between 1 and 32")
                if after is not None:
                    if not isinstance(after, str) or str(UUID(after)) != after:
                        raise ValueError("uploads cursor must be a canonical UUID")
                jobs = list(await current_uploads())
                start = 0
                if after is not None:
                    matches = [index for index, job in enumerate(jobs) if job.id == after]
                    if len(matches) != 1:
                        raise ValueError("uploads cursor is unknown")
                    start = matches[0] + 1
                page = jobs[start : start + limit]
                more = start + len(page) < len(jobs)
                return {
                    "items": [
                        {
                            "id": job.id,
                            "name": job.requested_name,
                            "state": job.state.value,
                            "size": job.size,
                            "error": job.error.value if job.error is not None else None,
                            "revision": job.revision,
                        }
                        for job in page
                    ],
                    "next": page[-1].id if more and page else None,
                }

            async def retry_upload(params: dict[str, Any]) -> dict[str, object]:
                if set(params) != {"id"} or not isinstance(params["id"], str):
                    raise ValueError("retry-upload requires one canonical job UUID")
                if not library.mutation_enabled:
                    raise PermissionError("mutations are disabled")
                job_id = str(UUID(params["id"]))
                if job_id != params["id"]:
                    raise ValueError("retry-upload requires one canonical job UUID")
                job = await _queue_call(upload_queue.status, job_id)
                if job is None:
                    raise ValueError("unknown upload job")
                if (
                    job.server_origin != settings.server_origin
                    or job.owner_id != trusted_profile.owner_id
                ):
                    raise PermissionError("upload job belongs to another Profile")
                await _queue_call(
                    upload_queue.retry,
                    job_id,
                    at_ns=0,
                    revision=job.revision,
                )
                try:
                    upload_requests.send_nowait(True)
                except trio.WouldBlock:
                    pass
                return {"id": job_id, "scheduled": True}

            async def cancel_upload(params: dict[str, Any]) -> dict[str, object]:
                if set(params) != {"id", "revision", "confirm_name"}:
                    raise ValueError("cancel-upload requires exact confirmation")
                job_id = params["id"]
                revision = params["revision"]
                name = params["confirm_name"]
                if (
                    not isinstance(job_id, str)
                    or str(UUID(job_id)) != job_id
                    or type(revision) is not int
                    or revision < 0
                    or not isinstance(name, str)
                    or not name
                ):
                    raise ValueError("cancel-upload requires exact confirmation")
                job = await _queue_call(upload_queue.status, job_id)
                if job is None:
                    raise ValueError("unknown upload job")
                if (
                    job.server_origin != settings.server_origin
                    or job.owner_id != trusted_profile.owner_id
                ):
                    raise PermissionError("upload job belongs to another Profile")
                await _queue_call(
                    upload_queue.cancel,
                    job_id,
                    requested_name=name,
                    revision=revision,
                )
                return {"id": job_id, "cancelled": True}

            async def refresh(params: dict[str, Any]) -> dict[str, bool]:
                if params:
                    raise ValueError("refresh takes no parameters")
                full_requested[0] = True
                try:
                    requests.send_nowait(True)
                except trio.WouldBlock:
                    pass
                return {"scheduled": True}

            async def describe(params: dict[str, Any]) -> dict[str, object]:
                uris = params.get("uris")
                if (
                    set(params) != {"uris"}
                    or not isinstance(uris, list)
                    or not 0 < len(uris) <= 64
                    or not all(isinstance(uri, str) for uri in uris)
                ):
                    raise ValueError("describe requires 1 to 64 mounted file URIs")
                items: list[dict[str, object]] = []
                for uri in uris:
                    name = _library_name_from_uri(uri, settings.mount_path)
                    entry = library.lookup(name)
                    if entry is None:
                        continue
                    state = content_cache.describe(entry.asset)
                    state["busy"] = (
                        state["busy"]
                        or entry.asset.id in pin_pending
                        or entry.asset.id in pin_inflight
                    )
                    items.append(
                        {
                            "uri": uri,
                            **state,
                            "recoverable": False,
                        }
                    )
                return {"items": items}

            async def pin(params: dict[str, Any]) -> dict[str, bool]:
                if set(params) == {"asset"} and isinstance(params["asset"], str):
                    asset_id = str(UUID(params["asset"]))
                    entry = next(
                        (
                            candidate
                            for candidate in library.list()
                            if candidate.asset.id == asset_id
                        ),
                        None,
                    )
                    if entry is None:
                        return {
                            "pinned": asset_id in catalog.pinned_ids(),
                            "cached": False,
                            "busy": asset_id in pin_inflight,
                            "scheduled": asset_id in pin_pending,
                        }
                    return {
                        **content_cache.describe(entry.asset),
                        "scheduled": asset_id in pin_pending,
                    }

                pinned = params.get("pinned")
                if type(pinned) is not bool or len(params) != 2:
                    raise ValueError("pin requires one asset identity and a boolean state")
                asset_id: str | None = None
                if set(params) == {"asset", "pinned"} and isinstance(
                    params["asset"], str
                ):
                    asset_id = str(UUID(params["asset"]))
                    entry = next(
                        (
                            candidate
                            for candidate in library.list()
                            if candidate.asset.id == asset_id
                        ),
                        None,
                    )
                elif set(params) == {"uri", "pinned"}:
                    name = _library_name_from_uri(params["uri"], settings.mount_path)
                    entry = library.lookup(name)
                else:
                    raise ValueError("pin requires one asset identity and a boolean state")
                if entry is not None:
                    asset_id = entry.asset.id
                if asset_id is None:
                    raise ValueError("unknown library entry")

                if not pinned:
                    pin_pending.pop(asset_id, None)
                    catalog.unpin(asset_id)
                    content_cache.unpin(asset_id)
                    return {
                        "pinned": False,
                        "cached": entry is not None
                        and content_cache.describe(entry.asset)["cached"],
                        "busy": asset_id in pin_inflight,
                        "scheduled": False,
                    }
                if entry is None:
                    raise ValueError("unknown library entry")

                state = content_cache.describe(entry.asset)
                if state["pinned"] and state["cached"]:
                    return {**state, "scheduled": False}
                catalog.pin(asset_id)
                content_cache.pin(asset_id)
                pin_pending[asset_id] = entry
                try:
                    pin_requests.send_nowait(True)
                except trio.WouldBlock:
                    pass
                return {
                    **content_cache.describe(entry.asset),
                    "scheduled": True,
                }

            async def evict(params: dict[str, Any]) -> dict[str, object]:
                if not params:
                    return {
                        "evicted": len(
                            content_cache.evict_to_limits(
                                max_age_seconds=0,
                                max_bytes=0,
                                minimum_free_bytes=0,
                            )
                        )
                    }
                if set(params) == {"asset"} and isinstance(params["asset"], str):
                    asset_id = params["asset"]
                    UUID(asset_id)
                elif set(params) == {"uri"}:
                    name = _library_name_from_uri(params["uri"], settings.mount_path)
                    entry = library.lookup(name)
                    if entry is None:
                        raise ValueError("unknown library entry")
                    asset_id = entry.asset.id
                else:
                    raise ValueError("evict accepts an asset UUID or mounted file URI")
                return {"evicted": content_cache.evict(asset_id)}

            async def restore(params: dict[str, Any]) -> dict[str, bool]:
                if set(params) != {"asset"} or not isinstance(
                    params["asset"], str
                ):
                    raise ValueError("restore requires one canonical asset UUID")
                asset_id = str(UUID(params["asset"]))
                if asset_id != params["asset"]:
                    raise ValueError("restore requires one canonical asset UUID")
                if not library.mutation_enabled:
                    raise PermissionError("mutations are disabled")
                job = restore_jobs.get(asset_id)
                if job is not None and job.done.is_set():
                    current = catalog.by_id(asset_id)
                    if (
                        not job.failed
                        and current is not None
                        and not current.asset.is_trashed
                    ):
                        return {"restored": True, "scheduled": True}
                    job = None
                if job is None:
                    job = _RestoreJob(asset_id)
                    restore_jobs[asset_id] = job
                    restore_pending.append(job)
                    try:
                        restore_requests.send_nowait(True)
                    except trio.WouldBlock:
                        pass
                await job.done.wait()
                if job.failed:
                    raise RuntimeError("restore failed")
                return {"restored": True, "scheduled": True}

            async with (
                requests,
                refreshes,
                pin_requests,
                pins,
                restore_requests,
                restores,
                upload_requests,
                uploads,
                trio.open_nursery() as nursery,
            ):
                if online[0]:
                    assert read_session is not None
                    await nursery.start(
                        _refresh_worker,
                        catalog,
                        library,
                        read_client,
                        read_session,
                        trusted_profile,
                        catalog_lock,
                        content_cache,
                        settings,
                        mount_ready,
                        library.list(),
                        refreshes,
                        full_requested,
                        fatal_errors,
                    )
                    try:
                        _evict_to_limits(content_cache, settings)
                    except Exception as error:
                        LOGGER.warning("initial cache eviction failed: %s", error)
                else:
                    await nursery.start(
                        _offline_worker,
                        catalog,
                        library,
                        read_client,
                        read_key,
                        trusted_profile,
                        catalog_lock,
                        content_cache,
                        settings,
                        mount_ready,
                        library.list(),
                        refreshes,
                        full_requested,
                        fatal_errors,
                        mutation_clients,
                        online,
                        pin_requests,
                        pin_pending,
                        upload_requests,
                    )
                _check_mountpoint(settings.mount_path)
                pyfuse3.init(
                    filesystem,
                    str(settings.mount_path),
                    set(pyfuse3.default_options) | {"fsname=immich-on-demand", "auto_unmount"},
                )
                mount_ready.set()
                try:
                    await nursery.start(
                        serve_control,
                        runtime_path() / "control.sock",
                        {
                            "status": status,
                            "refresh": refresh,
                            "evict": evict,
                            "describe": describe,
                            "pin": pin,
                            "restore": restore,
                            "uploads": list_uploads,
                            "retry-upload": retry_upload,
                            "cancel-upload": cancel_upload,
                        },
                    )
                    nursery.start_soon(
                        _pin_worker,
                        catalog,
                        content_cache,
                        pins,
                        pin_pending,
                        pin_inflight,
                    )
                    nursery.start_soon(
                        _restore_worker,
                        library,
                        restores,
                        restore_pending,
                        restore_jobs,
                        requests,
                    )
                    nursery.start_soon(
                        _upload_worker,
                        upload_queue,
                        catalog,
                        catalog_lock,
                        library,
                        read_client,
                        settings,
                        uploads,
                        online,
                        on_uploaded,
                    )
                    nursery.start_soon(
                        _periodic_refresh,
                        requests,
                        settings.refresh_seconds,
                        False,
                        full_requested,
                        online,
                    )
                    nursery.start_soon(
                        _periodic_refresh,
                        requests,
                        FULL_REFRESH_SECONDS,
                        True,
                        full_requested,
                        online,
                    )
                    if not online[0]:
                        nursery.start_soon(_offline_retries, requests, online)
                    await pyfuse3.main()
                finally:
                    try:
                        pyfuse3.close(unmount=True)
                    finally:
                        nursery.cancel_scope.cancel()
            if fatal_errors:
                raise RuntimeError(fatal_errors[0])
    finally:
        try:
            for mutation_client in mutation_clients:
                await mutation_client.close()
        finally:
            await read_client.close()
