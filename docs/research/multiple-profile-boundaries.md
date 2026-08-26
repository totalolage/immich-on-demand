# Multiple Profile boundaries

Status: core source implementation complete; reversible retirement and target acceptance pending
Date: 2026-08-26
Examined baseline: Immich On-Demand development tree at version `1.4.0.dev0`

## Decision

Run one existing filesystem process per Profile. Select that Profile before any configuration, credential, local state, socket, or mount operation. Do not put several mounts into one process and do not add a supervisor daemon.

Add one small `profiles` module with this interface:

```python
@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    config: Path
    state: Path
    data: Path
    cache: Path
    runtime: Path


def select_profile(profile_id: str) -> Profile: ...
def profiles() -> tuple[Profile, ...]: ...


@contextmanager
def manage_profile(profile: Profile, mount_path: Path | None = None) -> Iterator[Profile]: ...


@contextmanager
def claim_service(profile: Profile) -> Iterator[Settings]: ...
```

`select_profile` validates the identifier once and derives every XDG path without loading config. It refuses a non-`default` ID while the legacy config exists; the locked lifecycle contexts repeat that check authoritatively. `profiles` discovers the same validated directory names for the GUI and Nautilus and reports an unmigrated singleton installation as the only selectable `default` Profile. `manage_profile` owns the global-management and Profile-lock order for creation, save, migration, and retirement. `claim_service` briefly takes those locks in the same order, completes or rejects legacy migration, retains the Profile lock, releases the global lock, then loads strict config and acquires the mount claim before returning its `Settings`.

`run_service` accepts a `Profile`, not `Settings`, and enters `claim_service` itself. A caller therefore cannot accidentally load config before the running-instance lock. Management callers keep the `manage_profile` context open across both config and Secret Service writes. These two concrete lifecycle contexts are the whole concurrency interface; lock paths and ordering are not exposed to callers.

Callers continue to use `Settings`, `Catalog`, `ContentCache`, `UploadQueue`, and the existing control protocol; they receive paths from the claimed `Profile` instead of calling global path helpers. This is the seam: selection, layout, migration, and lock lifetime stay local to one module, while storage and remote behavior remain in their existing modules. Deleting it would spread identifier validation, six path rules, and lock ordering back across the CLI, service, desktop application, Nautilus extension, Secret Service adapter, and packaging.

There is no profile registry database, adapter hierarchy, plugin interface, central daemon, or cross-profile catalog. The profile directories are the local registry.

## Domain rules

A Profile remains exactly the term in `CONTEXT.md`: one Immich server, one authenticated Immich user, one mount, and their local state.

- A Profile ID is a local, user-chosen identity. It is not a server hostname, owner UUID, display label, or derived hash.
- The ID is immutable. Changing it is a migration, not a settings edit.
- Two Profiles may use the same canonical server origin and even the same Immich user. They must still have separate credentials and local roots.
- A resolved mount path may not equal, contain, or be contained by another active Profile's mount path.
- A catalog belongs to exactly one Profile. Its existing `TrustedProfile` row continues to bind that catalog to the canonical server origin, owner UUID, server version, read scope, and read-key fingerprint.
- An Upload recovery belongs to the Profile whose data root contains it. Its existing origin and owner fields remain the remote-identity guard. Do not add a redundant Profile ID to every manifest.
- Asset UUIDs and numeric catalog inodes are Profile-local. Two FUSE mounts may report the same numeric `st_ino`; Linux file identity is the pair `(st_dev, st_ino)`, so those are not shared inodes across mounts. View aliases continue to share an inode only inside one Profile.

Accept only IDs that match this complete ASCII expression:

```text
[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?
```

The ID is therefore 1 to 32 bytes, lowercase, starts and ends with a letter or digit, and may contain internal hyphens. Do not trim, lowercase, Unicode-normalize, or otherwise repair input; reject a non-canonical value. This keeps the same literal safe as an XDG path component and a systemd instance name. Reject a runtime control-socket path that does not fit the platform's encoded Unix-socket path limit even after the ID passes validation.

