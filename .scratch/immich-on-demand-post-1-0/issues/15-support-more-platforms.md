# Support more file managers and Linux distributions

Type: research
Status: resolved
Target: 2.0
Blocked by: 05, 09

## Answer

Keep support claims exact and versioned. The next distribution target is Ubuntu
Desktop 26.04 LTS amd64 with Nautilus 50.0 because its native packages satisfy
all current dependency bounds and expose the Nautilus 4.1 GI API used by the
existing extension. Ubuntu Cinnamon 26.04 LTS amd64 with Nemo 6.4.5 is the only
next file-manager candidate: its source uses the compatible GIO and GNOME
thumbnail route, but support waits for the complete browsing, sort, controls,
package, upgrade, and removal matrix.

Do not claim Debian 13, Fedora 44, Thunar, Dolphin, headless sessions, or broad
Linux support. The exact contracts, version snapshot, blockers, acceptance
matrix, and unsupported limits are in
[`docs/research/file-manager-distribution-support.md`](../../../docs/research/file-manager-distribution-support.md).

The source tree now includes an Ubuntu 26.04 native `.deb` candidate under
`debian/`. It uses only named Ubuntu dependencies and bounded user-unit
maintainer scripts; Ubuntu runtime and package-lifecycle acceptance remain
pending, so this is not yet a support claim.

## Scope

Test the FUSE contract and Preview path against named file managers before claiming support. Add distribution packages only where maintained system dependencies cover pyfuse3, PyGObject, Secret Service, and the user-service lifecycle.

## Acceptance

- Each supported file manager has a browsing-isolation test with zero original downloads.
- Each supported distribution has a native install, upgrade, service restart, and uninstall check.
- Platform adapters remain outside Library, catalog, cache, and Immich policy.
- The documentation names exact tested versions and known limitations.
