# Establish the Immich 3.0.3 API contract

Type: research
Status: resolved
Blocked by:

## Question

Which supported Immich 3.0.3 APIs and permissions cover complete asset enumeration, metadata, thumbnails, original and ranged downloads, new-asset upload, trash, change detection, version detection, and integrity checks, and what failure or compatibility behavior must 1.0 handle?

## Answer

Use the stable REST routes documented in [Immich 3.0.3 API contract](../../../docs/research/immich-3-api-contract.md). Scoped API keys cannot use the Sync API, original Range reads are not guaranteed, uploads are checksum-deduplicated, and deletion must always request trash with `force: false`.