## Current coupling to replace

The current modules already contain the right per-Profile behavior, but their roots are global:

| Concern | Current authority | 2.0 selection rule |
| --- | --- | --- |
| Configuration | `settings.config_path()` returns one `config.json` | Load and save only `profile.config / "config.json"` |
| Credentials | `_api_key_attributes` keys by application, canonical server, and purpose | Add the exact Profile ID to every Secret Service search, create, replace, and delete |
| Catalog and offline trust | `run_service` opens one `state/catalog.db`; `TrustedProfile` binds server and owner | Open the selected catalog; keep `TrustedProfile` unchanged |
| Originals | `run_service` opens one `cache/originals` | Open only the selected cache root |
| Pending uploads | `run_service` opens one `data/uploads` | Open only the selected data root; retain manifest origin/owner checks |
| Control | CLI and desktop always use one `runtime/control.sock` | Derive the selected socket before sending or serving |
| Desktop settings | `desktop_app` calls global `load`, `save`, and `run_action` | Hold one explicit selected Profile and pass it to every operation |
| Nautilus | `_load_mount` caches one configured mount | Cache all active non-overlapping Profile mounts and route a URI to exactly one Profile |
| Service and logs | one `immich-on-demand.service` | one systemd template instance and journal stream per Profile |

The freedesktop thumbnail cache is the deliberate exception. It stays at `$XDG_CACHE_HOME/thumbnails` because its key is the canonical mounted file URI. Distinct mount paths produce distinct Preview keys. Profile removal must not sweep this shared cache.

## Path and credential layout

For Profile `home`, use these exact roots:

| State | Path |
| --- | --- |
| Configuration | `$XDG_CONFIG_HOME/immich-on-demand/profiles/home/config.json` |
| Catalog | `$XDG_STATE_HOME/immich-on-demand/profiles/home/catalog.db` |
| Pending uploads | `$XDG_DATA_HOME/immich-on-demand/profiles/home/uploads/` |
| Complete originals | `$XDG_CACHE_HOME/immich-on-demand/profiles/home/originals/` |
| Control socket | `$XDG_RUNTIME_DIR/immich-on-demand/profiles/home/control.sock` |
| Running-instance lock | `$XDG_RUNTIME_DIR/immich-on-demand/profiles/home/service.lock` |
| systemd unit | `immich-on-demand@home.service` |
| logs | journal records for `immich-on-demand@home.service` |

Every application root below an XDG base, its `profiles` directory, and each Profile directory is a real directory owned by the current user with mode `0700`. Validate each path component with `lstat` or a directory descriptor; never follow a Profile parent or final entry through a symlink.

Open an existing config with `O_RDONLY | O_CLOEXEC | O_NOFOLLOW`, then require `fstat` to report a regular file owned by the current user, mode `0600`, and link count one before reading JSON. Save through a same-directory `0600` temporary opened relative to the validated directory descriptor, fsync the file, atomically replace the fixed `config.json` basename, and fsync that directory. These checks apply to normal load/save and to both sides of legacy migration. Reuse the existing strict catalog, cache, and Upload queue checks after their selected roots are derived.

Secret Service attributes become:

```text
application=immich-on-demand
profile=home
server=https://canonical.example
purpose=read-only | mutation
```

The item label includes the Profile ID for human inspection, but searches rely only on the exact attributes. After every normal Secret Service search, read and compare the complete returned attribute mapping; ignore subset or superset matches. The `profile` attribute is necessary even when origins differ: two local Profiles for the same origin must never replace each other's keys. Continue to canonicalize the origin through `Settings.server_origin`; never put a secret in config, argv, a socket frame, an upload manifest, or a log.

