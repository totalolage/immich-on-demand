# Queued upload recovery

Status: design recommendation
Date: 2026-08-26
Examined baseline: Immich On-Demand development tree, Immich 3.0.3, pyfuse3 3.5, Linux

## Decision

Make a queued upload a durable local job before any network request. Keep its bytes under `$XDG_DATA_HOME/immich-on-demand/uploads`, not the cache. The XDG specification reserves the data directory for user-specific data and calls cache data non-essential. An upload that has not reached Immich may be the user's only copy. [[XDG base directories](https://specifications.freedesktop.org/basedir/0.8/)]

Use one worker and retry the stable `POST /assets` operation with the same SHA-1 checksum and job marker. Do not make `POST /assets/bulk-upload-check` part of correctness. Keep the local job until the remote asset has been verified and published in the catalog.

This design does not claim that `close()` reports remote success. FUSE cannot provide that contract. A successful write means the bytes were accepted locally. Flush or fsync makes the current recovery copy durable. The Pending state and its fixed error code report remote progress.

## What Immich 3.0.3 guarantees

The upload route accepts an optional `x-immich-checksum` SHA-1 header. It returns `201` for a created asset and `200` for a duplicate. Both bodies contain a UUID and a literal status, either `created` or `duplicate`. [[upload controller](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset-media.controller.ts#L51-L90)] [[response schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media-response.dto.ts#L4-L16)]

The checksum interceptor checks the current user's upload library before it accepts the multipart file. A concurrent insert is also safe: the database has a unique index on owner and checksum for the upload library, and the service converts that constraint violation into the same duplicate response. Two overlapping attempts can therefore create at most one upload-library asset. [[checksum interceptor](https://github.com/immich-app/immich/blob/v3.0.3/server/src/middleware/asset-upload.interceptor.ts#L14-L25)] [[upload service](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L127-L221)] [[checksum indexes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/schema/tables/asset.table.ts#L31-L41)]

`bulk-upload-check` is not a reservation or an idempotency endpoint. Its input `id` is only echoed to match a response, and the schema sets no batch-size limit. Its query searches every asset owned by the user without restricting `libraryId`; the upload checksum check searches only `libraryId IS NULL`. If the same bytes exist in an external library, bulk check may reject an upload that the upload endpoint would accept. If several library rows have the checksum, the bulk service reduces them to one unordered map entry. [[bulk schema](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media.dto.ts#L59-L70)] [[bulk implementation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L314-L341)] [[repository queries](https://github.com/immich-app/immich/blob/v3.0.3/server/src/repositories/asset.repository.ts#L663-L684)]

A lost HTTP response is still ambiguous. POST is not idempotent by definition, and HTTP permits an automatic retry only when the client knows the resource semantics make it safe or can detect whether the first request applied. [[HTTP semantics](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2)]

Bind that ambiguity to one queue job. Send one upload metadata item:

```json
{"key":"immich-on-demand.upload","value":{"formatVersion":1,"uploadId":"<job UUID>"}}
```

Immich accepts metadata in the upload body, inserts it after creating the asset, and exposes it through an `asset.read` GET route. Version 3 removed the old `deviceId` and `deviceAssetId` fields, so they cannot act as client idempotency keys. [[upload DTO](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-media.dto.ts#L38-L57)] [[metadata insertion](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L150-L172)] [[metadata read](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset.controller.ts#L171-L179)] [[removed device fields](https://github.com/immich-app/immich/blob/v3.0.3/server/src/schema/migrations/1776263790468-DropDeviceIdAndDeviceAssetId.ts#L3-L10)]

On `201 created`, require the exact response schema, then verify owner, checksum, and marker through the read client. On `200 duplicate`, adopt the returned asset only when the same verification finds this job's marker. A missing or different marker means identical content already existed or Immich partially committed an earlier request. Mark the job Blocked and retain its bytes. Do not POST it again. This conservative case is necessary because asset creation and marker insertion are separate database calls in 3.0.3.

## Smallest correct state machine

Each job is one private directory named by a canonical random UUID. It contains fixed names `payload` and `manifest.json`. The manifest is the queue authority, not `catalog.db` and not a filename recovered by scanning arbitrary paths.

| Persisted state | Meaning | Next action |
| --- | --- | --- |
| `writing` | At least one FUSE handle may still change the payload. | Final release seals it. After daemon restart, change it to `blocked` with `interrupted-write`; never upload it automatically. |
| `pending` | Payload, size, SHA-1, timestamps, and manifest are durable. | The worker may attempt it when the validated Profile is online. |
| `attempting` | The manifest was committed before POST. The response may be lost. It may also hold a candidate remote UUID from a strict response. | With no candidate, retry the same checksum-marked POST. With a candidate, perform verification only and never POST until it resolves. |
| `committed` | Owner, checksum, and marker verification succeeded for the candidate remote UUID. | Fetch authoritative asset metadata, publish it to the catalog, then delete the local job. Never POST again. |
| `blocked` | Local corruption, interrupted writing, permanent server rejection, Profile mismatch, or a duplicate with the wrong marker. | Keep bytes until explicit Retry is valid or Cancel is confirmed. |
| `cancelled` | The user confirmed local deletion before remote commit. | Finish deleting the job after a crash. |

The normal path is `writing -> pending -> attempting -> committed -> removed`. Persist a candidate UUID immediately after an exact `201 created` or `200 duplicate` response. Verification outages retain that candidate and retry only the read. A duplicate with a wrong marker becomes Blocked. A transient availability failure or retryable response changes an attempt with no candidate back to `pending`, with a fixed error code and next-attempt time. Any upload response with a malformed code/body pair is ambiguous, so the next action uses the same checksum-marked POST rather than assuming failure. A `committed` job survives catalog or process failure and reconciles without another upload.

Cancel accepts only `pending` or `blocked`. It must refuse `writing`, `attempting`, and `committed`. Cancel never calls Immich. A job that may already have committed remotely is not a local cancellation problem.

Persist these fields and no secret: format version, job UUID, canonical server origin, owner UUID, requested name, state, byte size, lowercase SHA-1 hex, frozen creation and modification timestamps, attempt count, next attempt time, fixed error code, and optional remote asset UUID. Bind every attempt to the currently validated origin and owner. Keep raw exception text, URLs, headers, API keys, and response bodies out of the manifest.

## FUSE boundary

pyfuse3 calls `flush` for a descriptor close and may call it several times for one open file. libfuse also warns that flush may be absent and cannot identify the final write. `release` identifies the last reference to a file handle, but its error is discarded because no client request remains. [[pyfuse3 flush](https://pyfuse3.readthedocs.io/en/latest/operations.html#pyfuse3.Operations.flush)] [[pyfuse3 fsync](https://pyfuse3.readthedocs.io/en/latest/operations.html#pyfuse3.Operations.fsync)] [[libfuse flush and release](https://github.com/libfuse/libfuse/blob/master/include/fuse_lowlevel.h#L618-L672)] [[pyfuse3 release](https://pyfuse3.readthedocs.io/en/latest/operations.html#pyfuse3.Operations.release)]

Therefore:

- `write` records local I/O failures immediately.
- `flush` and `fsync` sync the current payload and repeat any prior local error. They do not seal or upload it.
- The final application-level handle release fsyncs the payload, freezes its size and hash, atomically publishes `pending`, and wakes the worker. It does no HTTP work.
- If final release cannot seal the job, it retains `writing` bytes and records a blocked recovery item when possible. The kernel cannot return that failure from release, so desktop notification and Pending status are the honest reporting path.

This is the narrow meaning of "never hide a failure behind a successful close": the service never labels a queued file Uploaded or deletes recovery bytes because close returned. It cannot make applications observe a release error that FUSE discards.

## Durable local operations

Require an absolute `$XDG_DATA_HOME` path, then open the queue root through a directory file descriptor. Require a real directory owned by the user with mode `0700`. Use `openat`-style relative operations with fixed basenames, `O_NOFOLLOW | O_CLOEXEC`, and `O_EXCL` for creation. Require job directories to be owned `0700` directories and payloads/manifests to be owned `0600` regular files with link count one. Linux documents that `O_NOFOLLOW` protects the final path component and that directory-relative APIs avoid path-prefix replacement races. [[XDG path rules](https://specifications.freedesktop.org/basedir/0.8/)] [[Linux open](https://man7.org/linux/man-pages/man2/open.2.html)]

For every manifest change, write a same-directory temporary file, cap it at 4096 bytes, fsync it, atomically replace `manifest.json`, then fsync the job directory. Linux guarantees atomic replacement by rename, while `fsync` on the file alone does not make its directory entry durable. [[Linux rename](https://man7.org/linux/man-pages/man2/rename.2.html)] [[Linux fsync](https://man7.org/linux/man-pages/man2/fsync.2.html)]

Before publishing `pending`, fsync the payload, calculate SHA-1 from the retained descriptor, freeze its `fstat` size and timestamps, publish the manifest, then fsync the job and queue-root directories. Reopen and revalidate type, owner, mode, link count, size, and SHA-1 before every attempt. Never upload an invalid job. Temporary files and malformed directories are quarantined in status and never deleted automatically.

After catalog publication or confirmed Cancel, publish `cancelled` when applicable, unlink only the two fixed files, fsync the job directory, remove the UUID directory, and fsync the queue root. Startup finishes an interrupted `cancelled` cleanup and a cataloged `committed` cleanup.

The queue root must support regular-file and directory `fsync`. If either returns an unsupported-operation error during an admission probe, disable queued uploads. Atomic rename without the directory sync is not enough for the promised crash recovery.

## Exact first-slice limits

- One worker and one in-flight upload. Order jobs by sealed time, then job UUID.
- One asset per request. Do not call or batch `bulk-upload-check`.
- Retry only typed reachability and timeout failures, HTTP 408, 425, 429, and 5xx after 5, 10, 20, 40, then at most 60 seconds, indefinitely. TLS, protocol, authentication, permission, schema, and other 4xx failures become Blocked. A manual Retry wakes one job early but does not bypass those checks.
- Preserve the existing HTTP timeouts: 10 seconds for connect and pool acquisition, 30 seconds for read and write inactivity. There is no whole-file wall-clock timeout.
- No arbitrary item-count or file-size cap. Before each extending write, require enough backing-filesystem space to keep the configured `minimum_free_bytes`; otherwise return `ENOSPC`. Queue bytes do not count toward `cache_max_bytes` and no eviction path may remove them.
- Cap a manifest at 4096 bytes and a requested mount name at 255 UTF-8 bytes. Store only fixed error codes.
- Offline-start creation remains out of this smallest slice. An upload already admitted while online can become Pending during an outage and survives restart. Enabling new writes in a degraded mount needs a separate policy because the mutation key is intentionally unvalidated there.

## Acceptance boundaries

Automate crashes after every durable step: job directory creation, payload fsync, Pending publication, attempt publication, remote response, remote-ID publication, catalog commit, and each cleanup unlink. Every restart must expose either a recoverable local job or the cataloged asset, never silently lose both.

Race two attempts with the same job and two jobs with identical bytes. Assert one remote asset at most. Exercise an existing upload-library duplicate, an external-library-only duplicate, a trashed duplicate, a lost `201` response, a lost `200` response, malformed success JSON, server failure after asset creation but before marker insertion, and a marker mismatch. Only the matching marker may turn a duplicate into this job's success.

For FUSE, test repeated flush, flush followed by more writes, explicit fsync, final release, daemon death before release, release failure, and applications that ignore close errors. For local storage, inject failure before and after each file and directory fsync, reject symlinks and hard links, and prove Cancel deletes only the selected job's fixed files.

On the target Immich server, use only a newly created Test asset. Drop the client connection after the server accepts its upload, restart the service, and verify that retry resolves the matching marker without a second asset. Then cancel a separate never-attempted Pending job and confirm that Immich was not called.
