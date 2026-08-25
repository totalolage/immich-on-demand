# Replace full catalog sweeps with bounded incremental refresh

Type: feature
Status: resolved
Target: 1.0.x
Blocked by: none

## Scope

Use `updatedAfter` with overlap and deduplication to reduce routine catalog traffic. Keep a periodic complete stable sweep because API-key clients cannot use the Immich Sync API and search pagination has no snapshot token.

## Acceptance

- Inserts, metadata changes, trash, restore, duplicate timestamps, and interrupted pagination converge without losing catalog rows.
- A routine no-change refresh transfers fewer pages than the 1.0 complete sweep.
- A periodic complete sweep detects deletions and repairs missed incremental changes.

## Answer

Routine refreshes query from the committed high-water timestamp minus twice the configured refresh interval. They accept shifted duplicate IDs, stage at most the page count of the last complete sweep, and atomically upsert without deleting absent rows. Startup, manual repair, the daily reconciliation, and an over-budget delta use the existing paired stable full sweep. The catalog cursor and page budget advance only in the same transaction that publishes staged rows.

The read-only Reference-server proof reduced one quiet routine refresh from 28 page requests for a paired complete sweep to one request.