The catalog needs no Profile column. The selected database path supplies local scope and the existing `TrustedProfile` supplies remote identity. The cache remains keyed by asset UUID and the upload queue remains keyed by job UUID because their roots are already Profile-specific.

## Process, socket, and log selection

Install `immich-on-demand@.service` with:

```text
ExecStart=/usr/bin/immich-on-demand --profile %i mount
RestartPreventExitStatus=78
```

Construct the unit name literally as `immich-on-demand@ID.service`; never pass the already validated ID through `systemd-escape`. With the restricted ID alphabet, `%i` is the same exact value. Do not use `%I`. Deterministic identifier, file-validation, migration, Profile-lock, or mount conflicts exit `78`. Both the template and the one-release compatibility singleton set `RestartPreventExitStatus=78`. Contention on the short-lived global `profiles.lock` is different: it is transient, returns retryable status `75`, and is not restart-prevented. Each instance runs the existing foreground service, owns one pyfuse3 mount, and serves one socket. systemd already attaches the instance unit to each journal record, so `journalctl --user-unit immich-on-demand@home.service` selects one Profile's logs without a new log file or logging framework. Use `fsname=immich-on-demand:home` only as a diagnostic mount label; it is not an identity check.

Every public CLI operation requires `--profile ID`, including `configure`, `auth-check`, `mount`, and every control command. Remove the public arbitrary `--config` escape hatch in 2.0; it selects settings without selecting the matching catalog, cache, credentials, or socket. The control frame does not need a Profile field. Connecting to the selected `0600` socket is the routing decision, and each handler already closes over one catalog and mount. A URI sent to that socket must still pass the service's existing mount-path check.

The desktop application has one visible Profile selector. A launcher with no Profile selected opens the selector and performs no status, mutation, or save. A Nautilus launch passes `--profile ID`, and the GUI displays that selected immutable ID and mount before enabling controls. Saving settings or keys enters `manage_profile` and holds both management and Profile locks across the config and all key writes; it is refused while that Profile runs.

Every desktop worker submission captures the selected Profile value. Its completion callback either updates that same Profile's model or is discarded if the selector has moved; it never reads a later selection and applies an old result to it. Result relays include `--profile ID`, and Pending-upload pagination keeps one captured Profile for every page.

One Nautilus extension process loads all active Profile IDs and their non-overlapping mount paths. `_Update` stores the explicit Profile with the URI. Describe batches contain updates for one Profile only and use only that Profile's socket; cache keys include `(profile.id, uri)`. Menu callbacks capture the same Profile and launch the desktop client with `--profile ID`. Equal or nested configured mounts are rejected rather than resolved by a longest-prefix rule. Context actions outside every configured mount remain absent.

## Collision and locking rules

Use native non-blocking `flock` locks; do not add a lock server.

1. `manage_profile` acquires `$XDG_RUNTIME_DIR/immich-on-demand/profiles.lock`, then the selected Profile's `service.lock`, and holds both for the whole creation, config-plus-key save, legacy migration, or retirement. While holding them, load every active config. Refuse an unreadable config because mount separation cannot then be proved.
2. Profile creation refuses an active or retired config, any state/data/cache artifact, or an exact Profile-tagged Secret Service item without an active config. It creates only the strict mode-`0700` config Profile directory; while still holding `manage_profile`, creation may reuse that directory when it is empty after an interrupted directory-creation step. Catalog, UploadQueue, and ContentCache create their own selected roots only after config exists. Creation never adopts a database, queue, cached original, retired config, key, symlink, wrong-mode path, or unknown entry.
3. Compare absolute, symlink-resolved mount paths by path components. Reject equality and either ancestor/descendant relation. This makes every filesystem URI belong to zero or one active Profile.
4. `run_service(profile)` enters `claim_service`, which briefly acquires the global lock and then `service.lock`. It completes `default` migration or rejects pending legacy migration while holding both, releases only the global lock, and then loads `config.json`. It therefore holds `service.lock` before config load, Secret Service, SQLite, upload recovery, FUSE, or HTTP. A second process with the same ID exits `78` immediately. Never unlink a lock file while a process may hold its inode.
5. After strict config load, `claim_service` resolves the mount once. For every proper resolved ancestor it acquires a shared lock file named by the SHA-256 of that encoded path, from filesystem root downward, then acquires an exclusive leaf lock for the complete mount path. It holds all of them for the service lifetime. Parent and child mounts conflict on the parent's lock, equal mounts conflict on the leaf lock, and siblings share only proper-ancestor locks. Resolve the mount again before FUSE and reject a change.
6. The service still runs `_prepare_mountpoint` and `_check_mountpoint`. Hierarchical mount locks close their check-to-mount race; they do not replace ownership, symlink, emptiness, or FUSE errors. Configuration scans catch ordinary conflicts; the locks remain authoritative after manual JSON edits or simultaneous starts.

