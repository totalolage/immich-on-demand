# Add Pinning

Type: feature
Status: open
Target: 1.1
Blocked by: 05

## Scope

Persist Pin state by asset ID. A Pin Hydrates the complete original, keeps it available through later outages, and excludes it from automatic and manual whole-cache Eviction until the user removes the Pin.

## Acceptance

- Pinning an uncached asset downloads and validates it once.
- Pinned originals survive age, size, free-space, and manual whole-cache Eviction.
- Removing a Pin makes the asset eligible for normal Eviction without deleting it immediately.
- Pin status is available through the control API, CLI, Nautilus, and GUI clients.
