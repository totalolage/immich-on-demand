# Publish the Arch package to the AUR

Type: delivery
Status: ready
Target: 1.0.x
Blocked by: none

## Scope

Publish the released `PKGBUILD` and generated `.SRCINFO` under `immich-on-demand`. Keep the AUR repository limited to packaging files and update its source checksum for each release tag.

## Acceptance

- `makepkg --cleanbuild` succeeds in a clean Arch environment.
- The package installs the executable and the systemd user unit.
- Install, upgrade, service restart, disable, and uninstall succeed on the Reference system.
- An unauthenticated client can download every source URL in `.SRCINFO`.
