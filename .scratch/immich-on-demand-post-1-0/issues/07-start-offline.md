# Start from trusted cached state while Immich is offline

Type: feature
Status: implemented
Target: 1.2
Blocked by: target acceptance

## Scope

Persist the validated server origin, user identity, Immich version, exact read scopes, and a SHA-256 fingerprint of the read key. If online validation is unreachable, mount a nonempty valid catalog in degraded read-only mode. Retry validation and a complete refresh in the background. Refuse offline startup when trusted state is absent or inconsistent.

## Acceptance

- With Immich unreachable, a previously validated Profile mounts its catalog and reads complete cached originals.
- Uncached reads and every remote mutation fail without changing local or remote state.
- Reconnection revalidates identity and scopes and completes a stable full refresh before downloads or mutations resume.
- A new local Profile or locally changed server or read key cannot use stale trust data. After the server responds, any changed user, version, or exact key scope refuses promotion before remote access resumes.

## Answer

`TrustedProfile` belongs to `Catalog` because the catalog already owns the asset projection and its SQLite transaction. `Catalog.finish_refresh()` commits the Profile and a stable full sweep together. An incremental or interrupted refresh never changes trust.

The Profile stores the canonical HTTPS origin, owner UUID, supported Immich version, exact read permissions, and SHA-256 of the current read key. It never stores a usable key. Offline admission also requires `PRAGMA quick_check == ok`, a completed full refresh, at least one asset, and one matching owner across every asset row.

The service enters degraded mode only for `ImmichUnavailableError`, which represents a timeout or network failure with no HTTP response. TLS failures, HTTP responses, invalid discovery, malformed data, wrong versions, wrong owners, Secret Service failures, and permission mismatches fail closed.

Degraded mode keeps one validated local catalog but publishes no remote capability. `ContentCache` verifies complete cached bytes, then rejects a miss before reservation, temporary-file creation, Eviction, or HTTP. `Library` has no mutation client. Preview preparation installs local failure records without HTTP, persisted Pins wait for reconnection, and automatic Eviction waits until the service is online. Explicit local Pin, Unpin, status, and Evict remain available.

One coalesced worker retries after 5, 10, 20, 40, and then 60 seconds. Explicit Refresh wakes the same worker. A successful attempt validates the current keys, verifies the origin and owner, commits a stable full refresh, and then enables downloads and mutation methods. An authoritative mismatch terminates the degraded mount.

`status` includes `online`. `mutation_enabled` remains false until online promotion.

## Automated checks

- The catalog rejects missing or malformed trust, empty or partial catalogs, owner mismatches, corruption, unsafe modes, links, and relative XDG paths.
- The HTTP boundary distinguishes availability failures from TLS, protocol, response, authentication, schema, version, and permission failures.
- Offline cache hits validate and read with no network call. Cache misses and corrupt entries preserve local state and fail before network or Eviction.
- Offline Preview preparation performs no HTTP call. Upload, trash, and Restore fail through the existing missing-mutation guards.
- Promotion completes validation and a full refresh before it enables downloads or mutations. Status changes from offline to online afterward.

## Target acceptance

Use only recorded Test assets:

1. Cache one Test original and record a second uncached Test asset.
2. Make Immich unreachable without changing TLS or credentials, then restart the user service.
3. Verify that the catalog mounts, `status` reports `online=False`, and the cached original matches byte for byte.
4. Verify that the uncached read fails promptly and that a create attempt leaves upload recovery unchanged.
5. Restore connectivity. Verify that `status` becomes online only after the full refresh, then read the second Test asset.
6. Repeat startup with a temporary mismatched read key and verify that the mount refuses to start.

Never trash, restore, or otherwise mutate a Protected-library asset.

## Limits and hazards

Immich 3.0.3 exposes no stable instance identifier in this client contract, so the canonical configured origin identifies the server. The service cannot detect a server replacement or permission change while that origin is unreachable. Offline trust records the last successful validation. Reconnection discovers a mismatch before network reads or mutations resume.

This single-Profile implementation refuses a different origin or owner instead of clearing or reusing cached state. Profile replacement belongs to the multiple-Profile ticket.
