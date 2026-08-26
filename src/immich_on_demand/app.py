from __future__ import annotations

from contextlib import aclosing

import trio

from .catalog import Catalog, CatalogStats, TrustedProfile
from .immich import ImmichClient, ImmichError, ServerSession
from .model import timestamp_nanoseconds


MAX_REFRESH_SWEEPS = 3


class FullRefreshRequired(ImmichError):
    pass


def _updated_ms(value: str) -> int:
    return timestamp_nanoseconds(value) // 1_000_000


async def refresh_catalog(
    catalog: Catalog,
    client: ImmichClient,
    session: ServerSession,
    catalog_lock: trio.Lock,
    *,
    trusted_profile: TrustedProfile | None = None,
) -> CatalogStats:
    if trusted_profile is not None and (
        trusted_profile.owner_id != session.owner_id
        or trusted_profile.server_version != session.version
    ):
        raise ValueError("trusted profile does not match the validated server session")
    async with catalog_lock:
        previous_ids: set[str] | None = None
        for _ in range(MAX_REFRESH_SWEEPS):
            catalog.begin_refresh()
            asset_ids: set[str] = set()
            high_water_ms = 0
            page_count = 0
            duplicate = False
            async with aclosing(client.asset_pages(session.owner_id)) as pages:
                async for page in pages:
                    page_count += 1
                    for asset in page:
                        if asset.id in asset_ids:
                            duplicate = True
                        asset_ids.add(asset.id)
                        high_water_ms = max(high_water_ms, _updated_ms(asset.updated_at))
                    catalog.stage(page)
            if duplicate:
                previous_ids = None
            elif asset_ids == previous_ids:
                return catalog.finish_refresh(
                    high_water_ms=high_water_ms,
                    page_count=page_count,
                    trusted_profile=trusted_profile,
                )
            else:
                previous_ids = asset_ids
        raise ImmichError(
            f"Immich asset list did not stabilize after {MAX_REFRESH_SWEEPS} complete sweeps"
        )


async def refresh_catalog_incremental(
    catalog: Catalog,
    client: ImmichClient,
    session: ServerSession,
    catalog_lock: trio.Lock,
    *,
    refresh_seconds: int,
) -> CatalogStats:
    async with catalog_lock:
        high_water_ms, full_refresh_pages = catalog.refresh_state()
        if full_refresh_pages < 1:
            raise FullRefreshRequired("incremental refresh has no complete-sweep page budget")
        # ponytail: this overlap covers short offset-pagination races; the daily
        # complete sweep repairs misses outside two refresh intervals.
        lower_ms = max(0, high_water_ms - 2 * refresh_seconds * 1000)
        next_high_water_ms = high_water_ms
        catalog.begin_refresh()
        async with aclosing(
            client.asset_pages(
                session.owner_id,
                updated_after_ms=lower_ms,
                allow_duplicate_ids=True,
                page_limit=full_refresh_pages,
            )
        ) as pages:
            async for page in pages:
                for asset in page:
                    next_high_water_ms = max(
                        next_high_water_ms,
                        _updated_ms(asset.updated_at),
                    )
                catalog.stage(page)
        return catalog.finish_incremental(high_water_ms=next_high_water_ms)
