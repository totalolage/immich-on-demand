# Immich 3.0.3 asset replacement

Status: implemented in source; Reference-system acceptance pending
Date: 2026-08-26
Examined baseline: Immich On-Demand development tree, Immich 3.0.3, pyfuse3 3.5

## Decision

Treat the supported temp-file rename-over pattern as a queued replacement, not an in-place update. Upload and verify a new managed asset first. Copy only the selected server metadata, trash the old asset with `force: false`, then replace the local namespace entry in one catalog transaction. Direct writes and truncation of an existing asset remain `EROFS`.

The mounted name moves to the new asset. The inode does not. A remote asset UUID keeps one catalog inode for its lifetime, so the new Immich UUID receives a new inode across every View. Open handles to the old inode continue to read the old content. This matches rename-over semantics and preserves the one-inode-per-asset rule.

Never retire the old asset before the new upload, its ownership, its checksum, and its upload marker have been verified. If any later step is ambiguous, keep the durable job and both remote UUIDs. The old asset stays live unless Immich already confirms that it is trashed.

## The API cannot replace original bytes in place

The complete asset-media controller exposes one multipart write, `POST /assets`, and describes it as uploading a new asset. Its remaining routes download or view media and check checksums. It has no route that accepts bytes for an existing UUID. [[asset-media routes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset-media.controller.ts#L43-L193)]

`PUT /assets/{id}` updates favorite state, visibility, capture time, location, rating, description, and Live Photo linkage. `UpdateAssetDto` has no file field, checksum field, or filename field. The editing routes store crop, rotation, and mirror actions. They do not replace the original. [[update route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset.controller.ts#L129-L155)] [[update schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset.dto.ts#L9-L54)] [[editing routes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset.controller.ts#L215-L248)]

The public v3.0.3 contract therefore cannot keep the old UUID when bytes change. This conclusion is an inference from the tagged route set and request schemas, not a separate Immich immutability guarantee.

## Server operations and limits

| Step | Pinned contract | Replacement rule |
| --- | --- | --- |
| Create | `POST /assets`, `asset.upload`. The request accepts the bytes, filename, creation and modification timestamps, favorite state, visibility, and custom metadata. It returns `201 created` or `200 duplicate`. | Reuse the queued-upload checksum and `immich-on-demand.upload` marker contract. Preserve the old capture time, favorite state, visibility, and mounted filename. Use the staged file time as `fileModifiedAt`. [[upload schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media.dto.ts#L32-L57)] |
| Verify | `GET /assets/{id}` and `GET /assets/{id}/metadata`, `asset.read`. The asset response includes owner, library, checksum, `updatedAt`, favorite state, visibility, and trash state. | Require the configured owner, `libraryId: null`, the staged SHA-1, and exactly one matching job marker. [[asset response](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L61-L117)] |
| Copy organization | `PUT /assets/copy`, `asset.copy`, response `204`. The request can copy albums, shared links, stack linkage, favorite state, and a sidecar. | Send every boolean explicitly. Set only `albums: true`; set `favorite`, `sharedLinks`, `stack`, and `sidecar` to false. Favorite state was set during upload. [[copy route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset.controller.ts#L94-L103)] [[copy schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset.dto.ts#L154-L164)] |
| Retire | `DELETE /assets`, `asset.delete`, body `{"ids":["<old UUID>"],"force":false}`, response `204`. The service marks the record as trashed. | Require a fresh literal `trash: true` feature response. Never send `force: true`. Read the old UUID through `GET /assets/{id}` after a lost or successful response; the tagged lookup includes trashed rows. [[delete route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset.controller.ts#L73-L82)] [[delete schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset.dto.ts#L56-L58)] [[delete behavior](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset.service.ts#L370-L381)] [[asset lookup](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset.service.ts#L62-L92)] |

Immich creates the asset record before it inserts custom upload metadata. A lost response can therefore leave a candidate without the marker. The existing queue already handles this case by retaining the local payload and blocking on a missing or different marker. [[upload implementation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L127-L221)]

Checksum deduplication is owner-scoped and limited to managed upload assets. It does not exclude trashed rows. A replacement whose bytes match any managed asset can therefore return that asset's UUID, including the old UUID or a trashed UUID. Only the same job marker can adopt a duplicate. A different marker blocks the replacement and leaves the old asset live. [[checksum lookup](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/asset.repository.ts#L673-L684)] [[early duplicate response](https://github.com/immich-app/immich/blob/v3.0.3/server/src/middleware/asset-upload.interceptor.ts#L14-L25)]

Before upload, compare the staged SHA-1 with the old managed asset checksum. Treat an exact match as a local no-op. This comparison is valid only when `libraryId` is null because managed uploads use a file SHA-1, while external-library assets use a path checksum. [[managed checksum](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L150-L168)] [[external checksum](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/library.service.ts#L400-L415)]

## Metadata policy

Preserve the user-facing organization that has a narrow, replay-safe contract:

- Send the old `isFavorite` and `visibility` values in the upload request.
- Copy album membership with `PUT /assets/copy` and every option set explicitly.
- Let Immich extract EXIF, dimensions, duration, faces, and thumbnails from the new bytes.
- Do not copy shared links. Replacement must not grant access to a new UUID.
- Do not copy stacks, Live Photo linkage, sidecars, arbitrary custom metadata, tags, descriptions, ratings, locations, or person assignments in the first implementation.

The copy implementation performs album insertion, shared-link insertion, stack changes, a favorite update, and sidecar file work as separate calls. It does not copy tags despite the controller description. With only `albums: true`, album insertion uses conflict-ignore behavior and is safe to replay after a lost `204`. [[copy implementation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset.service.ts#L183-L226)] [[album copy query](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/album.repository.ts#L458-L469)]

The replacement mutation key needs the exact core read scopes plus `asset.upload`, `asset.copy`, and `asset.delete`. `asset.copy` and `asset.delete` both require ownership of every supplied asset ID. Shared, partner, and foreign assets cannot pass those checks. [[permission names](https://github.com/immich-app/immich/blob/v3.0.3/server/src/enum.ts#L121-L131)] [[ownership rules](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/access.ts#L143-L153)]

## Durable replacement transaction

Extend the existing upload manifest with an operation kind, the old asset UUID, its catalog inode and name, and a source fingerprint. The fingerprint contains owner, library, checksum, `updatedAt`, capture time, favorite state, visibility, and a paired stable album-membership set. Store the candidate UUID in the same place that ordinary queued uploads use.

Use the existing `writing`, `pending`, and `attempting` phases. After candidate verification, a replacement enters one new `replacing` phase. Recovery can replay that phase from its recorded IDs:

1. Verify the candidate owner, checksum, and upload marker.
2. Fetch the old asset and a paired stable album projection. If the source fingerprint changed, block the job and leave the old asset live.
3. Copy album membership with all copy options explicit. Re-read both album sets. If either set changed or the target does not match, block the job.
4. Fetch the old asset again. If its record changed, block the job.
5. Fetch `/server/features` again and require literal `trash: true`. The server reads this value from current configuration without its config cache. [[feature implementation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/server.service.ts#L88-L109)]
6. Trash only the old UUID with `force: false`.
7. Confirm that the old UUID is trashed. A missing response does not prove failure.
8. In one catalog transaction, publish the candidate, move the old mounted name to it, transfer Pin state, and replace every View alias. Mark the old row trashed and assign it a deterministic collision name for a later Restore.
9. If the old asset was Pinned, move the live cache Pin to the candidate and hydrate its verified original before completion. Then mark the job committed, remove its recovery files, and dismiss the staged namespace overlay. An unpinned candidate populates the content cache on its first original read. Reusing the queue payload as a cache entry is deferred until that optimization has independent capacity and crash semantics.

The new UUID receives a new inode. The old UUID keeps its old inode and any cached original. Open handles continue to read the old bytes. The catalog name transfer is a narrow exception to immutable mounted names. It applies only to an explicit replacement pair after the old asset is confirmed trashed. If the user restores the old asset, it returns under its recorded collision name instead of displacing the replacement.

An active replacement suppresses both automatic catalog publication of the candidate and resurrection of the old aliases. The durable manifest remains the suppression authority until the catalog transaction commits and the worker removes the job. Local Cancel refuses a job after a candidate may exist remotely. Keep Both is not implemented.

## Crash and concurrency outcomes

| Failure point | Recoverable outcome |
| --- | --- |
| Before seal or upload | The old asset and namespace entry are unchanged. The queue retains the local bytes. |
| After upload with no response | The checksum and upload marker resolve the candidate. A marker mismatch blocks without trashing the old asset. |
| After candidate verification or album copy | The old asset remains live. The candidate stays suppressed and the `replacing` job replays its checks. |
| During old trash | A fresh read decides whether the old UUID is live or trashed. The client never guesses from a missing `204`. |
| After old trash but before catalog publication | The manifest still names both UUIDs. Recovery confirms the server state and repeats the local transaction. |
| After catalog publication but before cleanup | The committed catalog is authoritative. Recovery completes any required Pinned-original hydration, then commits and removes only that job's fixed recovery files. |

Immich exposes no conditional delete, entity tag, or atomic copy-and-trash route. The delete request contains only IDs and `force`. The copy and delete operations are separate controller calls. [[delete schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset.dto.ts#L56-L58)] [[copy and delete routes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset.controller.ts#L73-L103)] A remote metadata change can race after the final read. The design cannot claim serializable replacement. It prevents silent byte loss by creating and verifying the new original first, using reversible trash only, retaining both UUIDs in durable state, and never using permanent deletion. If a source record or album set changes before retirement, the job blocks and leaves the old asset live.

## FUSE and Nautilus contract

Allow replacement only through the `All` View. Return `EROFS` for writes through Album, People, Favorites, or by Date aliases.

Support the path-aware save form: `create` plus writes and `rename` over an existing All entry converts the source upload job into a replacement job. Serialize rename with upload-attempt admission. The worker defers a newly sealed ordinary upload for one second to cover close-then-rename saves. A rename that arrives after admission returns `EBUSY`; an explicit publish syscall is deferred until Reference-system applications need a longer window. A replacement retry with a recorded candidate keeps that candidate instead of uploading again. Direct `open`, `setattr`, or `ftruncate` of an existing asset remains `EROFS`: POSIX hardlinks share one inode and one mode, while pyfuse3 supplies only that shared inode at the open boundary, so it cannot distinguish All from a derived View without abandoning hardlink semantics.

When rename identifies the target before upload admission, send the target name as the remote filename. If the temp asset already exists remotely, its remote `originalFileName` stays unchanged because Immich exposes no filename update. The mounted target name still remains stable.

pyfuse3 passes truncation through `setattr`, not through the `open` flags. Its rename contract says that an existing destination points to the moved source inode while the dereferenced inode remains valid until its lookup count reaches zero. Those rules support a new inode at the stable destination name. [[pyfuse3 open and setattr](https://pyfuse3.readthedocs.io/en/latest/operations.html#pyfuse3.Operations.setattr)] [[pyfuse3 rename](https://pyfuse3.readthedocs.io/en/latest/operations.html#pyfuse3.Operations.rename)]

Keep sealed and replacing payloads visible at the target name in `All` through a local namespace overlay. Reads use the durable payload while remote work continues. Existing handles to the old inode keep the old bytes, and derived Views keep the old aliases until the catalog transaction. The overlay uses a private high inode. This inode is not the candidate's future inode. The catalog assigns the verified candidate a new durable inode when it moves every View alias. `flush` and `fsync` promise only local durability; final `release` seals the job and cannot report remote completion. Pending status reports later failure. [[pyfuse3 flush, fsync, and release](https://pyfuse3.readthedocs.io/en/latest/operations.html#pyfuse3.Operations.release)]

After the catalog swap, the completion callback invalidates each candidate alias entry and dismisses the `All` overlay. Nautilus then sees the stable name with the candidate's permanent inode. A blocked replacement remains visible through the existing upload status and Retry controls. Keep Both is not part of 1.4.

## Acceptance boundary

Automated tests cover rejected in-place mutation, open and sealed temp-file rename-over, durable manifest recovery, upload verification, album-copy replay, guarded trash, atomic catalog publication, stable open handles, and completion cleanup. Source-level crash recovery covers a candidate upload and a catalog publication that completed before queue cleanup.

Reference-system acceptance must cover unchanged bytes, a matching-marker retry, a duplicate with a different marker, album-copy replay, source metadata change, album membership change, a lost `204`, trash disabled, Restore after replacement, and restart during replacement. Every blocked case must keep the old asset live unless Immich already confirms trash.

Run live acceptance only on newly uploaded, recorded Test assets. Record the old UUID, the candidate UUID, the old original SHA-1, the mounted name, and both inodes. Prove that:

1. Immich still downloads the old original byte-for-byte after the candidate upload.
2. A successful replacement gives the stable mounted name to the candidate's new inode.
3. The old UUID is in trash and can be restored under its collision name.
4. A concurrent favorite or album change blocks retirement and leaves the old UUID live.
5. No Protected asset UUID appears in an upload, copy, trash, restore, or catalog-mutation log.

Do not run replacement acceptance against the existing Protected library. Create, record, and remove only project-owned Test fixtures.
