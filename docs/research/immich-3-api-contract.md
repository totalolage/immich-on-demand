# Immich 3.0.3 API contract

This report pins the client contract to Immich `v3.0.3`, commit `cd308ad93093735135f99d85ce6980c8e93df231`. Immich published that tag as a stable release, and the server package declares version `3.0.3`. [[release](https://github.com/immich-app/immich/releases/tag/v3.0.3)] [[package](https://github.com/immich-app/immich/blob/v3.0.3/server/package.json#L1-L7)]

"Verified" below means the behavior appears in the tagged OpenAPI document, server code, or first-party tests. "Recommendation" and "inference" mark conclusions for this client that Immich does not promise as an API contract.

## Decision

Use the stable REST endpoints for discovery, catalog reads, thumbnails, whole-original downloads, uploads, and trash. Do not use the internal timeline endpoints. Do not use the Sync API with an API key. Although Sync is marked stable, its service explicitly rejects every API-key caller with `403 Sync endpoints cannot be used with API keys`. [[sync controller](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/sync.controller.ts#L20-L36)] [[session requirement](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/sync.service.ts#L83-L95)]

Immich documents byte ranges only for the video playback endpoint, not for original downloads. The playback endpoint may return an encoded video instead of the original. Therefore 1.0 should download and atomically cache the complete original on the first content read. It should not implement partial hydration. [[media routes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset-media.controller.ts#L92-L177)] [[playback selection](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L296-L311)]

## Authentication and minimum permissions

Send the key in `x-api-key`. The tagged server also accepts `apiKey` as a query parameter, but a secret in a URL is liable to leak through logs and history, so this client should use the header only. [[header names](https://github.com/immich-app/immich/blob/v3.0.3/server/src/enum.ts#L19-L35)]

The permission sets are additive:

| Capability | Permissions |
| --- | --- |
| Connect, identify the configured user, enumerate, inspect metadata, preview, and download | `user.read`, `asset.read`, `asset.view`, `asset.download` |
| Upload new files | add `asset.upload` |
| Explicitly enabled remote trash | add `asset.delete` |

These are the permissions attached to the corresponding controllers. `asset.delete` checks ownership, while reads, views, and downloads can also authorize album or partner access. [[permission enum](https://github.com/immich-app/immich/blob/v3.0.3/server/src/enum.ts#L107-L131)] [[asset access rules](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/access.ts#L116-L149)]

`GET /api/api-keys/me` returns the current key's permission list and requires authentication but no additional permission. Setup should call it and report all missing scopes at once. `GET /api/users/me` requires `user.read` and supplies the configured user's ID, which the catalog can use to distinguish owned assets from partner assets. [[current key route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/api-key.controller.ts#L38-L46)] [[key response](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/api-key.dto.ts#L22-L30)] [[current user route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/user.controller.ts#L55-L63)]

Read-only testing can use the first row's four permissions. A separate key with `asset.upload` and `asset.delete` limits mutation tests to assets created for this project.

## Connection and version detection

All endpoint paths in the rest of this report are relative to the discovered API root.

1. Resolve `GET /.well-known/immich` against the user-entered server URL. In 3.0.3 it is public and returns `{"api":{"endpoint":"/api"}}`. The official CLI follows this discovery step. [[well-known controller](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/app.controller.ts#L9-L17)] [[CLI discovery](https://github.com/immich-app/immich/blob/v3.0.3/packages/cli/src/utils.ts#L67-L80)]
2. Call public `GET /server/version`. It returns `{major, minor, patch, prerelease}`. For the target server, accept exactly `3.0.3` during initial development and fail closed with an actionable message for a different major version. [[version route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/server.controller.ts#L76-L84)] [[version schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/server.dto.ts#L60-L72)]
3. Call public `GET /server/media-types` before accepting an upload and cache its response for the process lifetime. Call public `GET /server/features` before enabling trash and require `trash: true`. [[server routes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/server.controller.ts#L96-L103)] [[media-types route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/server.controller.ts#L127-L135)]

`GET /server/about` is unnecessary for compatibility detection and costs the extra `server.about` permission. Its optional build and source fields are diagnostic metadata, not a better version gate. [[about route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/server.controller.ts#L33-L41)]

## Complete catalog enumeration

Use stable `POST /search/metadata`, permission `asset.read`. [[search route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/search.controller.ts#L30-L39)]

Start with:

```http
POST /search/metadata
Content-Type: application/json
x-api-key: ...

{
  "page": 1,
  "size": 1000,
  "order": "asc",
  "withExif": true,
  "withDeleted": true,
  "withStacked": true
}
```

`size` accepts 1 through 1000. `withExif` includes `exifInfo.fileSizeInByte`. `withDeleted` is needed to observe trashed records. `withStacked: true` keeps stack members in the flat catalog. The request also supports `updatedAfter` and `updatedBefore`. [[search request schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/search.dto.ts#L10-L80)] [[file-size field](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/exif.dto.ts#L7-L18)]

Read `assets.items`, then parse `assets.nextPage` as the next 1-based page number. Stop on `null`. Do not use `assets.total` as the collection size. It is deprecated in v3.0.0, and the implementation fills it with the current page's item count. [[search response schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/search.dto.ts#L188-L207)] [[response mapping](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/search.service.ts#L86-L100)]

Each asset provides the fields needed by the flat filesystem catalog: `id`, `ownerId`, `originalFileName`, `originalMimeType`, `fileCreatedAt`, `fileModifiedAt`, `updatedAt`, `visibility`, `isTrashed`, `isOffline`, `checksum`, dimensions, duration, and optional EXIF. The checksum is Base64 text described as SHA-1. [[asset schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L61-L117)]

There are four boundaries to account for:

- An API-key search defaults to every non-locked visibility. This includes `hidden`, which Immich uses for the video half of Live Photos and Motion Photos. Filter `hidden` locally for the stated 1.0 scope. Locked assets require an elevated session and are unavailable through this scoped-key design. [[visibility values](https://github.com/immich-app/immich/blob/v3.0.3/server/src/enum.ts#L1150-L1158)] [[default visibility](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/search.service.ts#L86-L96)]
- With no album filter, search includes the configured user and partners whose timeline sharing is enabled. Filter on the ID returned by `/users/me` if the mount is meant to contain owned assets only. [[search owners](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/search.service.ts#L76-L84)]
- Pagination uses SQL `OFFSET` and orders only by `fileCreatedAt`. It has no snapshot token or ID tie-breaker. Inserts, trash operations, or other mutations during a scan can shift pages or reorder equal timestamps. [[search query](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/search.repository.ts#L197-L206)]
- Search excludes deleted assets unless `withDeleted` is true. Trashed date filters implicitly turn deleted records on. [[search builder](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/database.ts#L374-L395)]

Recommendation: deduplicate a sweep by asset ID, apply it to the catalog in one transaction only after the final page succeeds, and repeat a full sweep if the server changed during the run. This does not create snapshot isolation, but it prevents a failed half-scan from deleting good catalog rows.

## Incremental change detection

The scoped-key route is polling:

```json
{
  "page": 1,
  "size": 1000,
  "order": "asc",
  "withExif": true,
  "withDeleted": true,
  "withStacked": true,
  "updatedAfter": "<previous high-water timestamp minus overlap>"
}
```

`updatedAfter` is inclusive in the query. Keep an overlap, deduplicate by `(id, updatedAt)`, and advance the high-water mark only after every page commits. [[date filters](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/database.ts#L391-L398)]

This delta cannot report a record after Immich permanently removes it, and offset pagination can still race with concurrent changes. Run periodic full reconciliation and treat an asset as gone only after a complete successful sweep omits it. This is an inference from the query implementation, not an Immich guarantee.

WebSocket events are not a suitable scoped-key substitute. The socket authentication path supplies no route permission, which makes API-key authentication require the special `all` permission. That defeats the minimum-permission requirement. [[socket authentication](https://github.com/immich-app/immich/blob/v3.0.3/server/src/app.module.ts#L83-L94)] [[permission default](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/auth.service.ts#L218-L239)]

## Metadata lookup

Use stable `GET /assets/{id}`, permission `asset.read`, for a single authoritative refresh. It returns the same `AssetResponseDto` with EXIF, owner, faces, stack, edits, and tags loaded by the service. [[asset route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset.controller.ts#L92-L100)] [[asset lookup](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset.service.ts#L62-L80)]

Do not use `originalPath` as a client path. It is a server-local storage detail. Use the asset ID as remote identity and generate the flat mount name from `originalFileName` plus deterministic collision handling.

## Thumbnails and previews

Use stable `GET /assets/{id}/thumbnail?size=thumbnail&edited=false` for directory thumbnails. Use `size=preview` when a larger visual preview is needed. The valid sizes are `thumbnail`, `preview`, `fullsize`, and deprecated `original`; omission defaults to `thumbnail`. [[media option schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media.dto.ts#L8-L30)]

The response is binary with the derivative's actual content type, `Content-Disposition: inline`, and private cache headers. A missing derivative becomes `404`. [[thumbnail selection](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L247-L293)] [[file response](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/file.ts#L36-L82)]

Never request `size=fullsize` for Nautilus thumbnailing. For a web-supported original such as JPEG, PNG, or GIF, Immich redirects `fullsize` to `/original`, which requires `asset.download` and hydrates the original. `size=preview` does not take that redirect path. [[fullsize redirect](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L262-L281)]

Upload completion queues metadata extraction after the asset record and original are stored. A `201` upload response does not mean the preview job has completed, so a temporary thumbnail `404` is retryable. [[upload completion](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L182-L196)]

## Original download and byte ranges

Use stable `GET /assets/{id}/original?edited=false`, permission `asset.download`. `edited` defaults to false. The response is the original path selected by the server, with its MIME type and original base filename. [[download option](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset.dto.ts#L166-L170)] [[download implementation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L225-L244)]

The tagged OpenAPI operation documents only a `200 application/octet-stream` response. The controller does not claim byte-range support for originals. By contrast, `GET /assets/{id}/video/playback` explicitly documents byte ranges, but it chooses `encodedVideoPath || originalPath`. It is a playback representation, not an original-download endpoint. [[OpenAPI](https://github.com/immich-app/immich/blob/v3.0.3/open-api/immich-openapi-specs.json)] [[media controller](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset-media.controller.ts#L92-L177)]

The current original route delegates to the same Express `sendFile` helper as playback, so a deployed 3.0.3 server may honor a `Range` header. Immich does not document or test that behavior for originals. Treat it as an implementation detail. A later release may add partial hydration after a capability test and a fallback to full download.

Recommendation for 1.0: the first FUSE read starts one shared whole-file download into a temporary cache file. Readers wait for the required bytes or completion. Publish the cache entry with an atomic rename only after validation succeeds. A failed or interrupted response never becomes a complete cache entry.

## Integrity

For normal uploaded assets, Immich computes SHA-1 over the incoming bytes, stores the asset with `libraryId: null` and checksum algorithm `sha1`, and returns the checksum as Base64 in asset metadata. Its first-party end-to-end test confirms that an original download hashes to the catalog checksum. [[upload hashing](https://github.com/immich-app/immich/blob/v3.0.3/server/src/middleware/file-upload.interceptor.ts#L107-L136)] [[managed asset creation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L150-L168)] [[download integrity test](https://github.com/immich-app/immich/blob/v3.0.3/e2e/src/specs/server/api/asset.e2e-spec.ts#L678-L696)]

That checksum is not universally a content hash. External-library assets use SHA-1 over the string `path:<server path>`, and `AssetResponseDto` does not expose `checksumAlgorithm`. [[checksum algorithms](https://github.com/immich-app/immich/blob/v3.0.3/server/src/enum.ts#L47-L51)] [[external asset checksum](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/library.service.ts#L400-L415)]

For 3.0.3 managed assets, inferred from `libraryId: null`, compare `Base64(SHA1(downloaded bytes))` with `asset.checksum`. For external-library assets, validate successful response completion and byte count against `exifInfo.fileSizeInByte` when present. The API exposes no universal content digest, so stronger verification of external assets is impossible through this contract alone.

## Upload

Use stable `POST /assets`, permission `asset.upload`, with `multipart/form-data`:

| Field | Required | Value |
| --- | --- | --- |
| `assetData` | yes | non-empty file part with the real filename |
| `fileCreatedAt` | yes | ISO 8601 timestamp with timezone |
| `fileModifiedAt` | yes | ISO 8601 timestamp with timezone |
| `filename` | no | override for the multipart filename |
| `duration` | no | non-negative milliseconds for video |

The tagged DTO also accepts favorite, visibility, Live Photo, metadata, and sidecar fields. They are outside this application's 1.0 upload contract. [[upload DTO](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media.dto.ts#L32-L57)] [[required-field tests](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset-media.controller.spec.ts#L89-L143)]

Before upload, SHA-1 the complete local file and call `POST /assets/bulk-upload-check` with `{"assets":[{"id":"<local token>","checksum":"<hex or Base64 SHA-1>"}]}`. Each result says `accept` or `reject`; duplicate rejections include the existing `assetId` and whether it is trashed. The same `asset.upload` permission covers this endpoint. [[bulk-check DTO](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media.dto.ts#L59-L70)] [[bulk-check result](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media-response.dto.ts#L18-L49)] [[bulk-check implementation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L314-L341)]

Also send `x-immich-checksum` on `POST /assets`. If the checksum already exists for this user, Immich can return before reading the multipart body. If a concurrent upload reaches the database first, the unique-checksum path still returns the existing asset. [[upload route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset-media.controller.ts#L51-L89)] [[early duplicate check](https://github.com/immich-app/immich/blob/v3.0.3/server/src/middleware/asset-upload.interceptor.ts#L14-L25)] [[duplicate race handling](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L197-L217)]

Successful responses are:

- `201 {"status":"created","id":"<uuid>"}` for a new asset.
- `200 {"status":"duplicate","id":"<existing uuid>"}` for an existing checksum.

The controller and first-party tests assert both outcomes. [[response schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media-response.dto.ts#L4-L16)] [[upload test](https://github.com/immich-app/immich/blob/v3.0.3/e2e/src/specs/server/api/asset.e2e-spec.ts#L1004-L1025)]

This makes a retry after a lost response safe for the same user's identical bytes: either it creates once or resolves to the duplicate ID. This is an inference from the unique-checksum handling, not an explicit idempotency guarantee.

### Format acceptance

Downloads are opaque bytes and need no client format allowlist. Uploads do. Immich 3.0.3 decides whether an upload is an asset from the lowercase filename extension, not by inspecting its content. An unsupported extension returns `400 Unsupported file type <filename>`. [[upload filter](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L60-L90)] [[extension test](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/mime-types.ts#L133-L159)]

Despite the DTO description, public `GET /server/media-types` returns arrays of extensions with leading dots. In 3.0.3 these include `.gif`, `.jpeg`, `.jpg`, `.png`, `.m4v`, `.mov`, and `.mp4`, along with many other image and video extensions. Use the server response for upload admission instead of copying the list. [[image extensions](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/mime-types.ts#L42-L70)] [[video extensions](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/mime-types.ts#L106-L127)] [[response construction](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/server.service.ts#L168-L173)]

The application's JPEG, PNG, GIF, MP4, MOV, and M4V limit applies to local preview behavior only. It must not hide or reject other remote originals, and it should not reject an upload that the connected server reports as supported.

## Trash and restore

Use stable `DELETE /assets`, permission `asset.delete`, with `{"ids":["<uuid>"],"force":false}`. Omitted or false `force` marks assets as trashed and returns `204`. `force:true` marks them for permanent deletion and must never be sent by this application. [[delete route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset.controller.ts#L80-L90)] [[delete DTO](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset.dto.ts#L56-L58)] [[delete behavior](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset.service.ts#L370-L381)]

The access check covers every ID before the update. An unknown or unauthorized member makes the request fail with `400 Not found or no asset.delete access`; `asset.delete` only permits owned assets. [[access check](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/access.ts#L37-L41)] [[delete test](https://github.com/immich-app/immich/blob/v3.0.3/e2e/src/specs/server/api/asset.e2e-spec.ts#L531-L555)]

Before enabling the filesystem-to-remote-delete mapping, require the user's explicit opt-in and verify `/server/features` returns `trash: true`. When trash is disabled, the deletion job uses a retention period of zero days, so a nominal trash operation becomes eligible for physical deletion immediately. [[feature value](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/server.service.ts#L88-L109)] [[retention behavior](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset.service.ts#L273-L294)]

Recovery uses stable `POST /trash/restore/assets`, permission `asset.delete`, body `{"ids":[...]}`. It returns `200 {"count":N}`. This is useful for a recovery command, not for routine filesystem operation. [[restore controller](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/trash.controller.ts#L39-L49)] [[restore service](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/trash.service.ts#L12-L24)]

## Failure semantics

The OpenAPI file lists success responses for these operations but does not define typed error responses. Handle status codes first and treat response JSON as diagnostic text.

| Status | Verified meaning relevant here |
| --- | --- |
| `400` | Request validation failed, quota was exceeded, an extension is unsupported, or an asset ID is missing or inaccessible. Validation bodies use `{"message":"Validation failed","errors":[...]}`. |
| `401` | No credentials or an invalid API key. |
| `403` | The API key lacks the route permission. Sync also returns 403 because it requires a login session. |
| `404` | For original and thumbnail routes, the common file sender converts access, missing-file, and other pre-header failures to 404. Do not infer that the asset record itself is absent. |
| `200` | Search, metadata, thumbnail/original bytes, duplicate upload, bulk duplicate check, or restore, depending on the route. |
| `201` | New upload record and original accepted; derivative processing may still be pending. |
| `204` | Trash request accepted. |
| `500` | Unhandled server failure. |

Authentication distinguishes missing and invalid credentials, and permission failure names the missing scope. The global error handler preserves an error `message`, emits structured validation issues, removes redundant status fields, and adds `X-Correlation-ID`. [[authentication failures](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/auth.service.ts#L218-L270)] [[invalid key](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/auth.service.ts#L516-L526)] [[error response shape](https://github.com/immich-app/immich/blob/v3.0.3/server/src/middleware/global-exception.filter.ts#L24-L62)]

The upload path has a verified `400 Quota has been exceeded!` response. [[quota test](https://github.com/immich-app/immich/blob/v3.0.3/e2e/src/specs/server/api/asset.e2e-spec.ts#L1032-L1041)] File responses deliberately collapse failures to 404 before headers are sent. [[file sender](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/file.ts#L53-L82)]

## Contract tests to carry into implementation

These tests can run against a fixture server without touching the protected library:

1. Discovery returns an API root, version is exactly 3.0.3, and the key reports the expected scopes.
2. A paginated full search returns unique IDs and includes byte size when available.
3. `size=thumbnail` and `size=preview` do not redirect to `/original` for JPEG, PNG, GIF, MP4, MOV, or M4V fixture assets.
4. A complete managed-asset original matches both catalog size and Base64 SHA-1.
5. A fixture upload returns 201, a repeated upload returns 200 with the same asset ID, and the original downloads byte-for-byte.
6. With trash enabled, deleting only that fixture returns 204, a search with `withDeleted:true` reports it as trashed, and restore returns it.

Do not make the protected server's existing assets mutation fixtures. Range behavior on `/original` may be measured later, but 1.0 correctness must not depend on it.
