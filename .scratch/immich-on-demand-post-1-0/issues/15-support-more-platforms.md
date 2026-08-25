# Support more file managers and Linux distributions

Type: research
Status: open
Target: 2.0
Blocked by: 05, 09

## Scope

Test the FUSE contract and Preview path against named file managers before claiming support. Add distribution packages only where maintained system dependencies cover pyfuse3, PyGObject, Secret Service, and the user-service lifecycle.

## Acceptance

- Each supported file manager has a browsing-isolation test with zero original downloads.
- Each supported distribution has a native install, upgrade, service restart, and uninstall check.
- Platform adapters remain outside Library, catalog, cache, and Immich policy.
- The documentation names exact tested versions and known limitations.
