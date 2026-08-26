# Immich On-Demand

Immich On-Demand mounts one user's Immich library as a Linux filesystem. Directory listings use a local metadata catalog. Nautilus uses Immich-generated previews, and an application downloads an original only when it reads the file.

Released version 1.0 exposes a flat directory and targets Arch Linux, Niri, Nautilus 50, FUSE 3, and Immich 3.0.3. The current source tree is version 2.0.0.dev0. It adds isolated named Profiles to the rich Views and queued Asset replacement implemented in development. Reference-system acceptance remains pending. Other systems are not tested.

## Filesystem contract

The released 1.0 mount contains one file for each visible asset. The first asset with a given safe basename keeps that name. Later collisions include the complete asset UUID before the extension.

The development mount has this root namespace:

```text
/
├── All/
├── Albums/<album>/
├── People/<person>/
├── by Date/YYYY/MM/DD/
└── Favorites/
```

`All` contains every visible asset. An asset can also appear in several Albums, People, one date directory, and Favorites. Every alias has the same inode and reports the number of visible aliases as its hardlink count. Aliases share original-byte cache, Pin, and mutation state.

Existing asset inodes are immutable. Applications can list, read, and copy them through ordinary read-only opens. A read-only remote open with `O_NOATIME` returns `EOPNOTSUPP` before it can download the original. Direct overwrite, truncation, rename, link, and metadata changes return `EROFS`.

Released 1.0 accepts create and unlink at the mount root. Development builds accept them only in `All`; every other View is read-only. Creating a new file stages private local bytes. Flush and `fsync` make those bytes locally durable but do not contact Immich. The last close seals a Pending upload, but the staged name and bytes remain readable until the upload finishes. One service-owned worker uploads it, verifies the returned asset, publishes the Library entry, and then removes the private copy. Temporary outages retry with bounded backoff; blocked jobs remain available for explicit Retry or confirmed Cancel. FUSE cannot report upload completion from `close`, so a successful close means only that the local Pending copy is durable.

Development builds support the temp-file save pattern for replacement. An application creates and writes a temporary file in `All`, then renames it over an existing `All` entry. The target immediately reads the durable local payload while the worker verifies the candidate, copies album membership, and moves the old asset to Immich trash with `force: false`. The catalog then gives the stable target name, Pin, and every View alias to the candidate's new UUID and inode. The old UUID and inode remain recorded as trashed under a collision-safe name, so Restore cannot displace the replacement.

Replacing a Pinned asset transfers the Pin and verifies the candidate's cached original before the queue job completes. Unpinned replacements enter the normal original cache on first read.

The service defers a newly sealed ordinary upload for one second so an editor can issue rename-over after close. After the worker admits that upload, a late rename-over returns `EBUSY`. Direct writes and truncation of the existing target remain `EROFS` because every View alias shares its inode.

By default, unlink is disabled. If you enable remote deletion, unlink moves an owned asset to Immich trash. The client refuses deletion when the server has disabled trash, and it never requests permanent deletion. Cache eviction is a separate local operation and never changes Immich.

Released 1.0 supports Previews for JPEG, PNG, GIF, MP4, MOV, and M4V assets. Development builds install a Preview for every alias but group work by asset, so all aliases use at most one server Preview fetch. The missing-preview queue follows the Nautilus sort saved for `All` and reorders pending work when that sort changes. Downloads preserve original bytes in every format. Uploads accept every extension reported by the connected Immich server. The broader development image Preview behavior is described below.

## Build and install the Arch package

Install the Arch build tools, clone the source, and run `makepkg` as your normal user:

```bash
sudo pacman -S --needed base-devel git
git clone https://github.com/totalolage/immich-on-demand.git
cd immich-on-demand/packaging
makepkg -si
```

