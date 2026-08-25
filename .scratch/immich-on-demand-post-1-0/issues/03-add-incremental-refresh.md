# Replace full catalog sweeps with bounded incremental refresh

Type: research
Status: open
Target: 1.0.x
Blocked by: none

## Scope

Use `updatedAfter` with overlap and deduplication to reduce routine catalog traffic. Keep a periodic complete stable sweep because API-key clients cannot use the Immich Sync API and search pagination has no snapshot token.

## Acceptance

- Inserts, metadata changes, trash, restore, duplicate timestamps, and interrupted pagination converge without losing catalog rows.
- A routine no-change refresh transfers fewer pages than the 1.0 complete sweep.
- A periodic complete sweep detects deletions and repairs missed incremental changes.
