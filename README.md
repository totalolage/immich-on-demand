# Immich On-Demand

Immich On-Demand mounts one user's Immich library as a flat Linux directory. Directory listings use a local metadata catalog. Nautilus uses Immich-generated previews, and an application downloads an original only when it reads the file.

Version 1.0 targets Arch Linux, Niri, Nautilus 50, FUSE 3, and Immich 3.0.3. Other systems are not tested.

## Filesystem contract

The mount contains one file for each visible asset. The first asset with a given safe basename keeps that name. Later collisions include the complete asset UUID before the extension.

Existing assets are immutable. Applications can list, read, and copy them through ordinary read-only opens. A read-only remote open with `O_NOATIME` returns `EOPNOTSUPP` before it can download the original. Applications cannot overwrite, truncate, rename, link, or change their metadata.

Creating a new file stages private local bytes. Flush syncs those bytes but does not upload them. The last close makes one upload attempt. FUSE cannot report an error from release, so an upload failure is logged and the recovery copy stays outside the mount. A successful upload removes the staged copy. Version 1.0 does not queue writes or retry uploads while offline.

By default, unlink is disabled. If you enable remote deletion, unlink moves an owned asset to Immich trash. The client refuses deletion when the server has disabled trash, and it never requests permanent deletion. Cache eviction is a separate local operation and never changes Immich.

Previews are supported for JPEG, PNG, GIF, MP4, MOV, and M4V assets. The missing-preview queue follows the sort saved for this folder by Nautilus and reorders pending work when that sort changes. Downloads preserve original bytes in every format. Uploads accept every extension reported by the connected Immich server. Other previews, albums, people, favorites, date views, and hardlinks between views are future work.

## Build and install the Arch package

Install the Arch build tools, clone the source, and run `makepkg` as your normal user:

```bash
sudo pacman -S --needed base-devel git
git clone https://github.com/totalolage/immich-on-demand.git
cd immich-on-demand/packaging
makepkg -si
```

`packaging/PKGBUILD` builds the tagged release named by `pkgver`. The package installs the Python application and the `immich-on-demand.service` systemd user unit.

## Store API keys in Secret Service

Create a read-only API key in Immich with exactly these permissions:

- `user.read`
- `asset.read`
- `asset.view`
- `asset.download`

Store the key under the server hostname. The following command reads the value without echoing it or placing it in shell history:

```bash
read -rsp 'Read-only Immich API key: ' IMMICH_KEY && printf '\n'
printf '%s' "$IMMICH_KEY" | secret-tool store \
  --label='Immich On-Demand read-only API key' \
  application immich-on-demand server photos.example.com purpose read-only
unset IMMICH_KEY
```

To upload files, create a separate mutation key with these five permissions:

- `user.read`
- `asset.read`
- `asset.view`
- `asset.download`
- `asset.upload`

If you enable remote deletion, also grant the mutation key:

- `asset.delete`

Store it with the `mutation` purpose:

```bash
read -rsp 'Mutation Immich API key: ' IMMICH_KEY && printf '\n'
printf '%s' "$IMMICH_KEY" | secret-tool store \
  --label='Immich On-Demand mutation API key' \
  application immich-on-demand server photos.example.com purpose mutation
unset IMMICH_KEY
```

Replace `photos.example.com` with the hostname from the configured server URL. Do not include the scheme, port, or path.

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

Startup deliberately requires Immich to be online. Before mounting, the service validates the configured keys, refreshes the catalog, and prepares Preview suppression. If Immich becomes unavailable later, the running mount keeps its catalog and continues to serve cached originals. Reads of uncached originals fail until Immich returns.

In the development tree, routine background refreshes request only assets updated within an overlapping time window. These refreshes never remove an absent catalog row. Startup, explicit `refresh`, daily repair, and an over-budget delta use paired complete sweeps before removing rows.

The incremental refresh implementation has automated coverage and a read-only Immich 3.0.3 server check. The updated service has not completed its package and lifecycle checks on the target Arch system.

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

Development builds also provide Pin commands:

