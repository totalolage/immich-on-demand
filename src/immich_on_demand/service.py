from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
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

from .app import (
    FullRefreshRequired,
    reconcile_album_people,
    refresh_catalog,
    refresh_catalog_incremental,
)
from .catalog import (
    ROOT_INODE,
    Catalog,
    CatalogAsset,
    CatalogDirectory,
    CatalogFile,
    TrustedProfile,
)
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
from .uploads import (
    UploadErrorCode,
    UploadOperation,
    UploadQueue,
    UploadQueueError,
    UploadState,
    UploadStatus,
)


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


def _replacement_refresh_policy(
    catalog: Catalog, jobs: tuple[UploadStatus, ...]
) -> tuple[
    tuple[Asset, ...],
    frozenset[str],
    frozenset[tuple[str, int, str]],
]:
    preserved: dict[str, Asset] = {}
    excluded: set[str] = set()
    signatures: set[tuple[str, int, str]] = set()
    for job in jobs:
        if (
            job.operation is not UploadOperation.REPLACEMENT
            or job.size is None
            or job.sha1 is None
        ):
            continue
        source = catalog.by_id(job.old_asset_id or "")
        if source is None or source.inode != job.old_inode:
            raise RuntimeError("replacement source is unavailable during refresh")
        preserved[source.asset.id] = source.asset
        try:
            checksum = base64.b64encode(bytes.fromhex(job.sha1)).decode("ascii")
        except ValueError as error:
            raise RuntimeError("replacement payload identity is invalid") from error
        if job.candidate_asset_id is None:
            signatures.add((job.owner_id, job.size, checksum))
            continue
        candidate = catalog.by_id(job.candidate_asset_id)
        if (
            source.asset.is_trashed
            and candidate is not None
            and candidate.name == job.old_name
        ):
            preserved[candidate.asset.id] = candidate.asset
        elif (
            job.state is UploadState.BLOCKED
            and candidate is not None
            and job.error in {
                UploadErrorCode.CANDIDATE_MISMATCH,
                UploadErrorCode.UPLOAD_REJECTED,
            }
        ):
            preserved[candidate.asset.id] = candidate.asset
        else:
            excluded.add(job.candidate_asset_id)
    return (
        tuple(preserved.values()),
        frozenset(excluded),
        frozenset(signatures),
    )


async def _full_refresh(
    catalog: Catalog,
    read_client: ImmichClient,
    read_session: ServerSession,
    trusted_profile: TrustedProfile,
    catalog_lock: trio.Lock,
    mount_path: Path,
    replacement_jobs: tuple[UploadStatus, ...] = (),
) -> None:
    replacement_jobs = tuple(
        job
        for job in replacement_jobs
        if job.size is not None and job.sha1 is not None
    )
    await refresh_catalog(
        catalog,
        read_client,
        read_session,
        catalog_lock,
        suppression=partial(
            _replacement_refresh_policy,
            catalog,
            replacement_jobs,
        ),
    )
    try:
        await populate_previews(
            catalog,
            read_client,
            mount_path,
            downloads_enabled=False,
        )
    except Exception as error:
        raise _PreviewSuppressionError("preview suppression failed") from error
    if not any(
        job.state
        in {
            UploadState.PENDING,
            UploadState.ATTEMPTING,
            UploadState.REPLACING,
        }
        for job in replacement_jobs
    ):
        await reconcile_album_people(
            catalog,
            read_client,
            read_session,
            catalog_lock,
            trusted_profile=trusted_profile,
        )


