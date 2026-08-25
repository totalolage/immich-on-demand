from __future__ import annotations

from pathlib import Path
from uuid import UUID

from .catalog import Catalog, CatalogAsset
from .content_cache import ContentCache
from .immich import ImmichClient, ServerSession
from .model import safe_filename
from .settings import Settings


class LibraryError(RuntimeError):
    pass


class Library:
    """Immutable library reads plus explicitly guarded create and trash operations."""

    def __init__(
        self,
        catalog: Catalog,
        read_client: ImmichClient,
        content_cache: ContentCache,
        settings: Settings,
        *,
        mutation_client: ImmichClient | None = None,
        mutation_session: ServerSession | None = None,
    ) -> None:
        self._catalog = catalog
        self._read_client = read_client
        self._mutation_client = mutation_client
        self._mutation_session = mutation_session
        self._content_cache = content_cache
        self._settings = settings

    def list(self) -> list[CatalogAsset]:
        return self._catalog.list_visible()

    def lookup(self, identity: str | int) -> CatalogAsset | None:
        entry = (
            self._catalog.by_inode(identity)
            if isinstance(identity, int)
            else self._catalog.by_name(identity)
        )
        return entry if entry is not None and entry.asset.visible else None

    async def read(self, entry: CatalogAsset, offset: int, size: int) -> bytes:
        return await self._content_cache.read(entry.asset, offset, size)

    async def upload_new(self, staged_path: Path, requested_name: str) -> CatalogAsset:
        if safe_filename(requested_name, str(UUID(int=0))) != requested_name:
            raise ValueError("requested library name is not safe")
        if self._catalog.by_name(requested_name) is not None:
            raise FileExistsError(requested_name)
        mutation, session = self._mutation_access()

        result = await mutation.upload(staged_path, session.media_types)
        uploaded = await self._read_client.asset(result.asset_id)
        if uploaded.owner_id != session.owner_id:
            raise LibraryError("uploaded asset is not owned by the mutation user")
        return self._catalog.add_uploaded(uploaded, requested_name)

    async def remote_trash(self, entry: CatalogAsset) -> None:
        if not self._settings.remote_delete:
            raise PermissionError("remote deletion is disabled")
        mutation, session = self._mutation_access()
        if not session.trash_enabled:
            raise PermissionError("Immich trash is disabled")
        current = self._catalog.by_inode(entry.inode)
        if current is None or current.asset.id != entry.asset.id:
            raise LibraryError("asset is no longer in the catalog")
        if current.asset.owner_id != session.owner_id:
            raise PermissionError("only owned assets can be remotely deleted")

        await mutation.trash(current.asset.id, trash_enabled=True)
        self._catalog.mark_trashed(current.asset.id)

    def _mutation_access(self) -> tuple[ImmichClient, ServerSession]:
        if self._mutation_client is None or self._mutation_session is None:
            raise LibraryError("a validated mutation client and session are required")
        return self._mutation_client, self._mutation_session
