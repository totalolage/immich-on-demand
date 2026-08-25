# Immich 3.0.3 incremental refresh

This report is pinned to Immich `v3.0.3`, commit `cd308ad93093735135f99d85ce6980c8e93df231`. [[release](https://github.com/immich-app/immich/releases/tag/v3.0.3)]

## Decision

Use `POST /search/metadata` for routine, upsert-only delta scans. Query from the last committed high-water timestamp minus an overlap, include deleted assets, and cap the scan at the page count of the last full sweep. Never remove a catalog row because it is absent from a delta scan.

Run a full reconciliation at startup, every 24 hours, after a delta exceeds its page budget, and on manual repair. Accept absence only when two consecutive complete full sweeps return the same owned-asset ID set. This is the smallest safe design available to an API-key client. Immich's Sync service rejects API keys even when the key has Sync permissions. [[Sync restriction](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/sync.service.ts#L83-L95)]

## Search is inclusive but not cursor-ordered

Both timestamp bounds are inclusive: Immich implements `updatedAfter` as `asset.updatedAt >= value` and `updatedBefore` as `asset.updatedAt <= value`. [[date filters](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/database.ts#L391-L398)] Keep the lower bound inclusive. Do not add one millisecond to the stored high-water timestamp.

The `order` field does not order by `updatedAt`. Metadata search orders only by `fileCreatedAt`, applies `LIMIT size + 1`, and paginates with SQL `OFFSET`. It has no ID tie-breaker or snapshot token. Inserts and state changes can move records across page boundaries during a scan. Equal `fileCreatedAt` values also have no defined relative order. [[search pagination](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/search.repository.ts#L197-L206)]

`updatedBefore` does not fix that race. A closed timestamp interval is still read through separate offset queries, and a transaction can commit into the interval after an earlier page has been read. The REST contract cannot provide a lossless incremental cursor.

## `updatedAt` is not a complete record version

Updates to the `asset` table run a trigger that replaces `updatedAt` with the database clock. [[asset trigger](https://github.com/immich-app/immich/blob/v3.0.3/server/src/schema/tables/asset.table.ts#L23-L30)] [[trigger function](https://github.com/immich-app/immich/blob/v3.0.3/server/src/schema/functions.ts#L38-L50)] The API converts that value through JavaScript `Date.toISOString()`, which has millisecond precision. Two database updates can therefore have the same returned timestamp. [[date mapping](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/date.ts#L4-L9)] [[date codec](https://github.com/immich-app/immich/blob/v3.0.3/server/src/validation.ts#L147-L163)]

Some asset responses can change without any asset-table update. `AssetService.update()` writes description, rating, location, and capture-time fields to `asset_exif`; when the request has no asset-table fields, the following asset repository call only reads the asset. The response can therefore contain changed EXIF data with an identical asset `updatedAt`. [[asset update split](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset.service.ts#L95-L128)] [[no-op asset update](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/asset.repository.ts#L619-L648)] [[bulk update split](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset.service.ts#L130-L174)]

Consequences for the client:

- Treat each returned asset as authoritative and upsert its complete catalog projection.
- Deduplicate repeated pages by asset ID only, with the later response winning.
- Do not skip an asset because `(id, updatedAt)` matches the catalog.
- Use a full sweep to discover EXIF-only changes that never enter an `updatedAfter` result.

## State coverage

Routine deltas must send `withDeleted: true`. Without it, the query excludes every row whose `deletedAt` is non-null. [[deleted filter](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/database.ts#L374-L375)] [[deleted exclusion](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/database.ts#L488-L491)]

| Change | Delta result | Required handling |
| --- | --- | --- |
| Upload or asset-table edit | Returned after the asset trigger advances `updatedAt` | Upsert the record. |
| Trash | Returned with `isTrashed: true` because trash updates `deletedAt` on the asset row | Keep it in the catalog but hide it from the mount. [[trash write](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset.service.ts#L370-L381)] |
| Restore | Returned with `isTrashed: false` because restore clears `deletedAt` on the asset row | Upsert and expose it again. [[restore write](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/trash.repository.ts#L14-L23)] |
| External asset goes offline or online | Returned because library refresh updates `isOffline` and `deletedAt` on the asset row | Upsert and apply the visibility rule. [[offline writes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/library.service.ts#L547-L562)] |
| Timeline, archive, or hidden visibility change | Returned because `visibility` is an asset-table field | Upsert and apply the visibility rule. |
| Asset becomes locked | Not returned to an API-key search | Remove only after stable full reconciliation. API-key searches default to `visibility != locked`. [[visibility guard](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/search.service.ts#L65-L99)] |
| Permanent deletion or external-library row removal | No record remains for search to return | Remove only after stable full reconciliation. Physical removal deletes the asset row. [[physical removal](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset.service.ts#L312-L337)] |
| EXIF-only edit | May retain the same asset `updatedAt` and never match the delta lower bound | Repair on the next full reconciliation. |

## Bounded delta algorithm

Persist the high-water value as UTC epoch milliseconds derived from returned asset `updatedAt` values. Do not use the client clock. Use zero after a successful empty full sweep. Also persist the page count from the last successful full sweep. Then run each routine refresh as follows:

1. Set `lower` to `high_water - 2 * refresh_seconds`. With the current five-minute interval, this is a ten-minute overlap. The overlap covers ordinary late commits and small clock corrections, but it is not a correctness guarantee.
2. Request pages of 1,000 with `updatedAfter: lower`, `withDeleted: true`, `withExif: true`, and `withStacked: true`. Keep the existing owner-ID filter.
3. Stage responses in an ID-keyed map. If an ID repeats because pages shifted, replace its earlier value.
4. Stop and schedule a full reconciliation if the delta needs more pages than the last full sweep. Also stop on an invalid `count`, `nextPage`, or response item.
5. After the terminal page succeeds, atomically upsert every staged record. Do not delete catalog rows absent from the staged map.
6. Advance `high_water` to the maximum of its previous value and every returned `updatedAt`. If the scan fails, leave both the catalog and high-water value unchanged.

This policy bounds a routine delta to no more requests than the known cost of a full sweep. A quiet library still returns the inclusive boundary records, but normally fits in one page.

Do not require two identical delta scans. A busy, live offset query may not produce a stable pair, and matching scans still cannot prove that an absent record was deleted. The overlap and later full reconciliation repair missed delta records. Require a stable pair only before a full reconciliation removes absent catalog rows.

## Full reconciliation remains the correctness path

Run this reconciliation at startup and every 24 hours. Also run it after a delta reaches its page budget, after response validation fails, and when the user requests repair. Run two back-to-back full sweeps with the same options but without timestamp bounds. Filter both to the configured owner, and compare their ID sets. If the sets differ or either sweep fails, keep the current catalog and retry later. If the sets match, atomically replace the catalog projection with the second sweep. Replace the high-water value with the sweep's maximum `updatedAt`, or zero for an empty set. A complete sweep is authoritative and may move the cursor backward after the server timeline changes.

The full sweep repairs four cases that deltas cannot prove: permanent deletion, transition to locked visibility, EXIF-only edits, and records missed by offset pagination. Two matching sweeps reduce the chance of deleting a valid row because pages shifted, but Immich does not guarantee snapshot consistency. The client must therefore keep remote deletion separate from reconciliation. Removing a local catalog row must never issue an Immich delete request.
