# Offline startup trust contract

Status: implemented in the development tree; target acceptance pending
Date: 2026-08-26
Examined baseline: Immich On-Demand development tree, Immich 3.0.3, Arch Linux

## Decision

Persist one read-profile validation record inside `catalog.db`, not in a JSON sidecar. Publish that record in the same SQLite transaction that publishes a successful complete catalog reconciliation. SQLite transactions make the record and catalog projection change together; a separate file would need a second generation protocol to prevent a crash from pairing one Profile's record with another Profile's catalog. [[SQLite transactions](https://www.sqlite.org/lang_transaction.html)] [[atomic commit](https://www.sqlite.org/atomiccommit.html)]

An offline start is permitted only after a transport-availability failure and only when the current settings, current Secret Service read key, validation record, and nonempty catalog match exactly. The mounted service is degraded and read-only: it lists the trusted catalog and reads locally complete originals, but it performs no HTTP download, Preview fetch, Upload, Trash, Restore, or automatic cache eviction. A single background supervisor revalidates and completes a full reconciliation before enabling network reads or any mutation.

This fits the existing ownership boundaries. The [Catalog](../../src/immich_on_demand/catalog.py) already owns persistent names, inodes, refresh state, and transactions. The [service](../../src/immich_on_demand/service.py) alone loads keys, validates sessions, and decides when to mount. The [filesystem](../../src/immich_on_demand/filesystem.py) already rejects create before staging when no mutation session exists. The [content cache](../../src/immich_on_demand/content_cache.py) already validates complete local originals, but needs an offline gate before its download path.

## Persisted record

Add one schema-versioned row with these exact fields:

| Field | Contract |
| --- | --- |
| `format_version` | Literal `1`; unknown versions fail closed. |
| `server_origin` | The exact normalized HTTPS origin used by the client, including an explicit nondefault port. |
| `owner_id` | Canonical UUID returned by `GET /users/me`. |
| `server_version` | Literal validated version `3.0.3`. |
| `read_permissions` | Canonically sorted exact set: `asset.download`, `asset.read`, `asset.view`, `user.read`. |
| `read_key_sha256` | Lowercase SHA-256 of the UTF-8 Secret Service value. Compare with `hmac.compare_digest`; never log it. |

Immich 3.0.3 creates an API-key token from 32 random bytes and stores its SHA-256 rather than the token. A local SHA-256 therefore binds the exact high-entropy Secret Service value without persisting a usable bearer credential. The record omits the key ID, update time, and a local validation timestamp because none adds offline authority beyond the exact key fingerprint and the atomic catalog commit. [[key creation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/api-key.service.ts#L11-L27)] [[key response](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/api-key.dto.ts#L22-L36)]

The four stored read scopes are the exact set already required for user identity, metadata search, Preview access, and original download. [[user route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/user.controller.ts#L55-L63)] [[search route](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/search.controller.ts#L30-L39)] [[media routes](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset-media.controller.ts#L92-L177)]

Do not persist the mount path, cache policy, mutation secret or digest, media types, or the trash feature as offline authority. The first two do not identify the remote library. Mutation is always disabled offline. On reconnection, validate the current mutation key's exact policy-derived permissions and owner before installing a mutation session. Persisting its old validation would enable nothing safely.

Secret Service lookup attributes are case-sensitive strings and are normally stored unencrypted, so keep credentials and the key digest out of attributes. Continue to load exactly one read item and compare the loaded secret to the protected catalog record. A locked, missing, duplicate, or unavailable Secret Service item makes offline startup fail closed. [[Secret Service attributes](https://specifications.freedesktop.org/secret-service/latest/lookup-attributes.html)] [[locked items](https://specifications.freedesktop.org/secret-service/latest/unlocking.html)]

The record describes the last observation. A server administrator can revoke the key or change its scope while the client cannot reach the server; no offline design can observe that remote change. The degraded mount remains non-networked, then discovers the change before network access resumes.

## Commit and file safety

Write the record only in `Catalog.finish_refresh()`, after the stable complete sweep has staged successfully, in the same transaction that replaces the asset projection and refresh cursor. Never create or advance it after validation alone, a partial sweep, or an incremental refresh. An interrupted sweep then leaves the previous catalog and matching record authoritative.

Keep the database under `$XDG_STATE_HOME/immich-on-demand`; XDG defines this directory for state reused across restarts and requires configured base paths to be absolute. A relative `XDG_STATE_HOME` must be ignored in favor of the default or rejected, not used relative to the process directory. [[XDG Base Directory specification](https://specifications.freedesktop.org/basedir/0.8/)]

Before this database becomes a trust root, harden its existing open path:

- Require the state directory to be a real directory owned by the user with mode `0700`.
- Open `catalog.db` with `O_NOFOLLOW | O_CLOEXEC`; then require a regular file owned by the user, link count one, and mode `0600` through `fstat`.
- Apply the same regular-file, owner, link-count-one, and `0600` checks to existing `-wal` and `-shm` files before SQLite opens them. The private directory prevents other users from replacing newly created auxiliaries.
- Require `PRAGMA quick_check` to return exactly `ok` before offline use. It performs the principal formatting and consistency checks in linear time. [[SQLite quick check](https://www.sqlite.org/pragma.html#pragma_quick_check)]

On Linux, `O_NOFOLLOW` rejects a symlink in the final path component, while `O_CLOEXEC` prevents descriptor leakage across execution. The current `/proc/self/fd/<fd>` SQLite open can continue after these descriptor checks. [[Linux `open(2)`](https://man7.org/linux/man-pages/man2/open.2.html)]

For this single-Profile slice, an existing record also locks the catalog and cache to its server origin and owner. Refuse a different origin or owner even when online; require an explicit future Profile-reset or multi-Profile operation to select distinct state. An online read-key rotation for the same origin and owner may replace the credential fields only after exact validation and a complete sweep.

## Startup decision

Use this order:

1. Validate settings and load exactly one read key from Secret Service. Compute its fingerprint in memory.
2. Secure-open the catalog and read the profile record. A new database is not trusted.
3. Attempt normal read-key validation with a bounded startup budget.
4. On online success, require any existing record's origin and owner to match, run the stable complete reconciliation, commit the refreshed record atomically, then mount online. A new database establishes its first record. A read-key rotation for the same library is allowed; a different origin or owner is refused.
5. Only a typed availability failure with no usable HTTP response may enter degraded startup. Require exact record matches for origin, key digest, target version, owner ID, and read scopes. Also require `quick_check == ok`, a completed full-refresh marker, at least one catalog row, and every asset row to belong to the recorded owner.
6. Authentication or permission errors, any HTTP response error, invalid discovery or response schema, unsupported version, Secret Service failure, unsafe state files, and TLS verification failure do not qualify. Fail before mounting.

HTTPX separates response status failures from transport failures, but certificate verification is reported as `ConnectError`. Introduce a narrow `ImmichUnavailableError` at the HTTP boundary for timeouts and reachability errors that produced no response, explicitly excluding an `ssl.SSLError` cause, proxy errors, protocol errors, and TLS verification failures. Catch only that type for offline fallback. [[HTTPX exception hierarchy](https://www.python-httpx.org/exceptions/)] [[HTTPX TLS verification](https://www.python-httpx.org/advanced/ssl/)]

## Degraded behavior and recovery

Mount the same stable catalog names and inodes with `online: false` and `mutation_enabled: false` in status. A complete cached original remains readable after its normal local size, mtime, and checksum validation. A missing or invalid local original fails promptly with one stable offline error and makes no HTTP request. “Pinned” is not proof that bytes exist; only a locally complete pinned original can be read offline.

Skip initial Preview downloads, persisted-Pin hydration, periodic refresh, and automatic policy Eviction while degraded. Existing valid desktop thumbnails remain usable. Explicit local Pin, Unpin, status, and Evict may remain available; Upload creation must fail before a staging directory is made, while Trash and Restore must fail before an Immich call.

Run one retry supervisor immediately after mounting, then after 5, 10, 20, 40, and at most 60 seconds between attempts. A manual Refresh wakes it early; coalesce duplicate wakes. Each attempt follows this order:

1. Validate the read key and require the live origin, owner, version, and exact scopes to match the trusted record.
2. Load and validate the optional mutation key against the current policy, exact scopes, and the same owner.
3. Complete a stable full reconciliation and atomically refresh the record.
4. Enable original downloads, mutation methods, and the normal refresh, Preview, and Pin workers.

Network loss during validation or the full sweep leaves the mount degraded and retries. An authoritative identity, scope, version, TLS, schema, authentication, or mutation-key mismatch never overwrites trusted state and terminates the mount with a sanitized error. Normal incremental refresh starts only after the full recovery sweep.

## Acceptance

Automate these boundaries before a target test:

1. Unreachable startup mounts only for an exact record, safe nonempty catalog, completed full sweep, and matching current read-key fingerprint.
2. Missing records, empty catalogs, partial sweeps, corrupt databases, symlinks, hard links, wrong owner or mode, relative XDG state paths, and every record-field mismatch fail before FUSE initialization.
3. HTTP errors, malformed responses, invalid TLS, wrong versions, permission changes, and Secret Service failures never select offline fallback.
4. Offline cached reads perform zero HTTP calls. Uncached reads fail promptly. Create leaves no staging file; Trash and Restore make no request. Status remains responsive and reports degraded mode.
5. Retry backoff is bounded and coalesced. Recovery validates identity, completes a full sweep, and only then enables downloads. Mutation remains disabled until its independent exact validation succeeds.
6. Crash injection before and after the final reconciliation commit exposes either the old matching catalog-record pair or the new pair, never a mixed pair.
7. A changed origin or owner is refused without an explicit state-reset or multi-Profile operation. A changed read key cannot reuse the old record offline. Key rotation for the same origin and owner succeeds only through online validation and a complete sweep.

On the Arch/Niri target, pre-cache one recorded Test asset and record one uncached asset. Stop or firewall Immich without changing TLS, restart the user service, and verify listing plus a byte-for-byte cached read. Verify the uncached read fails within a fixed timeout, status reports degraded mode, and a unique create attempt leaves the upload-recovery directory unchanged. Do not Trash or Restore a Protected-library asset. Restore connectivity and verify status becomes online only after full reconciliation; then read the previously uncached asset. Repeat with a deliberately mismatched temporary read key and confirm that the mount never starts.

## Implementation status

The development tree implements the transactional Profile row, catalog integrity checks, narrow transport classification, no-download cache mode, local-only Preview preparation, degraded status, and one retry owner. Automated checks cover these boundaries. The remaining work is the target acceptance procedure above.