All lock roots and files receive the same owned-directory and no-symlink checks as other Profile paths. Lock files are owned `0600` regular files with link count one, opened with `O_NOFOLLOW`, and are never unlinked while a holder may exist. Management and service startup use the order global, Profile. Service startup releases global while retaining Profile, then acquires mount ancestors from root to leaf. No path ever waits for global while holding Profile, so they cannot deadlock; the steady-state service holds no global lock.

## Creation and settings crash boundary

Creation holds one `manage_profile` context from its first directory operation through its last requested key write. It validates mount separation, creates or reuses only the strict empty config Profile directory, then atomically publishes strict `config.json` before writing either key. Config is the local Profile-creation commit: any later Secret Service failure leaves an active, explicitly editable Profile rather than ownerless local state. State, data, and cache roots do not exist until their current owning modules first need them.

Interruption has these exact outcomes:

| Last completed step | Durable result and retry behavior |
| --- | --- |
| Config Profile directory | Only that strict empty scaffold exists. The next create may reuse it while holding the same locks. |
| Config publication | The active Profile is listed and editable. Service start fails closed on the missing read-only key before catalog, cache, uploads, FUSE, or HTTP. |
| Read-only key write and exact read-back | The Profile can validate and run without mutations. A missing requested mutation key remains an editable incomplete setting, not a creation rollback. |
| Mutation key write and exact read-back | The requested configuration and both keys are complete. |

An active config always selects settings management rather than create. Saving existing settings likewise holds `manage_profile` across atomic config publication and every requested exact key replacement. It does not roll back a valid config by deleting directories or secrets after a later key failure.

## One-Profile migration

Migrate the existing unprofiled installation to the reserved ordinary ID `default`. Do this locally before any Immich client is opened. The migration performs no HTTP request and does not trash, restore, upload, or edit any remote asset.

For one 2.0 release, keep the singleton `immich-on-demand.service` as a compatibility unit whose only command is `immich-on-demand --profile default mount`. New installations and additional Profiles use the template unit. The Profile lock rejects accidentally starting the compatibility unit and `immich-on-demand@default.service` together without a restart loop.

The presence of the legacy `config.json` blocks selection, creation, management, or service start of every Profile other than `default`. `profiles()` reports only the migration candidate. The user or compatibility unit must finish `default` migration first; a partial legacy installation can never coexist with a newly created Profile.

Migration is allowed only when:

- the `default` Profile service lock can be acquired;
- the old mount is not mounted;
- the legacy `config.json` exists;
- no active profiled config exists; and
- no Profile artifact exists for an ID other than `default`; and
- no migration source and destination both exist for any one exact entry.

Under `profiles.lock`, create private destination parents, then move within each XDG base directory:

