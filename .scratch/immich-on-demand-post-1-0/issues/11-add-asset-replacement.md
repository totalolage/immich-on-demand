# Add Asset replacement

Type: decision
Status: resolved
Target: 1.4
Blocked by: 08, 09

## Scope

Define what happens when an application writes, truncates, or replaces an existing entry. Immich originals are immutable, so changed bytes must create a new asset. Decide how metadata transfers, how conflicts appear, and whether the previous asset remains visible or moves to trash.

## Acceptance

- The documented transaction has a recoverable outcome for failures before upload, after upload, and before retiring the previous asset.
- Concurrent remote changes never cause silent data loss.
- Tests mutate only recorded Test assets and prove the old original is never overwritten in place.
- Common save patterns from image editors produce one understandable result.

## Answer

Immich 3.0.3 cannot replace an original's bytes through an existing asset UUID. A replacement uploads and verifies a new managed asset through the durable queue, copies only album organization, then trashes the old asset with `force: false`. The old asset remains live until the new checksum, owner, and upload marker have been verified. A lost upload or trash response is reconciled by reading the recorded UUIDs.

The candidate gets a new inode because inode identity follows the Immich asset UUID. After the old UUID is confirmed trashed, one catalog transaction gives the old mounted name and Pin state to the candidate and replaces every View alias. Open handles to the old inode keep the old bytes. Restore brings the old UUID back under a deterministic collision name.

Only the All View accepts writes, truncation, and rename-over. Partial writes first hydrate the complete old original. Temp-file rename converts the queued source into a replacement job. Pending payloads remain locally readable while upload, metadata copy, and retirement continue.

Concurrent source-record or album changes block retirement and leave the old asset live. Immich has no conditional or atomic copy-and-trash operation, so the transaction uses reversible trash and keeps both UUIDs in durable recovery state until local publication completes. Live acceptance may touch only newly recorded Test assets, never the Protected library.
