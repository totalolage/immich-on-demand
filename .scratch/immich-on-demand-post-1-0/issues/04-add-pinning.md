# Add Pinning

Type: feature
Status: implemented
Target: 1.1
Blocked by: 05
Target acceptance: pending

## Scope

Persist Pin state by asset ID. A Pin Hydrates the complete original, keeps it available through later outages, and excludes it from automatic and manual whole-cache Eviction until the user removes the Pin.

## Acceptance

- Pinning an uncached asset downloads and validates it once.
- Pinned originals survive age, size, free-space, and manual whole-cache Eviction.
- Removing a Pin makes the asset eligible for normal Eviction without deleting it immediately.
- Pin status is available through the control API, CLI, Nautilus, and GUI clients.

## Answer

The catalog stores Pins by asset UUID. The cache skips pinned originals when it enforces age, size, or minimum free space. Per-asset and whole-cache Eviction also reject pinned originals. Removing a Pin keeps the cached original. A failed download keeps its durable Pin and can be retried by another `pin` command, by Nautilus, or at the next service start.

The control API reports `pinned`, `cached`, `busy`, and `scheduled`. The CLI exposes `pin`, `unpin`, and `pin-status`. Nautilus exposes Pin, Unpin, retry, and state emblems within the configured mount.

## Remaining acceptance

Use one recorded Test asset on the target system. Pin it while uncached, verify the downloaded bytes, restart the service, and prove that both automatic and manual Eviction skip it. Then Unpin it, verify that the cached bytes remain, and Evict it. Do not Pin or Evict a Protected-library asset during mutation acceptance.
