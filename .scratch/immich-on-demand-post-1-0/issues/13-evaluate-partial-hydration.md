# Evaluate partial Hydration

Type: prototype
Status: open
Target: 1.4
Blocked by: none

## Scope

Measure byte-range behavior for Immich originals and video playback on the deployed server. Add a sparse cache only if capability detection, integrity, concurrent reads, Eviction, and a whole-file fallback stay simpler than the bandwidth they save.

## Acceptance

- The prototype proves range behavior across restart, proxy, and Immich upgrade boundaries.
- Unsupported or inconsistent servers fall back to the 1.0 complete-file cache.
- Interrupted and overlapping reads never publish corrupt bytes.
- A measured Reference-system workload justifies the added state and code.