`packaging/PKGBUILD` builds the tagged release named by `pkgver`, currently version 1.0.0 rather than the development source tree. The package installs the Python application and the `immich-on-demand.service` systemd user unit. Configure that package with the exact [v1.0.0 instructions](https://github.com/totalolage/immich-on-demand/blob/v1.0.0/README.md); its hostname-only Secret Service identity and mutation scopes differ from the development tree below.

## Store API keys in Secret Service

The commands in this section are only for the current 2.0 development tree. Released 1.0 uses the tagged instructions linked above; do not give its mutation key the development-only `asset.copy` permission. The examples use Profile ID `home`.

Publish the Profile's non-secret configuration before storing either key. Creation refuses an orphan Profile-tagged key so it cannot silently adopt unrelated local state:

```bash
immich-on-demand --profile home configure \
  --server https://photos.example.com \
  --mount "$HOME/Photos" \
  --cache-max-gib 10 \
  --cache-max-age-days 30 \
  --minimum-free-gib 5
```

Create a read-only API key in Immich with exactly these permissions:

- `user.read`
- `asset.read`
- `asset.view`
- `asset.download`
- `album.read`
- `person.read`

Released 1.0 requires only the first four core permissions.

Store the key under the canonical server origin. The following command reads the value without echoing it or placing it in shell history:

```bash
read -rsp 'Read-only Immich API key: ' IMMICH_KEY && printf '\n'
printf '%s' "$IMMICH_KEY" | secret-tool store \
  --label='Immich On-Demand home read-only API key' \
  application immich-on-demand profile home \
  server https://photos.example.com purpose read-only
unset IMMICH_KEY
```

To upload files, create a separate mutation key with exactly these five permissions:

- `user.read`
- `asset.read`
- `asset.view`
- `asset.download`
- `asset.upload`

If you enable remote deletion, also grant the mutation key:

- `asset.copy`
- `asset.delete`

Do not grant `album.read` or `person.read` to the mutation key. Album and People access belongs only to the read-only key.

Store it with the `mutation` purpose:

```bash
read -rsp 'Mutation Immich API key: ' IMMICH_KEY && printf '\n'
printf '%s' "$IMMICH_KEY" | secret-tool store \
  --label='Immich On-Demand home mutation API key' \
  application immich-on-demand profile home \
  server https://photos.example.com purpose mutation
unset IMMICH_KEY
```

Replace `home` with the selected lowercase Profile ID and `https://photos.example.com` with its configured HTTPS origin. Include a nondefault port, but do not include a path. The first 2.0 use of Profile `default` copies matching version 1.0 credentials into Profile-tagged items and retains the legacy items for rollback.

## Validate and run the service

Validate the configured Profile and its keys:

```bash
immich-on-demand --profile home auth-check
immich-on-demand --profile home auth-check --mutation
```

The first `auth-check` validates Immich 3.0.3 and the exact read-only permissions. The second command validates the optional mutation key against the configured operations.

The service creates a missing mount directory. An existing mount directory must be empty, owned by the current user, and not a symbolic link.

The released 1.0 service requires Immich to be online at startup. If Immich becomes unavailable later, the running mount keeps its catalog and continues to serve cached originals. Reads of uncached originals fail until Immich returns.

The development tree can start from trusted cached state after one successful online run. If Immich is unreachable, it mounts a safe, nonempty catalog in degraded mode. Cached originals remain readable, but uncached reads, Preview downloads, automatic Eviction, and every remote mutation stay disabled. The service retries validation and a stable full refresh in the background before it resumes network access. Observable TLS, authentication, schema, identity, version, scope, and local trust failures prevent the mount or terminate degraded mode. A server-side identity or scope change cannot be detected while the origin is unreachable; reconnection detects it before remote access resumes.

Development builds use Immich's bounded generated Preview for every image MIME type, including RAW and HEIF originals. Live Photo stills retain their explicit motion-video relationship across refreshes and Views, but the mount exposes and previews only the still. Composite playback, paired export/upload, and Asset replacement of Live Photos are not implemented.

Partial Hydration remains intentionally absent. Immich 3.0.3 does not promise Range behavior for original downloads or expose a strong byte validator or chunk hashes, so original reads continue to download, validate, and cache the complete file atomically.

Trusted Profile format v1 remains valid for offline startup after namespace migration. The service writes format v2 only after an online refresh validates and publishes both Album and People relations with the rich six-scope read key.

In the development tree, routine background refreshes request only assets updated within an overlapping time window. These refreshes never remove an absent catalog row. Startup, explicit `refresh`, daily repair, and an over-budget delta use paired complete asset sweeps before removing rows.

Album and People relations refresh as one pair after a complete asset sweep. The catalog publishes the pair only after both server inventories validate. Incremental asset refreshes update View aliases from current asset facts but never infer Album or People relation removal.

The 2.0 implementation has automated coverage. Concurrent Profile isolation, read-only rich-View acceptance, RAW/HEIF/Live Photo acceptance, Asset-replacement acceptance with project-owned Test assets, and package lifecycle acceptance remain pending on the Reference system.

To enable remote deletion and queued asset replacement, rerun `configure` with the same server and mount arguments plus `--enable-remote-delete`. The mutation key must then include `asset.copy` and `asset.delete`. The service fails closed if either key has unexpected permissions.

Start the filesystem as a systemd user service:

```bash
systemctl --user enable --now immich-on-demand@home.service
systemctl --user status immich-on-demand@home.service
```

Each Profile uses a literal `immich-on-demand@ID.service` instance, private socket, catalog, cache, upload queue, credentials, and journal stream. Equal or nested mount paths are refused. For one 2.0 release, `immich-on-demand.service` remains only as a compatibility entry point for the legacy unprofiled installation; it selects `default`, copies matching legacy credentials without deleting them, and migrates the exact local files before mounting.

Retire a Profile only with its exact confirmation:

```bash
immich-on-demand --profile home retire-profile --confirm-profile home
```

Retirement disables and stops only that Profile's systemd unit (plus the compatibility singleton for `default`), refuses a still-mounted Library, and renames only `config.json` to `config.retired.json`. It makes no Immich request and retains the catalog, cached originals, Pending uploads, configuration, and Secret Service items. The retired ID remains reserved; there is no destructive purge command.

For foreground diagnostics, stop the user service and run:

```bash
immich-on-demand --profile home mount
```

The following commands talk to the running service through its private Unix socket:

```bash
immich-on-demand --profile home status
immich-on-demand --profile home refresh
immich-on-demand --profile home evict
immich-on-demand --profile home evict --asset 12345678-1234-4234-8234-123456789abc
```

`evict` removes complete cached originals that are not open or downloading. With no `--asset`, it evicts every eligible original. The file remains in the mount and downloads again on its next read.

Development builds add durable upload controls:

```bash
immich-on-demand --profile home uploads
immich-on-demand --profile home retry-upload --id 12345678-1234-4234-8234-123456789abc
immich-on-demand --profile home cancel-upload \
  --id 12345678-1234-4234-8234-123456789abc \
  --revision 4 \
  --confirm-name 'exact original name.jpg'
```

Development builds also provide Pin commands:

```bash
immich-on-demand --profile home pin --asset 12345678-1234-4234-8234-123456789abc
immich-on-demand --profile home pin-status --asset 12345678-1234-4234-8234-123456789abc
immich-on-demand --profile home unpin --asset 12345678-1234-4234-8234-123456789abc
```

`pin` records the Pin before it downloads the original. A Pin protects a complete original from automatic and manual Eviction. If a download fails, the Pin remains and the next service start retries it. Run `pin` again to retry without restarting. The minimum free-space limit still applies.

`pin-status` reports `pinned`, `cached`, `busy`, and `scheduled`. `unpin` removes the protection but keeps any cached bytes until normal Eviction removes them.

`status` also reports `online`. While it is false, `mutation_enabled` is false and Pins without complete cached bytes wait for reconnection.

`uploads` prints one JSON object per Pending or recoverable upload. Retry reuses the durable job identity and verifies any earlier Immich candidate before sending another upload. Cancel deletes only local bytes and requires the current revision plus the exact requested name; it refuses work that may already exist remotely. `status` reports `pending_uploads` and `upload_quarantined` as local counts.

Development builds also provide an explicit Restore command:

```bash
immich-on-demand --profile home restore --asset 12345678-1234-4234-8234-123456789abc
```

Restore requires `--enable-remote-delete` and a mutation key with exactly `user.read`, `asset.read`, `asset.view`, `asset.download`, `asset.upload`, `asset.copy`, and `asset.delete`. The service accepts only a canonical asset UUID for a known, trashed asset owned by the mutation user. Immediately before the restore request, the client fetches the current server features and requires literal `trash: true`.

A successful response must report that Immich restored exactly one asset. The service then exposes the existing catalog row and schedules a refresh. The Library name and inode do not change. Restore is never a filesystem side effect.

After a configuration change, restart the service:

```bash
systemctl --user restart immich-on-demand@home.service
```

## Desktop integration in development

The development tree contains a GTK 4 and libadwaita settings application plus a Nautilus 50 extension. Create a Profile with the CLI `configure` command; the settings application then selects one configured Profile at a time and edits its server, mount, cache policy, refresh interval, remote-delete policy, and Profile-tagged keys. Each background operation retains the Profile selected when it began. Saving settings does not restart a service.

The `debian/` directory is a native-package candidate for Ubuntu Desktop 26.04 LTS amd64. It builds with Ubuntu's packaged PEP 517 tools, installs the same desktop and user-service files, and depends only on named Ubuntu packages. It never uses pip, a private virtual environment, or a local pyfuse3 build. This recipe is not an Ubuntu support claim until the documented install, upgrade, removal, and Nautilus matrices pass on that release.

On Arch Linux, build the VCS package from the current `main` branch:

```bash
cd packaging/development
makepkg -si
```

This installs `immich-on-demand-git`, including the desktop entry, Nautilus loader, icons, emblems, and user service. It conflicts with the released `immich-on-demand` package but preserves per-user configuration, Secret Service items, catalog, cache, and Pending uploads during replacement.

Before uninstalling it, stop the per-user service and remove its enablement link:

```bash
systemctl --user disable --now immich-on-demand@home.service
sudo pacman -Rns immich-on-demand-git
```

The GUI Restore control accepts one asset UUID. The UUID is transient and is not saved in configuration. The GUI also lists Pending uploads and exposes Retry and confirmed Cancel. These operations use the private control socket from its bounded worker and display only fixed success or failure text.

Inside every configured, non-overlapping Profile mount, Nautilus routes folder actions, menus, emblems, and result relays to that Profile's private control socket. It adds folder actions for Refresh, Settings, and Manage Pending Uploads. A one-file selection can Pin, Unpin, retry a failed pinned download, or Evict its local copy. The extension returns no actions or emblems outside configured mounts.

The released version 1.0 Arch recipe does not install this desktop entry, the Nautilus loader, or the emblem icons. The following development-package checks remain open on the Reference system:

- Build, install, upgrade, restart, and uninstall a package that contains the desktop files.
- Run two configured template instances concurrently, including two Profiles for the same origin and owner, and verify separate mounts, sockets, credentials, catalogs, caches, Pending uploads, and journal units.
- Load the extension in Nautilus 50 and verify mount scoping, menus, emblem changes, and failure behavior.
- Save settings and replacement keys through the GUI, then restart the user service and verify the new configuration.
- Pin one recorded Test asset, verify restart and Eviction behavior, inspect it with `pin-status`, then Unpin it. Do not use a Protected-library asset.
- Restore only the recorded trashed Test asset. Verify that its Library name and inode remain stable. Never use Restore on a Protected-library asset.
- Replace only a newly uploaded, recorded Test asset through temp-file rename-over in `All`. Verify the stable name, the new inode, and the old UUID in trash, then Restore the old UUID under its collision name.
- Run routine incremental refresh and complete-reconciliation checks through the installed target service.

## Local data and upload recovery

Immich On-Demand follows the XDG base-directory variables. Without overrides, it stores local data at these paths:

- Configuration: `~/.config/immich-on-demand/profiles/home/config.json`
- Catalog: `~/.local/state/immich-on-demand/profiles/home/catalog.db`
- Pending uploads: `~/.local/share/immich-on-demand/profiles/home/uploads/`
- Complete originals: `~/.cache/immich-on-demand/profiles/home/originals/`
- Previews: `~/.cache/thumbnails/`
- Control socket: `$XDG_RUNTIME_DIR/immich-on-demand/profiles/home/control.sock`

Configured XDG base directories must be absolute paths. The service rejects a relative value before it opens local state.

Original downloads use a private temporary file. The cache publishes the file only after its byte count and available checksum pass validation. An interrupted or invalid download never replaces a complete cache entry.

Each upload job has a private `0700` directory containing a `0600` payload and an atomic bounded manifest. Cache limits and Eviction never touch these bytes. On restart, a complete job resumes; an interrupted write becomes blocked and is never retried automatically. Unsafe or unknown queue entries are preserved, counted as quarantined, and excluded from service operations.

Version 1.0 recovery bytes under `~/.cache/immich-on-demand/uploads/` are left untouched during upgrade and are not imported automatically. Copy a known recovery file into the mount to create a new durable queue job, and remove the legacy copy only after the asset is visible in Immich.

## Test against an Immich library

Use the read-only key for tests that enumerate metadata, fetch previews, or download originals. Existing assets form the protected library. Never upload over, trash, restore, or otherwise change them.

Create separate test assets with the mutation key. Mutation tests may act only on asset UUIDs created for that test. Keep the mutation key out of read-only test runs, and enable remote deletion only after recording the allowed test UUIDs.

Run the local test suite with:

```bash
scripts/check
```

## Future work

Version 1.0 is available on GitHub. The package has not been published to the AUR.

The [post-1.0 roadmap](.scratch/immich-on-demand-post-1-0/map.md) records target acceptance for incremental refresh, Pin, Restore, trusted offline startup, desktop controls, queued uploads, rich Views, Asset replacement, and multiple Profiles. It also tracks AUR publication, broader Preview formats, partial Hydration, and more platforms.

## License

Immich On-Demand is licensed under GPL-3.0-or-later. See [LICENSE](LICENSE).