async def _refresh_loop(
    catalog: Catalog,
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
    upload_queue: UploadQueue,
) -> None:
    async for force_full in requests:
        force_full = force_full or full_requested[0]
        full_requested[0] = False
        try:
            replacement_jobs = tuple(
                job
                for job in await _queue_call(upload_queue.list)
                if job.operation is UploadOperation.REPLACEMENT
            )
            if force_full:
                await _full_refresh(
                    catalog,
                    read_client,
                    read_session,
                    trusted_profile,
                    catalog_lock,
                    settings.mount_path,
                    replacement_jobs,
                )
            else:
                preserved, excluded, signatures = _replacement_refresh_policy(
                    catalog, replacement_jobs
                )
                await refresh_catalog_incremental(
                    catalog,
                    read_client,
                    read_session,
                    catalog_lock,
                    refresh_seconds=settings.refresh_seconds,
                    preserve_asset_ids=frozenset(asset.id for asset in preserved),
                    exclude_asset_ids=excluded,
                    exclude_asset_signatures=signatures,
                )
        except (FullRefreshRequired, ImmichPageLimitError, ImmichResponseError):
            try:
                await _full_refresh(
                    catalog,
                    read_client,
                    read_session,
                    trusted_profile,
                    catalog_lock,
                    settings.mount_path,
                    replacement_jobs,
                )
            except _PreviewSuppressionError as error:
                LOGGER.error("preview suppression failed; terminating mount: %s", error)
                fatal_errors.append("preview suppression failed; mount terminated")
                pyfuse3.terminate()
                return
            except Exception as error:
                LOGGER.warning("background full refresh failed: %s", error)
                continue
        except _PreviewSuppressionError as error:
            LOGGER.error("preview suppression failed; terminating mount: %s", error)
            fatal_errors.append("preview suppression failed; mount terminated")
            pyfuse3.terminate()
            return
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
                catalog,
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
    upload_queue: UploadQueue,
    *,
    task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
) -> None:
    await populate_previews(
        catalog,
        read_client,
        settings.mount_path,
        mount_ready=mount_ready,
        task_status=task_status,
    )
    await _refresh_loop(
        catalog,
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
        upload_queue,
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
    pending: dict[str, CatalogAsset | CatalogFile],
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


def _replacement_source_matches(
    job: UploadStatus, asset: Asset, *, allow_trashed: bool = False
) -> bool:
    return (
        asset.id == job.old_asset_id
        and asset.owner_id == job.source_owner_id == job.owner_id
        and asset.library_id == job.source_library_id is None
        and asset.live_photo_video_id is None
        and asset.checksum == job.source_checksum
        and (
            asset.updated_at == job.source_updated_at
            or (allow_trashed and asset.is_trashed)
        )
        and asset.created_ns == job.source_created_ns
        and asset.is_favorite == job.source_is_favorite
        and asset.visibility == job.source_visibility
        and asset.size is not None
        and asset.visibility != "hidden"
        and not asset.is_offline
        and (allow_trashed or not asset.is_trashed)
    )


def _replacement_candidate_matches(source: Asset, candidate: Asset) -> bool:
    return (
        candidate.created_ns == source.created_ns
        and candidate.local_date == source.local_date
        and candidate.is_favorite == source.is_favorite
        and candidate.visibility == source.visibility
        and candidate.live_photo_video_id == source.live_photo_video_id
    )


def _same_managed_checksum(job: UploadStatus, source: Asset) -> bool:
    try:
        checksum = base64.b64decode(source.checksum, validate=True).hex()
    except (ValueError, TypeError):
        return False
    return (
        source.library_id is None
        and job.sha1 is not None
        and hmac.compare_digest(checksum, job.sha1)
    )


async def _stable_album_ids(
    client: ImmichClient, asset_id: str
) -> tuple[str, ...] | None:
    first = tuple(sorted(album.id for album in await client.albums(asset_id=asset_id)))
    second = tuple(sorted(album.id for album in await client.albums(asset_id=asset_id)))
    return first if first == second else None


async def _block_upload(
    queue: UploadQueue, job_id: str, error: UploadErrorCode
) -> None:
    try:
        await _queue_call(queue.block, job_id, error)
    except Exception as queue_error:
        LOGGER.warning(
            "could not block an upload job: %s", type(queue_error).__name__
        )


async def _retry_upload(
    queue: UploadQueue,
    job: UploadStatus,
    error: UploadErrorCode,
) -> None:
    try:
        await _queue_call(
            queue.retry,
            job.id,
            at_ns=_upload_retry_at(job.attempt_count),
            error=error,
        )
    except Exception as queue_error:
        LOGGER.warning(
            "could not schedule an upload retry: %s", type(queue_error).__name__
        )


async def _notify_upload_finished(
    callback: Callable[[str], Awaitable[None] | None] | None,
    job_id: str,
) -> None:
    if callback is None:
        return
    try:
        result = callback(job_id)
        if isinstance(result, Awaitable):
            await result
    except Exception as error:
        LOGGER.warning(
            "upload completion notification failed: %s", type(error).__name__
        )


async def _remove_committed_upload(
    queue: UploadQueue,
    callback: Callable[[str], Awaitable[None] | None] | None,
    job_id: str,
) -> None:
    try:
        await _queue_call(queue.remove, job_id)
    except Exception as error:
        LOGGER.warning(
            "could not remove committed upload job: %s", type(error).__name__
        )
    await _notify_upload_finished(callback, job_id)


async def _transfer_replacement_pin(
    content_cache: ContentCache | None,
    catalog: Catalog,
    source_asset_id: str,
    replacement: CatalogAsset,
) -> None:
    if content_cache is not None:
        await content_cache.transfer_pin(
            source_asset_id,
            replacement.asset,
            pinned=replacement.asset.id in catalog.pinned_ids(),
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
    on_upload_finished: Callable[[str], Awaitable[None] | None] | None = None,
    *,
    content_cache: ContentCache | None = None,
) -> None:
    if job.server_origin != settings.server_origin:
        await _block_upload(queue, job.id, UploadErrorCode.PROFILE_MISMATCH)
        return
    if not library.mutation_enabled:
        return
    mutation, session = library.upload_access()
    if session.owner_id != job.owner_id:
        await _block_upload(queue, job.id, UploadErrorCode.PROFILE_MISMATCH)
        return

    current = job
    try:
        replacement = current.operation is UploadOperation.REPLACEMENT
        if replacement and not settings.remote_delete:
            await _block_upload(queue, current.id, UploadErrorCode.UPLOAD_REJECTED)
            return

        source_entry: CatalogAsset | None = None
        async with catalog_lock if replacement else nullcontext():
            if replacement:
                source_entry = catalog.by_id(current.old_asset_id or "")
                published = (
                    catalog.by_id(current.candidate_asset_id)
                    if current.candidate_asset_id is not None
                    else None
                )
                if (
                    current.state is UploadState.REPLACING
                    and current.candidate_verified
                    and source_entry is not None
                    and source_entry.inode == current.old_inode
                    and source_entry.asset.is_trashed
                    and _replacement_source_matches(
                        current, source_entry.asset, allow_trashed=True
                    )
                    and published is not None
                    and published.name == current.old_name
                    and published.inode != source_entry.inode
                    and _upload_matches(current, published.asset, current.id)
                    and _replacement_candidate_matches(
                        source_entry.asset, published.asset
                    )
                ):
                    await on_uploaded(published)
                    await _transfer_replacement_pin(
                        content_cache,
                        catalog,
                        source_entry.asset.id,
                        published,
                    )
                    await _queue_call(queue.commit, current.id)
                    await _remove_committed_upload(
                        queue, on_upload_finished, current.id
                    )
                    return
                if (
                    source_entry is None
                    or source_entry.inode != current.old_inode
                    or source_entry.name != current.old_name
                    or current.requested_name != current.old_name
                    or not _replacement_source_matches(current, source_entry.asset)
                    or catalog.album_ids(source_entry.asset.id)
                    != current.source_album_ids
                ):
                    await _block_upload(
                        queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
                    )
                    return
                remote_source = await read_client.asset(source_entry.asset.id)
                if not _replacement_source_matches(
                    current,
                    remote_source,
                    allow_trashed=(
                        current.state is UploadState.REPLACING
                        and current.candidate_verified
                    ),
                ):
                    await _block_upload(
                        queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
                    )
                    return
                if (
                    current.state is UploadState.PENDING
                    and current.candidate_asset_id is None
                    and current.attempt_count == 0
                    and _same_managed_checksum(current, source_entry.asset)
                ):
                    await _queue_call(
                        queue.cancel,
                        current.id,
                        requested_name=current.requested_name,
                        revision=current.revision,
                    )
                    await _notify_upload_finished(on_upload_finished, current.id)
                    return

            if current.state is not UploadState.REPLACING:
                current, descriptor = await _queue_call(
                    queue.open_attempt,
                    current.id,
                    revision=current.revision,
                )
                try:
                    if current.candidate_asset_id is None:
                        if source_entry is None:
                            result = await mutation.upload(
                                descriptor,
                                current.requested_name,
                                session.media_types,
                                current.id,
                            )
                        else:
                            result = await mutation.upload(
                                descriptor,
                                current.requested_name,
                                session.media_types,
                                current.id,
                                replacement_source=source_entry.asset,
                            )
                    else:
                        result = None
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
                await _block_upload(
                    queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
                )
                return

            if source_entry is None:
                async with catalog_lock:
                    entry = catalog.add_uploaded(asset, current.requested_name)
                    await on_uploaded(entry)
            else:
                if not _replacement_candidate_matches(source_entry.asset, asset):
                    await _block_upload(
                        queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
                    )
                    return
                current = await _queue_call(queue.begin_replacing, current.id)
                expected_albums = current.source_album_ids
                assert expected_albums is not None
                remote_source = await read_client.asset(source_entry.asset.id)
                if not _replacement_source_matches(
                    current, remote_source, allow_trashed=True
                ):
                    await _block_upload(
                        queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
                    )
                    return

                if remote_source.is_trashed:
                    candidate_albums = await _stable_album_ids(
                        read_client, asset.id
                    )
                    if candidate_albums != expected_albums:
                        await _block_upload(
                            queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
                        )
                        return
                else:
                    source_albums = await _stable_album_ids(
                        read_client, remote_source.id
                    )
                    if source_albums != expected_albums:
                        await _block_upload(
                            queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
                        )
                        return
                    await mutation.copy_albums(remote_source.id, asset.id)
                    source_albums = await _stable_album_ids(
                        read_client, remote_source.id
                    )
                    candidate_albums = await _stable_album_ids(
                        read_client, asset.id
                    )
                    remote_source = await read_client.asset(remote_source.id)
                    if (
                        source_albums != expected_albums
                        or candidate_albums != expected_albums
                        or not _replacement_source_matches(current, remote_source)
                    ):
                        await _block_upload(
                            queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
                        )
                        return

                    trash_error: Exception | None = None
                    try:
                        await mutation.trash(remote_source.id)
                    except (
                        ImmichUnavailableError,
                        ImmichRetryableError,
                        ImmichResponseError,
                    ) as error:
                        trash_error = error
                    remote_source = await read_client.asset(remote_source.id)
                    if not _replacement_source_matches(
                        current, remote_source, allow_trashed=True
                    ):
                        await _block_upload(
                            queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
                        )
                        return
                    if not remote_source.is_trashed:
                        if trash_error is not None:
                            raise trash_error
                        raise ImmichRetryableError(
                            "Immich did not confirm replacement source trash"
                        )

                asset = await read_client.asset(asset.id)
                if not _upload_matches(current, asset, marker) or not (
                    _replacement_candidate_matches(source_entry.asset, asset)
                ):
                    await _block_upload(
                        queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
                    )
                    return
                entry = catalog.publish_replacement(
                    old_asset_id=source_entry.asset.id,
                    candidate=asset,
                )
                await on_uploaded(entry)
                await _transfer_replacement_pin(
                    content_cache,
                    catalog,
                    source_entry.asset.id,
                    entry,
                )

            await _queue_call(queue.commit, current.id)
    except (ImmichUnavailableError, ImmichRetryableError):
        await _retry_upload(queue, current, UploadErrorCode.UPLOAD_UNAVAILABLE)
    except ImmichResponseError:
        if (
            current.candidate_asset_id is None
            or current.state is UploadState.REPLACING
        ):
            await _retry_upload(queue, current, UploadErrorCode.AMBIGUOUS_RESPONSE)
        else:
            await _block_upload(
                queue, current.id, UploadErrorCode.CANDIDATE_MISMATCH
            )
    except ImmichError:
        await _block_upload(queue, current.id, UploadErrorCode.UPLOAD_REJECTED)
    except UploadQueueError as error:
        LOGGER.warning("upload queue rejected a job: %s", type(error).__name__)
    except _PreviewSuppressionError:
        await _block_upload(queue, current.id, UploadErrorCode.LOCAL_STATE_FAILED)
        raise
    except Exception as error:
        await _block_upload(queue, current.id, UploadErrorCode.LOCAL_STATE_FAILED)
        LOGGER.warning("upload job was blocked: %s", type(error).__name__)
    else:
        await _remove_committed_upload(queue, on_upload_finished, current.id)


def _next_upload_delay(queue: UploadQueue) -> float | None:
    now = time.time_ns()
    due = [
        job.next_attempt_ns
        for job in queue.list()
        if job.state
        in {UploadState.PENDING, UploadState.ATTEMPTING, UploadState.REPLACING}
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
    content_cache: ContentCache,
    notifications: trio.MemoryReceiveChannel[bool],
    online: list[bool],
    on_uploaded: Callable[[CatalogAsset], Awaitable[None]],
    on_upload_finished: Callable[[str], Awaitable[None] | None] | None = None,
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
                on_upload_finished,
                content_cache=content_cache,
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
    on_restored: Callable[[CatalogAsset], Awaitable[None]],
) -> None:
    async for _ in notifications:
        while pending:
            job = pending.pop()
            try:
                entry = await library.remote_restore(job.asset_id)
                await on_restored(entry)
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
        format_version=2,
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
    requests: trio.MemoryReceiveChannel[bool],
    full_requested: list[bool],
    fatal_errors: list[str],
    mutation_clients: list[ImmichClient],
    online: list[bool],
    pin_requests: trio.MemorySendChannel[bool],
    pin_pending: dict[str, CatalogAsset | CatalogFile],
    upload_requests: trio.MemorySendChannel[bool],
    upload_queue: UploadQueue,
    *,
    task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
) -> None:
    await populate_previews(
        catalog,
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
            await _full_refresh(
                catalog,
                read_client,
                read_session,
                candidate,
                catalog_lock,
                settings.mount_path,
                tuple(
                    job
                    for job in await _queue_call(upload_queue.list)
                    if job.operation is UploadOperation.REPLACEMENT
                ),
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
                catalog,
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
            upload_queue,
        )
        return


def _catalog_file_from_uri(
    uri: object, mount_path: Path, catalog: Catalog
) -> CatalogFile | None:
    if not isinstance(uri, str):
        raise ValueError("URI must identify a mounted file")
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("URI must identify a mounted file")
    try:
        decoded = unquote_to_bytes(parsed.path).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("URI must identify a mounted file") from error
    if not decoded.startswith("/") or any(
        part in {"", ".", ".."} for part in decoded.split("/")[1:]
    ):
        raise ValueError("URI must identify a mounted file")
    candidate = Path(decoded)
    try:
        relative = candidate.relative_to(mount_path)
    except ValueError:
        raise ValueError("URI must identify a mounted file") from None
    if not relative.parts:
        raise ValueError("URI must identify a mounted file")
    parent = ROOT_INODE
    for index, name in enumerate(relative.parts):
        node = catalog.lookup(parent, name)
        if node is None:
            return None
        if index == len(relative.parts) - 1:
            if isinstance(node, CatalogFile):
                return node
            raise ValueError("URI must identify a mounted file")
        if not isinstance(node, CatalogDirectory):
            raise ValueError("URI must identify a mounted file")
        parent = node.inode
    raise AssertionError("nonempty mounted path did not resolve")


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
                await _full_refresh(
                    catalog,
                    read_client,
                    read_session,
                    trusted_profile,
                    catalog_lock,
                    settings.mount_path,
                    tuple(
                        job
                        for job in await _queue_call(upload_queue.list)
                        if job.operation is UploadOperation.REPLACEMENT
                    ),
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
                    read_permissions=stored_profile.read_permissions,
                    read_key_sha256=hashlib.sha256(
                        read_key.encode("utf-8")
                    ).hexdigest(),
                    format_version=stored_profile.format_version,
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
            all_view = catalog.lookup(ROOT_INODE, "All")
            if not isinstance(all_view, CatalogDirectory) or not all_view.mutation_root:
                raise RuntimeError("trusted catalog All View is unavailable")

            requests, refreshes = trio.open_memory_channel[bool](1)
            pin_requests, pins = trio.open_memory_channel[bool](1)
            restore_requests, restores = trio.open_memory_channel[bool](1)
            upload_requests, uploads = trio.open_memory_channel[bool](1)
            pin_pending: dict[str, CatalogAsset | CatalogFile] = {}
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
                invalidations: list[tuple[int, bytes]] = []
                try:
                    if entry.asset.size is not None:
                        mtime = entry.asset.modified_ns // 1_000_000_000
                        aliases = catalog.aliases(entry.asset.id)
                        if not aliases:
                            raise ValueError("uploaded asset has no mounted aliases")
                        for alias in aliases:
                            if alias.name != entry.name:
                                raise ValueError("uploaded asset alias name changed")
                            parent_inode = ROOT_INODE
                            for component in alias.parts[:-1]:
                                parent = catalog.lookup(parent_inode, component)
                                if not isinstance(parent, CatalogDirectory):
                                    raise ValueError("uploaded asset alias is invalid")
                                parent_inode = parent.inode
                            prepare_thumbnail_cache(
                                settings.mount_path.joinpath(*alias.parts),
                                mtime,
                                entry.asset.size,
                                retain_size=None,
                            )
                            invalidations.append(
                                (parent_inode, entry.name.encode("utf-8"))
                            )
                except Exception:
                    fatal_errors.append("preview suppression failed; mount terminated")
                    pyfuse3.terminate()
                    raise _PreviewSuppressionError(
                        "preview suppression failed"
                    ) from None
                for parent_inode, name in invalidations:
                    try:
                        await trio.to_thread.run_sync(
                            pyfuse3.invalidate_entry, parent_inode, name, 0
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
                catalog,
                upload_queue,
                settings.server_origin,
                trusted_profile.owner_id,
                on_pending=on_pending,
            )
            upload_finished = getattr(filesystem, "upload_finished", None)

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
                await _notify_upload_finished(upload_finished, job_id)
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
                    entry = _catalog_file_from_uri(uri, settings.mount_path, catalog)
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
                    entry = _catalog_file_from_uri(
                        params["uri"], settings.mount_path, catalog
                    )
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
                    entry = _catalog_file_from_uri(
                        params["uri"], settings.mount_path, catalog
                    )
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
                        read_client,
                        read_session,
                        trusted_profile,
                        catalog_lock,
                        content_cache,
                        settings,
                        mount_ready,
                        refreshes,
                        full_requested,
                        fatal_errors,
                        upload_queue,
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
                        refreshes,
                        full_requested,
                        fatal_errors,
                        mutation_clients,
                        online,
                        pin_requests,
                        pin_pending,
                        upload_requests,
                        upload_queue,
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
                        on_uploaded,
                    )
                    nursery.start_soon(
                        _upload_worker,
                        upload_queue,
                        catalog,
                        catalog_lock,
                        library,
                        read_client,
                        settings,
                        content_cache,
                        uploads,
                        online,
                        on_uploaded,
                        upload_finished,
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
