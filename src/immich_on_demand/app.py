from __future__ import annotations

import trio

from .catalog import Catalog, CatalogStats
from .immich import ImmichClient, ServerSession


async def refresh_catalog(
    catalog: Catalog,
    client: ImmichClient,
    session: ServerSession,
    catalog_lock: trio.Lock,
) -> CatalogStats:
    async with catalog_lock:
        catalog.begin_refresh()
        async for page in client.asset_pages(session.owner_id):
            catalog.stage(page)
        return catalog.finish_refresh()
