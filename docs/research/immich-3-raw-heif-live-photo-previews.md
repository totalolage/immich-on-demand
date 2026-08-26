# Immich 3.0.3 RAW, HEIF, and Live Photo preview contracts

This report is pinned to Immich `v3.0.3`, commit `cd308ad93093735135f99d85ce6980c8e93df231`. [[release](https://github.com/immich-app/immich/releases/tag/v3.0.3)]

## Decision

Broaden Preview eligibility for every Immich image, while keeping the existing selected video types. Eligibility should test whether the asset's original MIME type starts with `image/`; it should not enumerate RAW camera MIME strings. This remains safe because Immich On-Demand requests the generated `preview` derivative, streams at most 32 MiB, and decodes that derivative under Pillow's decompression-bomb checks. It never opens the mounted original to make a Preview.

Treat a Live Photo as one visible still asset with a nullable link to one motion-video component. Persist `livePhotoVideoId` with the asset facts and carry it through every full or incremental refresh and View rebuild. Suppress the referenced component from the namespace regardless of its server visibility. Do not expose it as a second file or call it a sidecar: Immich reserves the sidecar media type for XMP.

The first slice does not animate a Live Photo, combine its components on open, or upload a new Live Photo pair. It shows the still's generated Preview and preserves enough server identity for later paired download, export, or playback.

## Server preview contract

Immich 3.0.3 classifies HEIC, HEIF, HIF, and a broad set of camera RAW extensions as images. Its public supported-formats page lists HEIC, HEIF, RAW, and RW2, while the tagged MIME table is the complete executable list. The response MIME types include `image/heic`, `image/heif`, `image/hif`, and camera-specific `image/*` values. [[format documentation](https://github.com/immich-app/immich/blob/v3.0.3/docs/docs/features/supported-formats.md#L1-L28)] [[tagged MIME table](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/mime-types.ts#L4-L70)]

`GET /assets/{id}/thumbnail?size=preview` retrieves an `AssetFileType.Preview` file. The response content type comes from the generated file's extension, not the original asset's extension. A missing generated Preview returns `404 Asset media not found`. The endpoint never falls back from `preview` to the original. [[size contract](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media.dto.ts#L8-L17)] [[endpoint](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset-media.controller.ts#L112-L160)] [[lookup and response](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L247-L293)]

Immich generates Preview and Thumbnail files from a decoded image. For RAW assets, the administrator may prefer an embedded JPEG or JPEG XL preview when it is present and large enough; otherwise Immich decodes the original RAW. The configured Preview format is JPEG or WebP. The v3.0.3 defaults are 1,440 pixels, JPEG, and quality 80. Embedded-preview quality is camera-dependent, so source inspection cannot establish that a particular user's RAW Preview is useful. [[generation pipeline](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/media.service.ts#L250-L343)] [[embedded extraction](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/media.repository.ts#L71-L90)] [[defaults](https://github.com/immich-app/immich/blob/v3.0.3/server/src/config.ts#L369-L389)] [[operator guidance](https://github.com/immich-app/immich/blob/v3.0.3/docs/docs/administration/system-settings.md#L25-L59)]

The local Preview path already supplies the required isolation:

- `ImmichClient.thumbnail` requests only `size=preview`, rejects content encoding, and stops the stream above 32 MiB.
- `install_thumbnail` rejects another over-limit input, invalid images, and decompression bombs before atomically installing a PNG.
- `populate_previews` installs failure records before network requests and catches failures per asset, so one missing or invalid derivative does not stop other jobs.

Before this change, `PREVIEW_MIME_TYPES` admitted three image MIME types and selected video MIME types. An `image/` predicate matches Immich's own asset classification without duplicating its camera matrix. Keep the video allowlist because a `video/` prefix alone would promise useful still-frame generation for every video container without representative evidence.

## Live Photo relationship contract

`AssetResponseDto` includes nullable `livePhotoVideoId`, including sanitized responses, and the normal mapper copies the database value into every asset response. The sync schemas also carry the same nullable field. It is therefore stable refresh data, not a relationship that the client must infer from names or timestamps. [[response schema and mapper](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L28-L48)] [[normal response mapping](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L212-L234)] [[sync schemas](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/sync.dto.ts#L60-L108)]

Immich's mobile client uploads the motion video first with `visibility=hidden`, then uploads the still with the returned video UUID in `livePhotoVideoId`. The server validates that the linked asset exists, is a video, and has the same owner. When an existing target has `visibility=timeline`, the server changes it to `hidden`; it does not force every other visibility to `hidden`. The asset table stores the relation as a nullable self-referencing foreign key with `ON DELETE SET NULL`. [[mobile upload](https://github.com/immich-app/immich/blob/v3.0.3/mobile/lib/services/foreground_upload.service.dart#L335-L360)] [[link validation and visibility](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/asset.util.ts#L145-L187)] [[stored relation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/schema/tables/asset.table.ts#L84-L96)]

The upload API also names this relation directly. Its separate `sidecarData` part is not the motion video: Immich's sidecar MIME table accepts only `.xmp`. [[upload schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media.dto.ts#L38-L56)] [[XMP sidecar definition](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/mime-types.ts#L129-L161)]

Immich On-Demand records `livePhotoVideoId` as one nullable UUID field across API validation, both asset tables, and every refresh path. The visible still keeps its inode and all View aliases. The referenced motion component remains cataloged for integrity checks but has no active alias, even if its server visibility is not `hidden`.

## Smallest safe implementation boundary

Implement one narrow slice:

1. Replace the original-image MIME allowlist with `mime_type.lower().startswith("image/")`; retain the existing explicit video MIME types.
2. Parse `livePhotoVideoId` as either a canonical UUID or `null`. Persist it in the catalog's live and incoming asset tables, migrations, row mapping, comparison logic, and full and incremental refreshes.
3. Suppress every asset referenced by `livePhotoVideoId` from `All` and every derived View. Do not depend on the component's visibility. Do not synthesize a `.mov` neighbor, bundle directory, or XMP sidecar.
4. Fetch only the still asset's generated Preview. Opening or hydrating that visible path continues to return only its original still bytes.

This boundary changes no upload or original-download protocol. Immich already accepts the listed formats as normal image uploads, and an original download remains an opaque byte stream. Uploading paired Live Photo components and exporting both components are separate features.

## Acceptance boundary

Source contracts justify implementation, but representative files must justify format claims. On the designated Immich 3.0.3 Test account, record UUIDs for one `.heic`, one `.heif`, one `.dng`, and one camera-native RAW such as `.cr3`, `.nef`, `.arw`, or `.raf`. For each asset:

1. Confirm Immich has generated `size=preview` and returns JPEG or WebP no larger than 32 MiB.
2. Open its mounted directory in Nautilus and confirm a useful Preview appears without an original-asset request, Hydration, or content-cache entry.
3. Repeat with one absent derivative and one malformed or over-limit fake response. Each must keep its failure record and must not stop unrelated Preview jobs.
4. Download and upload one representative original byte-for-byte to confirm those paths remain format-agnostic.

For one Test Live Photo, record the visible still UUID and `livePhotoVideoId`. After a full refresh, an incremental refresh, and each rich-View rebuild, verify that the still keeps the same linked UUID and inode in every alias. Verify that the referenced motion component has no mounted path regardless of its server visibility. Confirm that the still Preview needs no request for the motion UUID. Composite playback and paired export are outside this acceptance run.
