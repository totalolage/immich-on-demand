# Add Asset replacement

Type: decision
Status: open
Target: 1.4
Blocked by: 08, 09

## Scope

Define what happens when an application writes, truncates, or replaces an existing entry. Immich originals are immutable, so changed bytes must create a new asset. Decide how metadata transfers, how conflicts appear, and whether the previous asset remains visible or moves to trash.

## Acceptance

- The documented transaction has a recoverable outcome for failures before upload, after upload, and before retiring the previous asset.
- Concurrent remote changes never cause silent data loss.
- Tests mutate only recorded Test assets and prove the old original is never overwritten in place.
- Common save patterns from image editors produce one understandable result.
