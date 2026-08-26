# Evaluate partial Hydration

Type: prototype
Status: resolved
Target: 1.4
Blocked by: none

## Answer

Do not add a sparse production cache in 1.4. Immich 3.0.3's current Express stack happens to serve single byte ranges for originals, and the playback endpoint explicitly supports them, but the original-download API does not promise that contract. Its generated file validator is a weak size-and-mtime ETag, not a content identity suitable for `If-Range`, and the deployed proxy path and actual player-read workload remain unmeasured.

Keep the complete-file cache and its existing atomic size and checksum checks. This is a source-level go/no-go decision; no live restart, proxy-upgrade, Immich-upgrade, or player-workload claim has been completed. See [the pinned analysis and bounded probe](../../../docs/research/immich-3-partial-hydration.md).

## Scope

Evaluate the pinned original and playback Range contracts, integrity boundary, and smallest bounded public-origin probe. Keep the complete-file cache unless the reopening criteria supply enough deployed evidence to justify a sparse prototype.

## Acceptance

- Pinned Immich 3.0.3 primary-source analysis records the original, playback, Range, `If-Range`, validator, checksum, proxy, and player boundaries.
- A sanitized read-only probe bounds every expected response to one byte beyond its declared range and rejects weak, malformed, or multiple entity tags as strong validators.
- The evaluation selects the existing atomic complete-file cache as the fallback and adds no sparse production code.

## Reopening criteria

- The public-origin probe remains stable across an Immich restart and every supported proxy or Immich upgrade.
- A seven-day Reference trace shows at least 10 GiB of avoidable original traffic and at least 75% of affected bytes left unread.
- The public origin supplies a strong validator bound to the original bytes or authenticated chunk hashes.
- A future prototype proves crash-safe overlapping reads, Eviction, and whole-file fallback without publishing incomplete or conflicting extents.
