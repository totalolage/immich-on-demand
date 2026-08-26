from __future__ import annotations

import trio

from .catalog import Catalog, CatalogAsset
from .content_cache import ContentCache
from .immich import ImmichClient, ServerSession
from .settings import Settings


class LibraryError(RuntimeError):
    pass


class Library:
    """Immutable library reads plus explicitly guarded create and trash operations."""

    def __init__(
        self,
        catalog: Catalog,
        content_cache: ContentCache,
        settings: Settings,
        *,
        mutation_client: ImmichClient | None = None,
        mutation_session: ServerSession | None = None,
        catalog_lock: trio.Lock,
    ) -> None:
        self._catalog = catalog
        self._mutation_client = mutation_client
        self._mutation_session = mutation_session
        self._content_cache = content_cache
        self._settings = settings
        # ponytail: one service-wide lock; split only if mutation throughput matters.
        self._catalog_lock = catalog_lock

    def list(self) -> list[CatalogAsset]:
        return self._catalog.list_visible()

    @property
    def mutation_enabled(self) -> bool:
        return self._mutation_client is not None and self._mutation_session is not None

    @property
    def replacement_enabled(self) -> bool:
        return self.mutation_enabled and self._settings.remote_delete

    def enable_mutations(
        self, mutation_client: ImmichClient, mutation_session: ServerSession
    ) -> None:
        self._mutation_client, self._mutation_session = mutation_client, mutation_session

    def upload_access(self) -> tuple[ImmichClient, ServerSession]:
        return self._mutation_access()

    async def read(self, entry: CatalogAsset, offset: int, size: int) -> bytes:
        return await self._content_cache.read(entry.asset, offset, size)

    def acquire(self, entry: CatalogAsset) -> None:
        self._content_cache.acquire(entry.asset.id)

    def release(self, entry: CatalogAsset) -> None:
        self._content_cache.release(entry.asset.id)

    async def remote_trash(self, entry: CatalogAsset) -> None:
        if not self._settings.remote_delete:
            raise PermissionError("remote deletion is disabled")
        mutation, session = self._mutation_access()
        if not session.trash_enabled:
            raise PermissionError("Immich trash is disabled")
        async with self._catalog_lock:
            current = self._catalog.by_inode(entry.inode)
            if current is None or current.asset.id != entry.asset.id:
                raise LibraryError("asset is no longer in the catalog")
            if current.asset.owner_id != session.owner_id:
                raise PermissionError("only owned assets can be remotely deleted")

            await mutation.trash(current.asset.id)
            self._catalog.mark_trashed(current.asset.id)

    async def remote_restore(self, asset_id: str) -> CatalogAsset:
        if not self._settings.remote_delete:
            raise PermissionError("remote deletion is disabled")
        mutation, session = self._mutation_access()
        if not session.trash_enabled:
            raise PermissionError("Immich trash is disabled")
        async with self._catalog_lock:
            current = self._catalog.by_id(asset_id)
            if current is None:
                raise LibraryError("asset is not in the catalog")
            if not current.asset.is_trashed:
                raise LibraryError("asset is not trashed")
            if current.asset.owner_id != session.owner_id:
                raise PermissionError("only owned assets can be remotely restored")

            await mutation.restore(current.asset.id)
            self._catalog.mark_restored(current.asset.id)
            restored = self._catalog.by_id(current.asset.id)
            if restored is None or restored.asset.is_trashed:
                raise LibraryError("asset restore was not committed to the catalog")
            return restored

    def _mutation_access(self) -> tuple[ImmichClient, ServerSession]:
        if self._mutation_client is None or self._mutation_session is None:
            raise LibraryError("a validated mutation client and session are required")
        return self._mutation_client, self._mutation_session
