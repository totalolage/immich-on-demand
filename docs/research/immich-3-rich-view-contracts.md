# Immich 3.0.3 rich View contracts

This report is pinned to Immich `v3.0.3`, commit `cd308ad93093735135f99d85ce6980c8e93df231`. [[release](https://github.com/immich-app/immich/releases/tag/v3.0.3)]

## Decision

Materialize directories and View aliases inside the existing `Catalog`. The catalog already owns asset identity and refresh transactions, so a second write interface would split one invariant across two modules. FUSE reads the namespace through `node`, `lookup`, and `children`. Preview and URI controls use `aliases` when they need every mounted path for one asset.

Populate the index from complete, strictly validated inventories:

- `All`, `by Date`, and `Favorites` derive from the owned asset catalog.
- `Albums` uses the accessible album list and one metadata search per album, filtered back to cataloged assets owned by the configured user.
- `People` uses the non-hidden People inventory and one owned asset sweep with `withPeople: true`.

Do not set `isOwned: true` on the Album inventory. That would omit a shared-with-me album which contains assets owned by the configured user. Listing every accessible album and filtering each membership to owned catalog assets preserves those useful memberships without admitting another user's asset.

Publish Album and People facts only after two consecutive complete projections match. Immich exposes no relation cursor or snapshot token, and its offset pagination can move while it is read. Run this reconciliation at startup, every 24 hours, after a full asset reconciliation, and on manual refresh. A failed or unstable pass leaves the previous namespace intact.

The read key must add exactly `album.read` and `person.read` to the current read policy. `face.read` is unnecessary for this design. Change the exact-permission validator and persisted trusted-profile permission fingerprint in the same migration.

## Read contracts

All paths below are relative to the discovered `/api` root.

| View data | Endpoint and permission | Exact v3.0.3 contract |
| --- | --- | --- |
| Album inventory | `GET /albums`, `album.read` | Returns one unpaginated `AlbumResponseDto[]`. Optional filters are `id`, exact `name`, `isOwned`, `isShared`, and `assetId`. Each album has UUID `id`, `albumName`, `updatedAt`, `albumUsers`, and `assetCount`; it has no asset list. Empty albums are returned with `assetCount: 0`. [[route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/album.controller.ts#L28-L37)] [[schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/album.dto.ts#L66-L78)] [[response](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/album.dto.ts#L108-L141)] [[empty count](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/album.service.ts#L40-L67)] |
| Album membership | `POST /search/metadata`, API-key scope `asset.read`; an `albumIds` filter also checks resource-level `album.read` access | Send one album UUID in `albumIds`, plus `page`, `size: 1000`, `withDeleted: true`, and `withStacked: true`. Read `assets.items` and decimal `assets.nextPage` until `null`. Multiple album IDs mean membership in **all** supplied albums, not any album, so they cannot preserve per-album provenance. The route does not require a second API-key scope for the filter, but `album.read` is still needed for `GET /albums`. [[route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/search.controller.ts#L30-L40)] [[authorization](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/search.service.ts#L65-L99)] [[intersection query](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/database.ts#L253-L264)] [[response](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/search.dto.ts#L188-L207)] |
| People inventory | `GET /people`, `person.read` | Query pages from 1 with `size: 1000` and default `withHidden: false`. The default size is 500 and the maximum is 1,000. The response is `{total, hidden, people, hasNextPage?}`. Each person has UUID `id`, `name`, `isHidden`, and optional `updatedAt`. Follow `hasNextPage`; do not use `total` as a page checksum. [[route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/person.controller.ts#L49-L58)] [[schemas](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/person.dto.ts#L51-L87)] [[page response](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/person.dto.ts#L159-L172)] |
| Person membership | `POST /search/metadata`, `asset.read` | Add `withPeople: true` to a complete owned asset sweep. Each asset's optional `people` array contains each visible, non-deleted related person once, even if the asset has several faces for that person. Intersect those IDs with the People inventory to exclude hidden or otherwise unlisted people. [[request](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/search.dto.ts#L53-L80)] [[asset field](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L61-L117)] [[deduplication](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L165-L179)] [[face selection](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/database.ts#L216-L234)] |
| Favorite membership | Existing owned asset sweep, `asset.read` | `AssetResponseDto.isFavorite` is a required boolean. Compute `Favorites` locally from visible, owned catalog assets. `POST /search/metadata` also accepts `isFavorite: true`, but should be an acceptance cross-check rather than a second source of truth. [[asset response](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L90-L104)] [[filter](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/search.dto.ts#L10-L35)] [[query](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/database.ts#L468-L470)] |

`GET /faces?id=<asset UUID>` is a stable alternative for one asset. The route requires API-key scope `face.read`, then checks resource-level `asset.read` access internally. It returns face UUIDs and nullable person records, but using it for a library would add one request per asset. The `withPeople` sweep gives the membership needed by this View without that scope or N+1 cost. [[face route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/face.controller.ts#L34-L43)] [[access check](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/person.service.ts#L126-L133)]

## Identity, visibility, and empty collections

The durable identities are the album UUID, person UUID, and asset UUID. Names are labels, not keys. A person merge reassigns the source faces to the target UUID and deletes the source people; the target directory therefore survives while source directories disappear. [[merge implementation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/person.service.ts#L555-L608)]

`localDateTime` is a required string in `AssetResponseDto`, not optional EXIF data. Persist its calendar date in the asset facts and catalog. Immich defines it as the timezone-agnostic local capture time used for local day and month grouping. [[asset schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L25-L48)]

The namespace is an **owned-asset projection**:

- List every album accessible to the user, including shared and empty albums.
- Album-filtered search deliberately omits the normal owner filter. It can return another contributor's assets, so accept a membership only when the response UUID and owner match an existing catalog asset for the configured owner. An album may therefore appear empty locally even when its server `assetCount` is nonzero. [[owner bypass](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/search.service.ts#L76-L84)]
- The People query is owner-scoped, excludes hidden people by default, and requires a visible, non-trashed timeline asset through an inner join. It also omits an unnamed person until the person's face count reaches the user's configured minimum, which defaults to three. A person with no qualifying asset has no directory. [[People query](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/person.repository.ts#L150-L213)]
- Global catalog visibility remains authoritative. Trashed, offline, hidden, or size-less assets create no active aliases in any View. Membership facts may remain stored so a restored asset can reappear without changing identity.

Album names and person names may be empty strings. Use a local fallback such as `Unnamed`, then apply the normal collection-name collision policy. Do not manufacture an empty Person directory that `GET /people` did not return.

## Rename, delete, and mutation behavior

The rich Views are read-only in the first implementation. These tagged mutation contracts explain what a later refresh must observe:

| Change | Endpoint and permission | Namespace result |
| --- | --- | --- |
| Rename album | `PATCH /albums/{id}`, `album.update`, body `{"albumName":"..."}` | Observe the new label during paired reconciliation. Keep the first assigned mounted name and directory inode because the album UUID is unchanged. [[route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/album.controller.ts#L72-L86)] [[body](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/album.dto.ts#L56-L64)] |
| Delete album | `DELETE /albums/{id}`, `album.delete`, response 204 | Remove the directory and memberships only after a stable complete inventory omits the UUID. Although the route description mentions scheduled deletion, the tagged repository issues a database delete. [[route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/album.controller.ts#L88-L99)] [[repository](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/album.repository.ts#L372-L374)] |
| Add/remove album assets | `PUT` or `DELETE /albums/{id}/assets`, `albumAsset.create` or `albumAsset.delete` | Replace the album's membership set after a stable complete projection. Asset removal does not reliably advance the album's `updatedAt`, so it is not a relation cursor. [[routes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/album.controller.ts#L112-L150)] [[removal](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/album.service.ts#L263-L278)] |
| Rename person | Tagged OpenAPI exposes deprecated `PUT /people/{id}`; server v3 also implements OpenAPI-excluded `PATCH /people/{id}`, both `person.update` | Observe the new label during paired reconciliation. Keep the first assigned mounted name and directory inode. [[routes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/person.controller.ts#L105-L133)] |
| Delete person | `DELETE /people/{id}`, `person.delete`, response 204 | Remove the directory after a stable complete inventory omits it. Faces remain on assets with their person foreign key set to null. [[route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/person.controller.ts#L135-L145)] [[foreign key](https://github.com/immich-app/immich/blob/v3.0.3/server/src/schema/tables/asset-face.table.ts#L40-L55)] |
| Merge people | `POST /people/{target}/merge`, `person.merge`, body `{"ids":[...]}` | Preserve the target directory identity, move memberships there, and remove successful source IDs. [[route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/person.controller.ts#L190-L204)] |
| Toggle asset favorite | Tagged OpenAPI exposes deprecated `PUT /assets/{id}`; server v3 also implements OpenAPI-excluded `PATCH /assets/{id}`, both `asset.update`, body `{"isFavorite":true|false}` | Add or remove only the `Favorites` alias. Favorite is independent of the local Pin state. [[routes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset.controller.ts#L141-L168)] [[body](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset.dto.ts#L9-L54)] |

## Reconciliation safety

Metadata search uses SQL `OFFSET`, orders only by `fileCreatedAt`, and has no snapshot token or UUID tie-breaker. The People list also uses offset pagination. Its final ordering key is `createdAt`, not UUID. Both scans can miss or duplicate rows during concurrent edits. [[asset pagination](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/search.repository.ts#L197-L206)] [[People pagination](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/person.repository.ts#L150-L213)]

There is no safe `updatedAfter` shortcut for relationships. Album asset removal does not update the album in the tagged service, and face assignment changes an `asset_face` row rather than the asset row. Run a complete relation projection twice and compare canonical sets of:

- `(album_id, album_name)` and `(album_id, owned_asset_id)`
- `(person_id, person_name, is_hidden)` and `(person_id, owned_asset_id)`

Reject duplicate IDs, invalid pages, foreign owner claims, dangling collection references, or conflicting labels. Publish the second projection in one local transaction only when both canonical sets match. Matching pairs reduce offset races but are not a server snapshot guarantee. Never remove a prior collection or membership because one partial scan omits it.

## Catalog namespace interface

The common caller should not know View rules or SQL:

```python
class Catalog:
    def node(self, inode: int) -> Node | None: ...
    def lookup(self, parent_inode: int, name: str) -> Node | None: ...
    def children(self, parent_inode: int) -> tuple[DirEntry, ...]: ...
    def aliases(self, asset_id: str) -> tuple[Alias, ...]: ...
```

`Node` is either a directory or an asset-backed file. It includes the inode, the node kind, the mode data, and `nlink`. `DirEntry` contains the mounted name and the node.

The catalog derives `All`, `by Date`, and `Favorites` while it commits asset facts. Album and People adapters pass complete validated collection and membership facts into the existing refresh transaction. The catalog returns only after the projection and asset rows commit together.

Typical FUSE use stays short:

```python
node = catalog.lookup(parent_inode, decoded_name)
for entry in catalog.children(directory_inode):
    reply(entry.name, entry.node)
asset = catalog.node(inode).asset
```

The refresh coordinator validates remote responses before it calls the catalog's existing commit methods. No lookup or listing path performs HTTP, Hydration, thumbnail work, or relation reconstruction.

### Invariants

- Root inode 1 survives migration. Import every existing asset inode into a shared inode registry before allocating directory inodes; never reserve fixed directory numbers that could collide with current asset inodes.
- Every active alias for one asset points to the existing `CatalogAsset.inode`. Opening any alias therefore shares content-cache, Pin, Preview, and mutation identity.
- A file's `nlink` equals its active alias count across all Views. A directory's `nlink` is `2 + immediate child directory count`.
- Asset aliases always reuse the catalog's persisted global filename. A later filename collision cannot rename an existing asset in any View.
- Directory identity uses `(view, stable collection ID)`, not its label. The first mounted name remains stable after a server rename. Deleted identities remain tombstoned, and the catalog never reassigns their inodes or names.
- Each `(parent, name)` and `(parent, asset_id)` is unique. Repeated faces or repeated source facts cannot create duplicate aliases.
- `by Date` uses `localDateTime`'s calendar date, matching Immich's documented local timeline grouping, with stable synthetic IDs for year, month, and day directories. [[timestamp meaning](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L35-L48)]

Persist collection names after applying the existing filename safety rules: NFC normalization, control and slash replacement, UTF-8 byte truncation, and deterministic collision suffixes. Use `Unnamed` for an empty label. Keep the first persisted mounted name. Name tombstones prevent a deleted collection's old name from moving a surviving entry when another collection arrives.

### Errors

- `lookup` returns `None` for a missing child. `node` returns `None` for an unknown or stale inode.
- `children` raises `NotADirectoryError` for a file inode and returns an immutable per-call snapshot for a directory. FUSE maps absence and the wrong node kind to `ENOENT` and `ENOTDIR`.
- The catalog rejects malformed or contradictory facts with one fixed error. Its transaction rolls back, so readers retain the last complete projection.
- Remote availability, authorization, and response-schema errors belong to the Immich adapter. They do not mutate the catalog.

### Hidden implementation and dependencies

Keep the module concrete. It needs SQLite tables for directory identities, name tombstones, memberships, materialized directory entries, and active link counts. Existing asset rows remain the shared file identity. The catalog uses the current model and name helpers and needs no provider registry.

Dependencies point inward in four categories:

1. Immich adapters validate endpoint-specific JSON and emit facts.
2. The service coordinator schedules paired reconciliation and serializes it with catalog refresh.
3. `Catalog` owns names, identities, memberships, transactions, and queries.
4. FUSE and desktop adapters translate nodes and errors without embedding View logic.

Materializing aliases costs one small database row per visible occurrence, which can be much larger than the asset count for heavily tagged libraries. It buys constant-depth FUSE operations, atomic namespace swaps, exact link counts, and no network work during listing. Full paired relationship scans also cost more requests, especially one search per album, but the v3.0.3 API has no cursor that can replace them safely.

## Acceptance checks

Use read-only target checks until designated Test assets exist:

1. Compare `Albums` to two matching `GET /albums` plus per-album searches after filtering to the configured owner and catalog visibility.
2. Compare `People` to two matching People inventories plus `withPeople` asset sweeps.
3. Compare `Favorites` to owned catalog `isFavorite` values and a separate `isFavorite: true` search filtered to the configured owner.
4. Pick one asset in several Views and verify every path reports the same inode, the expected `nlink`, and identical Pin and hydration state.
5. Confirm an empty album remains as an empty directory, unnamed people receive safe names, and duplicate labels do not move an existing directory.
6. Trace listing and lookup to prove they issue no Immich or content-download request.

Mutation acceptance must later use only recorded Test asset, album, and person UUIDs. It should cover an album rename, membership removal, person rename or merge, and favorite toggle, then verify that identities and unrelated protected-library records remain unchanged.
