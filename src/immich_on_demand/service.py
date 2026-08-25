from __future__ import annotations

from dataclasses import asdict
import logging
import os
from pathlib import Path
import stat
from typing import Any
from uuid import UUID

import pyfuse3
import trio

from .app import refresh_catalog
from .catalog import Catalog, CatalogAsset
from .content_cache import ContentCache
from .control import serve_control
from .filesystem import ImmichFilesystem
from .immich import ImmichClient, MUTATION_PERMISSIONS, ServerSession, UPLOAD_PERMISSIONS
from .library import Library
from .previewer import populate_previews
from .settings import Settings, cache_path, load_api_key, runtime_path, state_path
from .thumbnails import install_failed_thumbnail


LOGGER = logging.getLogger(__name__)


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


def _evict_to_limits(content_cache: ContentCache, settings: Settings) -> list[str]:
    return content_cache.evict_to_limits(
        max_age_seconds=settings.cache_max_age_seconds,
        max_bytes=settings.cache_max_bytes,
        minimum_free_bytes=settings.minimum_free_bytes,
    )


async def _refresh_worker(
    catalog: Catalog,
    library: Library,
    read_client: ImmichClient,
    read_session: ServerSession,
    catalog_lock: trio.Lock,
    content_cache: ContentCache,
    settings: Settings,
    initial_entries: list[CatalogAsset],
    requests: trio.MemoryReceiveChannel[None],
    *,
    task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
) -> None:
    await populate_previews(
        initial_entries,
        read_client,
        settings.mount_path,
        task_status=task_status,
    )

    async for _ in requests:
        try:
            await refresh_catalog(catalog, read_client, read_session, catalog_lock)
            try:
                _evict_to_limits(content_cache, settings)
            except Exception as error:
                LOGGER.warning("background cache eviction failed: %s", error)
            await populate_previews(library.list(), read_client, settings.mount_path)
        except Exception as error:
            LOGGER.warning("background refresh failed: %s", error)


async def _periodic_refresh(
    requests: trio.MemorySendChannel[None], interval: int
) -> None:
    while True:
        await trio.sleep(interval)
        try:
            requests.send_nowait(None)
        except trio.WouldBlock:
            pass


def _missing_mutation_key(error: RuntimeError) -> bool:
    return "expected one mutation API key" in str(error) and str(error).endswith("found 0")


async def run_service(settings: Settings) -> None:
    _prepare_mountpoint(settings.mount_path)
    read_client = ImmichClient(settings.server_url, load_api_key(settings, purpose="read-only"))
    mutation_client: ImmichClient | None = None
    try:
        read_session = await read_client.validate()
        try:
            mutation_key = load_api_key(settings, purpose="mutation")
        except RuntimeError as error:
            if not _missing_mutation_key(error):
                raise
            mutation_key = None

        mutation_session: ServerSession | None = None
        if mutation_key is not None:
            mutation_client = ImmichClient(settings.server_url, mutation_key)
            permissions = MUTATION_PERMISSIONS if settings.remote_delete else UPLOAD_PERMISSIONS
            mutation_session = await mutation_client.validate(permissions)
            if mutation_session.owner_id != read_session.owner_id:
                raise RuntimeError("read-only and mutation keys belong to different Immich users")

        state_root = state_path()
        cache_root = cache_path()
        with Catalog(state_root / "catalog.db") as catalog:
            catalog_lock = trio.Lock()
            content_cache = ContentCache(cache_root / "originals", read_client)
            library = Library(
                catalog,
                read_client,
                content_cache,
                settings,
                mutation_client=mutation_client,
                mutation_session=mutation_session,
                catalog_lock=catalog_lock,
            )
            await refresh_catalog(catalog, read_client, read_session, catalog_lock)

            requests, refreshes = trio.open_memory_channel[None](1)

            async def on_uploaded(entry: CatalogAsset) -> None:
                if entry.asset.size is not None:
                    install_failed_thumbnail(
                        settings.mount_path / entry.name,
                        entry.asset.modified_ns // 1_000_000_000,
                        entry.asset.size,
                    )
                try:
                    requests.send_nowait(None)
                except trio.WouldBlock:
                    pass

            filesystem = ImmichFilesystem(
                library,
                cache_root / "uploads",
                on_uploaded=on_uploaded,
            )

            async def status(params: dict[str, Any]) -> dict[str, Any]:
                if params:
                    raise ValueError("status takes no parameters")
                result = asdict(catalog.stats())
                result["mutation_enabled"] = library.mutation_enabled
                return result

            async def refresh(params: dict[str, Any]) -> dict[str, bool]:
                if params:
                    raise ValueError("refresh takes no parameters")
                try:
                    requests.send_nowait(None)
                    scheduled = True
                except trio.WouldBlock:
                    scheduled = False
                return {"scheduled": scheduled}

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
                if set(params) != {"asset"} or not isinstance(params["asset"], str):
                    raise ValueError("evict accepts only an optional asset UUID")
                asset_id = params["asset"]
                UUID(asset_id)
                return {"evicted": content_cache.evict(asset_id)}

            async with requests, refreshes, trio.open_nursery() as nursery:
                await nursery.start(
                    _refresh_worker,
                    catalog,
                    library,
                    read_client,
                    read_session,
                    catalog_lock,
                    content_cache,
                    settings,
                    library.list(),
                    refreshes,
                )
                try:
                    _evict_to_limits(content_cache, settings)
                except Exception as error:
                    LOGGER.warning("initial cache eviction failed: %s", error)
                _check_mountpoint(settings.mount_path)
                pyfuse3.init(
                    filesystem,
                    str(settings.mount_path),
                    set(pyfuse3.default_options) | {"fsname=immich-on-demand", "auto_unmount"},
                )
                try:
                    await nursery.start(
                        serve_control,
                        runtime_path() / "control.sock",
                        {"status": status, "refresh": refresh, "evict": evict},
                    )
                    nursery.start_soon(_periodic_refresh, requests, settings.refresh_seconds)
                    await pyfuse3.main()
                finally:
                    try:
                        pyfuse3.close(unmount=True)
                    finally:
                        nursery.cancel_scope.cancel()
    finally:
        try:
            if mutation_client is not None:
                await mutation_client.close()
        finally:
            await read_client.close()
