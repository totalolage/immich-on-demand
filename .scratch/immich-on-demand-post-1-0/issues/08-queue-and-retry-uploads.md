# Queue and retry uploads

Type: feature
Status: implemented
Target: 1.2
Blocked by: 05, 07

## Scope

Turn retained recovery bytes into explicit Pending uploads with status, retry, and cancel operations. Keep upload attempts idempotent through Immich's upload checksum guard and a per-job metadata marker. A successful close means the local Pending bytes are durable, not that the remote upload succeeded; later upload failure remains visible through status.

## Acceptance

- A file admitted while online can close after connectivity is lost; its locally durable Pending job keeps the requested name, and a failed upload attempt records a fixed retryable error.
- Reconnection or an explicit retry creates at most one Immich asset.
- Cancel removes only local Pending bytes after explicit confirmation.
- Status survives service restart and is visible through CLI, Nautilus, and GUI clients.

## Answer

One private queue under `$XDG_DATA_HOME/immich-on-demand/uploads` owns Pending bytes. Cache limits and Eviction never touch it. FUSE `flush` and `fsync` make the local payload durable; final release seals it as Pending and wakes one service-owned uploader without waiting for HTTP. An interrupted write remains blocked Upload recovery and is never retried automatically.

Each upload retries `POST /api/assets` with the same SHA-1 checksum and a canonical queue-job marker in Immich asset metadata. The advisory bulk-upload check is not retry authority. A created or duplicate response is accepted only after `asset.read` verifies the trusted owner, checksum, and exact marker. Ambiguous or mismatched results keep the private bytes.

Retry advances one Pending or retryable blocked job. Cancel requires the job UUID, its current revision, and its exact requested name; it never calls Immich and refuses a job that may be committing remotely. The daemon exposes bounded pages, a Pending count in status, and fixed error codes to CLI and desktop clients. Staged names remain readable as local overlays in `All`; Nautilus opens the desktop manager from the mount background menu for queue status, Retry, and confirmed Cancel.

New file creation still requires an online-validated mutation key. A file admitted online may finish after connectivity is lost and becomes Pending. Allowing new writes when the service starts offline is a separate policy decision.

## Remaining acceptance

- Exercise crash points around seal, attempt, candidate publication, catalog publication, and cleanup.
- On the Reference system, use only a new Test asset to prove lost-response retry creates at most one Immich asset.
- Confirm Pending status, Retry, and confirmed Cancel in the packaged CLI, Nautilus menu, and desktop application.
- Confirm that an upgrade preserves any version 1.0 recovery bytes; automatic import is deliberately deferred.
