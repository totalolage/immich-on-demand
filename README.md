# Immich On-Demand

Immich On-Demand mounts one user's Immich library as a Linux filesystem. Directory listings use a local metadata catalog. Nautilus uses Immich-generated previews, and an application downloads an original only when it reads the file.

Released version 1.0 exposes a flat directory and targets Arch Linux, Niri, Nautilus 50, FUSE 3, and Immich 3.0.3. The current source tree is version 1.3.0.dev0. It implements rich Views, but read-only acceptance on the Reference system remains pending. Other systems are not tested.

## Filesystem contract

The released 1.0 mount contains one file for each visible asset. The first asset with a given safe basename keeps that name. Later collisions include the complete asset UUID before the extension.

The 1.3 development mount has this root namespace:

```text
/
├── All/
├── Albums/<album>/
├── People/<person>/
├── by Date/YYYY/MM/DD/
└── Favorites/
```

`All` contains every visible asset. An asset can also appear in several Albums, People, one date directory, and Favorites. Every alias has the same inode and reports the number of visible aliases as its hardlink count. Aliases share original-byte cache, Pin, and mutation state.

Existing assets are immutable. Applications can list, read, and copy them through ordinary read-only opens. A read-only remote open with `O_NOATIME` returns `EOPNOTSUPP` before it can download the original. Applications cannot overwrite, truncate, rename, link, or change their metadata.

Released 1.0 accepts create and unlink at the mount root. Development 1.3 accepts them only in `All`; every other View is read-only. Creating a new file stages private local bytes. Flush and `fsync` make those bytes locally durable but do not contact Immich. The last close seals a Pending upload and removes its temporary name from the mount. One service-owned worker uploads it, verifies the returned asset, publishes the Library entry, and then removes the private copy. Temporary outages retry with bounded backoff; blocked jobs remain available for explicit Retry or confirmed Cancel. FUSE cannot report upload completion from release, so a successful close means only that the local Pending copy is durable.

By default, unlink is disabled. If you enable remote deletion, unlink moves an owned asset to Immich trash. The client refuses deletion when the server has disabled trash, and it never requests permanent deletion. Cache eviction is a separate local operation and never changes Immich.

Previews are supported for JPEG, PNG, GIF, MP4, MOV, and M4V assets. Development 1.3 installs a Preview for every alias but groups work by asset, so all aliases use at most one server Preview fetch. Its missing-preview queue follows the Nautilus sort saved for `All` and reorders pending work when that sort changes. Downloads preserve original bytes in every format. Uploads accept every extension reported by the connected Immich server. Other Preview formats remain future work.

## Build and install the Arch package

Install the Arch build tools, clone the source, and run `makepkg` as your normal user:

```bash
sudo pacman -S --needed base-devel git
git clone https://github.com/totalolage/immich-on-demand.git
cd immich-on-demand/packaging
makepkg -si
```

`packaging/PKGBUILD` builds the tagged release named by `pkgver`, currently version 1.0.0 rather than the development source tree. The package installs the Python application and the `immich-on-demand.service` systemd user unit.

## Store API keys in Secret Service

For the current 1.3 development tree, create a read-only API key in Immich with exactly these permissions:

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
  --label='Immich On-Demand read-only API key' \
  application immich-on-demand server https://photos.example.com purpose read-only
unset IMMICH_KEY
```

To upload files, create a separate mutation key with exactly these five permissions:

- `user.read`
- `asset.read`
- `asset.view`
- `asset.download`
- `asset.upload`

If you enable remote deletion, also grant the mutation key:

- `asset.delete`

Do not grant `album.read` or `person.read` to the mutation key. Album and People access belongs only to the read-only key.

Store it with the `mutation` purpose:

```bash
read -rsp 'Mutation Immich API key: ' IMMICH_KEY && printf '\n'
printf '%s' "$IMMICH_KEY" | secret-tool store \
  --label='Immich On-Demand mutation API key' \
  application immich-on-demand server https://photos.example.com purpose mutation
