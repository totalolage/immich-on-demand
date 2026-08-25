from __future__ import annotations

import trio

from .catalog import Catalog, CatalogStats
from .immich import ImmichClient, ImmichError, ServerSession


MAX_REFRESH_SWEEPS = 3


async def refresh_catalog(
    catalog: Catalog,
    client: ImmichClient,
    session: ServerSession,
    catalog_lock: trio.Lock,
) -> CatalogStats:
    async with catalog_lock:
        previous_ids: set[str] | None = None
        for _ in range(MAX_REFRESH_SWEEPS):
            catalog.begin_refresh()
            asset_ids: set[str] = set()
            duplicate = False
            async for page in client.asset_pages(session.owner_id):
                for asset in page:
                    if asset.id in asset_ids:
                        duplicate = True
                    asset_ids.add(asset.id)
                catalog.stage(page)
            if duplicate:
                previous_ids = None
            elif asset_ids == previous_ids:
                return catalog.finish_refresh()
            else:
                previous_ids = asset_ids
        raise ImmichError(
            f"Immich asset list did not stabilize after {MAX_REFRESH_SWEEPS} complete sweeps"
        )
