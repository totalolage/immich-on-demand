from __future__ import annotations

from dataclasses import asdict
import logging
import os
from pathlib import Path
import stat
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit
from uuid import UUID

import pyfuse3
import trio

from .app import FullRefreshRequired, refresh_catalog, refresh_catalog_incremental
from .catalog import Catalog, CatalogAsset
from .content_cache import ContentCache
from .control import serve_control
from .filesystem import ImmichFilesystem
from .immich import (
    ImmichClient,
    ImmichPageLimitError,
    ImmichResponseError,
    MUTATION_PERMISSIONS,
    ServerSession,
    UPLOAD_PERMISSIONS,
)
from .library import Library
from .previewer import populate_previews
from .settings import Settings, cache_path, load_api_key, runtime_path, state_path
from .thumbnails import prepare_thumbnail_cache


LOGGER = logging.getLogger(__name__)
FULL_REFRESH_SECONDS = 24 * 60 * 60


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


async def _refresh_worker(
    catalog: Catalog,
    library: Library,
    read_client: ImmichClient,
    read_session: ServerSession,
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

    async for force_full in requests:
        force_full = force_full or full_requested[0]
        full_requested[0] = False
        try:
            if force_full:
                await refresh_catalog(catalog, read_client, read_session, catalog_lock)
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
                await refresh_catalog(catalog, read_client, read_session, catalog_lock)
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


async def _periodic_refresh(
    requests: trio.MemorySendChannel[bool],
    interval: int,
    force_full: bool,
    full_requested: list[bool],
) -> None:
    while True:
        await trio.sleep(interval)
        if force_full:
            full_requested[0] = True
        try:
            requests.send_nowait(force_full)
        except trio.WouldBlock:
            pass


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


def _missing_mutation_key(error: RuntimeError) -> bool:
    return "expected one mutation API key" in str(error) and str(error).endswith("found 0")


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
        with Catalog(state_root / "catalog.db") as catalog:
            catalog_lock = trio.Lock()
            content_cache = ContentCache(
                cache_root / "originals",
                read_client,
                max_bytes=settings.cache_max_bytes,
                minimum_free_bytes=settings.minimum_free_bytes,
                pinned_ids=catalog.pinned_ids(),
            )
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

            requests, refreshes = trio.open_memory_channel[bool](1)
            pin_requests, pins = trio.open_memory_channel[bool](1)
            pin_pending: dict[str, CatalogAsset] = {}
            pin_inflight: set[str] = set()
            persisted_pins = catalog.pinned_ids()
            for entry in library.list():
                if (
                    entry.asset.id in persisted_pins
                    and not content_cache.describe(entry.asset)["cached"]
                ):
                    pin_pending[entry.asset.id] = entry
            if pin_pending:
                pin_requests.send_nowait(True)
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
                    raise
                try:
                    requests.send_nowait(False)
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

            async with requests, refreshes, pin_requests, pins, trio.open_nursery() as nursery:
                await nursery.start(
                    _refresh_worker,
                    catalog,
                    library,
                    read_client,
                    read_session,
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
                        _periodic_refresh,
                        requests,
                        settings.refresh_seconds,
                        False,
                        full_requested,
                    )
                    nursery.start_soon(
                        _periodic_refresh,
                        requests,
                        FULL_REFRESH_SECONDS,
                        True,
                        full_requested,
                    )
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
            if mutation_client is not None:
                await mutation_client.close()
        finally:
            await read_client.close()
