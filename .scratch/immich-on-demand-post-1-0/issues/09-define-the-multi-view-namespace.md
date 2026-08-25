# Define the multi-View namespace

Type: decision
Status: open
Target: 1.3
Blocked by: none

## Scope

Replace the Flat library root with stable top-level Views such as `All`, `Albums`, `People`, `by Date`, and `Favorites`. Reuse one inode for an asset across Views so repeated entries behave as hardlinks and share Hydration, Pin, Preview, and mutation state.

## Acceptance

- The namespace defines escaping, collisions, empty values, deleted collections, renamed collections, and assets with many memberships.
- One asset shown in several Views reports one inode and one link count that matches visible entries.
- A refresh never renames an existing asset entry because a later collision arrives.
- The migration from the Flat library preserves local cache and catalog identity.