unset IMMICH_KEY
```

Replace `https://photos.example.com` with the configured HTTPS origin. Include a nondefault port, but do not include a path. On first use, a development build moves a version 1.0 hostname-only item for a default HTTPS origin to this canonical identity.

## Configure and run the service

Write the server URL, mount path, and default cache limits:

```bash
immich-on-demand configure \
  --server https://photos.example.com \
  --mount "$HOME/Photos" \
  --cache-max-gib 10 \
  --cache-max-age-days 30 \
  --minimum-free-gib 5
immich-on-demand auth-check
immich-on-demand auth-check --mutation
```

The first `auth-check` validates Immich 3.0.3 and the exact read-only permissions. The second command validates the optional mutation key against the configured operations.

The service creates a missing mount directory. An existing mount directory must be empty, owned by the current user, and not a symbolic link.

The released 1.0 service requires Immich to be online at startup. If Immich becomes unavailable later, the running mount keeps its catalog and continues to serve cached originals. Reads of uncached originals fail until Immich returns.

The development tree can start from trusted cached state after one successful online run. If Immich is unreachable, it mounts a safe, nonempty catalog in degraded mode. Cached originals remain readable, but uncached reads, Preview downloads, automatic Eviction, and every remote mutation stay disabled. The service retries validation and a stable full refresh in the background before it resumes network access. TLS, authentication, schema, identity, version, scope, and local trust failures still prevent the mount.

Trusted Profile format v1 remains valid for offline startup after namespace migration. The service writes format v2 only after an online refresh validates and publishes both Album and People relations with the rich six-scope read key.

In the development tree, routine background refreshes request only assets updated within an overlapping time window. These refreshes never remove an absent catalog row. Startup, explicit `refresh`, daily repair, and an over-budget delta use paired complete asset sweeps before removing rows.

Album and People relations refresh as one pair after a complete asset sweep. The catalog publishes the pair only after both server inventories validate. Incremental asset refreshes update View aliases from current asset facts but never infer Album or People relation removal.

The 1.3 implementation has automated coverage. Read-only acceptance of its rich Views and package lifecycle on the Reference system remains pending.

To enable remote deletion, rerun `configure` with the same server and mount arguments plus `--enable-remote-delete`. The mutation key must then include `asset.delete`. The service fails closed if either key has unexpected permissions.

Start the filesystem as a systemd user service:

```bash
systemctl --user enable --now immich-on-demand.service
systemctl --user status immich-on-demand.service
```

For foreground diagnostics, stop the user service and run:

```bash
immich-on-demand mount
```

The following commands talk to the running service through its private Unix socket:

```bash
immich-on-demand status
immich-on-demand refresh
immich-on-demand evict
immich-on-demand evict --asset 12345678-1234-4234-8234-123456789abc
```

`evict` removes complete cached originals that are not open or downloading. With no `--asset`, it evicts every eligible original. The file remains in the mount and downloads again on its next read.

Development builds add durable upload controls:

```bash
immich-on-demand uploads
immich-on-demand retry-upload --id 12345678-1234-4234-8234-123456789abc
immich-on-demand cancel-upload \
  --id 12345678-1234-4234-8234-123456789abc \
  --revision 4 \
  --confirm-name 'exact original name.jpg'
```

Development builds also provide Pin commands:

```bash
immich-on-demand pin --asset 12345678-1234-4234-8234-123456789abc
immich-on-demand pin-status --asset 12345678-1234-4234-8234-123456789abc
immich-on-demand unpin --asset 12345678-1234-4234-8234-123456789abc
```

`pin` records the Pin before it downloads the original. A Pin protects a complete original from automatic and manual Eviction. If a download fails, the Pin remains and the next service start retries it. Run `pin` again to retry without restarting. The minimum free-space limit still applies.

`pin-status` reports `pinned`, `cached`, `busy`, and `scheduled`. `unpin` removes the protection but keeps any cached bytes until normal Eviction removes them.

`status` also reports `online`. While it is false, `mutation_enabled` is false and Pins without complete cached bytes wait for reconnection.