```text
$XDG_STATE_HOME/immich-on-demand/catalog.db      -> profiles/default/catalog.db
$XDG_STATE_HOME/immich-on-demand/catalog.db-wal  -> profiles/default/catalog.db-wal
$XDG_STATE_HOME/immich-on-demand/catalog.db-shm  -> profiles/default/catalog.db-shm
$XDG_DATA_HOME/immich-on-demand/uploads          -> profiles/default/uploads
$XDG_CACHE_HOME/immich-on-demand/originals        -> profiles/default/originals
$XDG_CONFIG_HOME/immich-on-demand/config.json     -> profiles/default/config.json
```

Those are exact names, not globs. Move `catalog.db`, then an existing `catalog.db-wal`, then an existing `catalog.db-shm`. Any other `catalog.db`-prefixed legacy entry blocks migration and is left untouched. For every named entry, source-only moves, destination-only is already complete, and source plus destination is always a conflict; do not compare and choose between local files. Move the config last as the completion marker. Each rename stays inside one XDG filesystem and is followed by fsync of both source and destination parents. Leave empty legacy parents and unrelated entries in place. The freedesktop thumbnail cache is unchanged because the mount URI is unchanged.

Before the first rename, preflight every exact source and destination named above, including absent entries. Validate all legacy and destination parent directories as real, current-user-owned mode-`0700` directories. Apply the Catalog predicate to each existing `catalog.db`, `catalog.db-wal`, and `catalog.db-shm`: `O_NOFOLLOW`, regular file, current owner, mode `0600`, and link count one. Apply the UploadQueue root predicate to each existing `uploads`: real directory, current owner, and mode `0700`. Apply the ContentCache root predicate to each existing `originals`: real directory, current owner, and mode `0700`. Validate source and destination config through the strict config predicate. Any unsafe entry or any source-plus-destination pair aborts the whole migration before a rename. Perform later fixed-name renames relative to the retained validated directory descriptors; the destination config must pass strict validation before migration is complete.

Secret migration is a dedicated path and never calls the normal load helper that deletes hostname-form legacy items. For each purpose, search the unprofiled canonical-origin attributes and, only for the existing default-port compatibility case, the unprofiled hostname attributes. Post-filter every returned item's complete attribute mapping for exact equality. Require all exact legacy candidates for that purpose to contain the same nonempty secret or refuse migration. A read-only source is required. If no exact legacy mutation item exists, no exact Profile-tagged mutation destination may exist; otherwise migration refuses the inconsistent partial state.

Create the Profile-tagged item with `replace=False`. If an exact destination already exists after a partial run, unlock and compare its secret with the selected legacy secret; continue only when they match. After creation or resume, search again, post-filter exact attributes, require exactly one destination, and compare its bytes before any local rename. Retain every legacy item. This makes migration restartable and non-destructive; normal 2.0 reads use only exact Profile-tagged items. A later, explicit cleanup may remove legacy items after acceptance.

Do not copy the catalog, cache, or Pending upload payloads. Atomic rename preserves their bytes, inode allocations, Pins, trusted offline state, queue manifests, and recovery permissions without the space and split-brain risk of two writable copies. A crash before the final config move resumes from the remaining sources. A destination conflict stops without choosing or deleting either side.

## Removal safety

The first 2.0 removal operation is deliberately reversible. The removal command requires both `--profile ID` and `--confirm-profile ID`, disables and stops only `immich-on-demand@ID.service`, acquires the Profile and mount locks, and refuses a still-mounted path. Removing `default` also disables and stops the one-release compatibility singleton. It makes no HTTP request.

Removal refuses an existing `config.retired.json`, then atomically renames only that Profile's `config.json` to it without replacement. The Profile disappears from active selection, while its catalog, originals, Pending uploads, config, and Secret Service items remain intact under the same ID. The retired directory reserves the ID and prevents an unrelated new Profile from adopting its state. A hard purge is not part of this slice: recursive deletion across four XDG roots and Secret Service is unnecessary to satisfy active removal and is the operation most likely to cross a scope boundary.

