# Add Asset replacement

Type: feature
Status: implemented
Target: 1.4
Blocked by: 08, 09

## Scope

Replace an existing `All` entry through the temp-file rename-over pattern. Immich originals are immutable, so changed bytes create a new asset. Direct writes and truncation of an existing asset remain read-only.

## Acceptance

- The documented transaction has a recoverable outcome for failures before upload, after upload, and before retiring the previous asset.
- Concurrent remote changes never cause silent data loss.
- Tests mutate only recorded Test assets and prove the old original is never overwritten in place.
- Common save patterns from image editors produce one understandable result.

## Answer

Immich 3.0.3 cannot replace an original's bytes through an existing asset UUID. A replacement uploads and verifies a new managed asset through the durable queue, copies only album organization, then trashes the old asset with `force: false`. The old asset remains live until the new checksum, owner, and upload marker have been verified. A lost upload or trash response is reconciled by reading the recorded UUIDs.

The candidate gets a new inode because inode identity follows the Immich asset UUID. While the replacement is Pending, the `All` overlay exposes the local payload through a private high inode. This inode is not the candidate's future inode. Publication invalidates the `All` entry and exposes the candidate's newly allocated durable inode. One catalog transaction gives the stable mounted name, Pin, album membership, and every View alias to the candidate. Open handles to the old inode keep the old bytes. The catalog retains the old UUID and inode as trashed under a deterministic collision name, which Restore later exposes.

Only the All View accepts rename-over. Direct writes and truncation of an existing hardlinked inode remain `EROFS`: FUSE does not receive the alias path at `open(inode)`, so it cannot distinguish All from a derived View without abandoning shared-inode semantics. Temp-file rename converts the queued source into a replacement job. Pending payloads remain locally readable while upload, metadata copy, and retirement continue.

The worker waits one second before admitting a newly sealed ordinary upload. This delay covers the close-then-rename save sequence. A rename that arrives after admission returns `EBUSY`; add an explicit publish syscall only if Reference-system applications need a longer window.

Concurrent source-record or album changes block retirement and leave the old asset live. Immich has no conditional or atomic copy-and-trash operation, so the transaction uses reversible trash and keeps both UUIDs in durable recovery state until local publication completes. Live acceptance may touch only newly recorded Test assets, never the Protected library.

## Remaining acceptance

- Build and install version 1.4 from the development Arch recipe on the Reference system.
- Replace a newly uploaded, recorded Test asset through `All` and verify the stable name, new inode, transferred Views and Pin, copied albums, and old UUID in trash.
- Restart during a replacement and verify that the durable job resumes without publishing both assets.
- Restore the old Test UUID and verify that it appears under the recorded collision name with its old inode.