`uploads` prints one JSON object per Pending or recoverable upload. Retry reuses the durable job identity and verifies any earlier Immich candidate before sending another upload. Cancel deletes only local bytes and requires the current revision plus the exact requested name; it refuses work that may already exist remotely. `status` reports `pending_uploads` and `upload_quarantined` as local counts.

Development builds also provide an explicit Restore command:

```bash
immich-on-demand restore --asset 12345678-1234-4234-8234-123456789abc
```

Restore requires `--enable-remote-delete` and a mutation key with exactly `user.read`, `asset.read`, `asset.view`, `asset.download`, `asset.upload`, and `asset.delete`. The service accepts only a canonical asset UUID for a known, trashed asset owned by the mutation user. Immediately before the restore request, the client fetches the current server features and requires literal `trash: true`.

A successful response must report that Immich restored exactly one asset. The service then exposes the existing catalog row and schedules a refresh. The Library name and inode do not change. Restore is never a filesystem side effect.

After a configuration change, restart the service:

```bash
systemctl --user restart immich-on-demand.service
```

## Desktop integration in development

The development tree contains a GTK 4 and libadwaita settings application plus a Nautilus 50 extension. The settings application edits the single configured server, mount, cache policy, refresh interval, and remote-delete policy. Nonblank API key fields replace the matching Secret Service item. Saving settings does not restart the service.

On Arch Linux, build the VCS package from the current `main` branch:

```bash
cd packaging/development
makepkg -si
```

This installs `immich-on-demand-git`, including the desktop entry, Nautilus loader, icons, emblems, and user service. It conflicts with the released `immich-on-demand` package but preserves per-user configuration, Secret Service items, catalog, cache, and Pending uploads during replacement.

Before uninstalling it, stop the per-user service and remove its enablement link:

```bash
systemctl --user disable --now immich-on-demand.service
sudo pacman -Rns immich-on-demand-git
```

The GUI Restore control accepts one asset UUID. The UUID is transient and is not saved in configuration. The GUI also lists Pending uploads and exposes Retry and confirmed Cancel. These operations use the private control socket from its bounded worker and display only fixed success or failure text.

Inside the configured mount, Nautilus adds folder actions for Refresh, Settings, and Manage Pending Uploads. A one-file selection can Pin, Unpin, retry a failed pinned download, or Evict its local copy. Emblems report cached, pinned, and busy state. The extension returns no actions or emblems outside the configured mount.

The released version 1.0 Arch recipe does not install this desktop entry, the Nautilus loader, or the emblem icons. The following development-package checks remain open on the Reference system:

- Build, install, upgrade, restart, and uninstall a package that contains the desktop files.
- Load the extension in Nautilus 50 and verify mount scoping, menus, emblem changes, and failure behavior.
- Save settings and replacement keys through the GUI, then restart the user service and verify the new configuration.
- Pin one recorded Test asset, verify restart and Eviction behavior, inspect it with `pin-status`, then Unpin it. Do not use a Protected-library asset.
- Restore only the recorded trashed Test asset. Verify that its Library name and inode remain stable. Never use Restore on a Protected-library asset.
- Run routine incremental refresh and complete-reconciliation checks through the installed target service.

## Local data and upload recovery

Immich On-Demand follows the XDG base-directory variables. Without overrides, it stores local data at these paths:

- Configuration: `~/.config/immich-on-demand/config.json`
- Catalog: `~/.local/state/immich-on-demand/catalog.db`
- Pending uploads: `~/.local/share/immich-on-demand/uploads/`
- Complete originals: `~/.cache/immich-on-demand/originals/`
- Previews: `~/.cache/thumbnails/`
- Control socket: `$XDG_RUNTIME_DIR/immich-on-demand/control.sock`

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

The [post-1.0 roadmap](.scratch/immich-on-demand-post-1-0/map.md) records target acceptance for incremental refresh, Pin, Restore, trusted offline startup, desktop controls, queued uploads, and rich Views. It also tracks AUR publication, broader Preview formats, Asset replacement, partial Hydration, multiple Profiles, and more platforms.

## License

Immich On-Demand is licensed under GPL-3.0-or-later. See [LICENSE](LICENSE).