Neither removal nor a later restore scans by server origin, owner UUID, asset UUID, filename, or mount basename. Every local target comes from the confirmed canonical Profile ID. The shared thumbnail cache is untouched. Remote deletion, Restore, Upload retry, and Asset replacement are unreachable during removal.

## Concurrent acceptance matrix

Run these checks with separate processes, because pyfuse3 owns process-global mount state and production uses one systemd process per Profile.

| Scenario | Required observation |
| --- | --- |
| Two Profiles, different origins and owners | Both mounts and sockets stay live; each key search contains its own Profile ID; status and catalog rows never cross |
| Two Profiles, same origin and owner, different mounts | Separate catalogs, caches, upload roots, sockets, and journal units remain distinguishable even when the same asset UUID exists in both |
| Same Profile started twice | The second process exits `78` on `service.lock` before Secret Service, SQLite, upload recovery, FUSE, or HTTP; systemd does not restart it |
| Different IDs, equal or nested resolved mounts, simultaneous start | Hierarchical mount locks admit at most one; the loser has read only its strict config, then exits `78` before credentials, catalog, cache, uploads, FUSE, or HTTP |
| Profile creation recovery | A strict empty config directory resumes; a retired config, any state/data/cache artifact, or orphan exact key refuses; interruptions after that directory, config, read key, and mutation key produce the four documented outcomes |
| Global management contention | A busy `profiles.lock` returns retryable `75`; deterministic validation, migration, Profile, and mount conflicts return non-retryable `78` in both packaged units |
| Config path attacks | Normal load/save and migration reject a symlink, hard link, wrong owner, wrong mode, non-directory parent, or changed directory descriptor before JSON or secrets are used |
| Control isolation | Status, Refresh, Pin, Evict, Restore, retry, and cancel sent to A's socket affect only A; A's socket rejects a URI under B's mount |
| Offline trust | With both servers unavailable, each Profile validates only its own `TrustedProfile` and read-key fingerprint and exposes only its own cached originals |
| Cache and Pin isolation | Hydrating or Pinning the same asset UUID in A creates/touches only A's original and Pin row; B remains byte-for-byte unchanged |
| Recovery isolation | Concurrent writes with the same mounted filename create jobs only under their selected data roots; each socket lists only its own jobs |
| Nautilus routing | Each `_Update`, batch, menu action, cache entry, and result uses one explicit Profile; unrelated directories get none; equal or nested configs are rejected |
| Journal isolation | A known warning in each process is independently selectable by `journalctl --user-unit immich-on-demand@ID.service` and contains no credential |
| Legacy migration | Exact config, `catalog.db`, `catalog.db-wal`, `catalog.db-shm`, Pins, cache bytes, upload manifests/payloads, and exact key copies survive; destination secrets are created with `replace=False` and compared after every resume; no legacy item or local byte is deleted; no HTTP call occurs |
| Desktop callback isolation | Switch the selector while status, save, uploads pagination, and a relayed result are in flight; every completion remains attached to its captured Profile |
| Remove A while B runs | A must first be stopped and unmounted; retiring A changes only A's config name and unit state; hashes of B's four roots and B's Secret items are unchanged; the remote-call fake records nothing |

For inode isolation, compare `(st_dev, st_ino)` and behavior across the two mounts, not the raw inode number alone. For deletion safety, seed similarly named files and identical UUIDs in both Profile roots so a basename- or asset-based sweep would fail the test.

## Deliberate exclusions

- No cross-Profile combined Library View, search, cache quota, upload worker, or control socket.
- No automatic Profile choice based on current directory, sole configured Profile, server hostname, or stored key.
- No Profile rename in the first slice.
- No destructive local purge in the first slice. Add it only with a separate confirmation and directory-descriptor deletion design when users need to reclaim retired state.
- No change to Immich remote semantics. Each existing mutation remains guarded by the selected catalog, validated owner, and current opt-in settings.