```bash
immich-on-demand pin --asset 12345678-1234-4234-8234-123456789abc
immich-on-demand pin-status --asset 12345678-1234-4234-8234-123456789abc
immich-on-demand unpin --asset 12345678-1234-4234-8234-123456789abc
```

`pin` records the Pin before it downloads the original. A Pin protects a complete original from automatic and manual Eviction. If a download fails, the Pin remains and the next service start retries it. Run `pin` again to retry without restarting. The minimum free-space limit still applies.

`pin-status` reports `pinned`, `cached`, `busy`, and `scheduled`. `unpin` removes the protection but keeps any cached bytes until normal Eviction removes them.

After a configuration change, restart the service:

```bash
systemctl --user restart immich-on-demand.service
```

## Desktop integration in development

The development tree contains a GTK 4 and libadwaita settings application plus a Nautilus 50 extension. The settings application edits the single configured server, mount, cache policy, refresh interval, and remote-delete policy. Nonblank API key fields replace the matching Secret Service item. Saving settings does not restart the service.

Inside the configured mount, Nautilus adds folder actions for Refresh and Settings. A one-file selection can Pin, Unpin, retry a failed pinned download, or Evict its local copy. Emblems report cached, pinned, and busy state. The extension returns no actions or emblems outside the configured mount.

The released version 1.0 Arch recipe does not install this desktop entry, the Nautilus loader, or the emblem icons. The following target-system checks remain open:

- Build, install, upgrade, restart, and uninstall a package that contains the desktop files.
- Load the extension in Nautilus 50 and verify mount scoping, menus, emblem changes, and failure behavior.
- Save settings and replacement keys through the GUI, then restart the user service and verify the new configuration.
- Pin one recorded Test asset, verify restart and Eviction behavior, inspect it with `pin-status`, then Unpin it. Do not use a Protected-library asset.
- Run routine incremental refresh and complete-reconciliation checks through the installed target service.

## Local data and upload recovery

Immich On-Demand follows the XDG base-directory variables. Without overrides, it stores local data at these paths:

- Configuration: `~/.config/immich-on-demand/config.json`
- Catalog: `~/.local/state/immich-on-demand/catalog.db`
- Complete originals: `~/.cache/immich-on-demand/originals/`
- Upload recovery copies: `~/.cache/immich-on-demand/uploads/`
- Previews: `~/.cache/thumbnails/`
- Control socket: `$XDG_RUNTIME_DIR/immich-on-demand/control.sock`

Original downloads use a private temporary file. The cache publishes the file only after its byte count and available checksum pass validation. An interrupted or invalid download never replaces a complete cache entry.

The last close makes one upload attempt. A successful upload removes its staged copy. A failed upload or duplicate-content rejection is written to the service log and keeps the file below `uploads/` with mode `0600` in a directory with mode `0700`. FUSE release has no error return, so the application that closed the file cannot detect this failure. The service does not retry it. Copy the recovery file to a safe location, then copy it into the mount again when you are ready to retry. Delete the recovery copy only after the new asset appears in Immich.

## Test against an Immich library

Use the read-only key for tests that enumerate metadata, fetch previews, or download originals. Existing assets form the protected library. Never upload over, trash, restore, or otherwise change them.

Create separate test assets with the mutation key. Mutation tests may act only on asset UUIDs created for that test. Keep the mutation key out of read-only test runs, and enable remote deletion only after recording the allowed test UUIDs.

Run the local test suite with:

```bash
scripts/check
```

## Future work

Version 1.0 is available on GitHub. The package has not been published to the AUR.

Albums, people, dates, and other views are also deferred. A future version may expose paths such as `{Albums,People,All,by Date}/asset.ext` and hardlink repeated views of the same asset.

The [post-1.0 roadmap](.scratch/immich-on-demand-post-1-0/map.md) records target acceptance for incremental refresh, Pin, and desktop controls. It also tracks AUR publication, Restore, offline behavior, rich Views, broader Preview formats, Asset replacement, partial Hydration, multiple Profiles, and more platforms.

## License

Immich On-Demand is licensed under GPL-3.0-or-later. See [LICENSE](LICENSE).
