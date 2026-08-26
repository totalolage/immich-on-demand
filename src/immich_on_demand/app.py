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


async def reconcile_album_people(
    catalog: Catalog,
    client: ImmichClient,
    session: ServerSession,
    catalog_lock: trio.Lock,
    *,
    trusted_profile: TrustedProfile | None = None,
) -> None:
    if trusted_profile is not None and (
        trusted_profile.owner_id != session.owner_id
        or trusted_profile.server_version != session.version
    ):
        raise ValueError("trusted profile does not match the validated server session")
    async with catalog_lock:
        visible_ids = {
            entry.asset.id
            for entry in catalog.list_visible()
            if entry.asset.owner_id == session.owner_id
        }
        previous = None
        for _ in range(MAX_REFRESH_SWEEPS):
            albums = tuple(sorted(await client.albums(), key=lambda item: item.id))
            album_memberships: set[tuple[str, str]] = set()
            for album in albums:
                async with aclosing(
                    client.asset_pages(session.owner_id, album_id=album.id)
                ) as pages:
                    async for page in pages:
                        album_memberships.update(
                            (album.id, asset.id)
                            for asset in page
                            if asset.owner_id == session.owner_id
                            and asset.id in visible_ids
                        )

            people = tuple(sorted(await client.people(), key=lambda item: item.id))
            person_ids = {person.id for person in people}
            person_memberships: set[tuple[str, str]] = set()
            async with aclosing(
                client.asset_pages(session.owner_id, with_people=True)
            ) as pages:
                async for page in pages:
                    for asset in page:
                        if (
                            asset.owner_id != session.owner_id
                            or asset.id not in visible_ids
                        ):
                            continue
                        person_memberships.update(
                            (person_id, asset.id)
                            for person_id in asset.person_ids
                            if person_id in person_ids
                        )

            album_relations = tuple(sorted(album_memberships))
            person_relations = tuple(sorted(person_memberships))
            snapshot = (
                tuple((album.id, album.name) for album in albums),
                album_relations,
                tuple(
                    (person.id, person.name, person.is_hidden) for person in people
                ),
                person_relations,
            )
            if snapshot == previous:
                catalog.replace_album_people(
                    albums=albums,
                    album_memberships=album_relations,
                    people=people,
                    person_memberships=person_relations,
                    trusted_profile=trusted_profile,
                )
                return
            previous = snapshot
        raise ImmichError(
            "Immich album and people lists did not stabilize after "
            f"{MAX_REFRESH_SWEEPS} complete sweeps"
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
