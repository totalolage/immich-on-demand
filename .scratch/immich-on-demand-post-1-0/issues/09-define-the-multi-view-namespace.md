# Define the multi-View namespace

Type: decision
Status: resolved
Target: 1.3
Blocked by: none

## Scope

Replace the Flat library root with stable top-level Views such as `All`, `Albums`, `People`, `by Date`, and `Favorites`. Reuse one inode for an asset across Views so repeated entries behave as hardlinks and share Hydration, Pin, Preview, and mutation state.

## Acceptance

- The namespace defines escaping, collisions, empty values, deleted collections, renamed collections, and assets with many memberships.
- One asset shown in several Views reports one inode and one link count that matches visible entries.
- A refresh never renames an existing asset entry because a later collision arrives.
- The migration from the Flat library preserves local cache and catalog identity.

## Answer

The Library root contains `All`, `Albums`, `People`, `by Date`, and `Favorites`. The All View contains one View alias for every visible asset. It is the only View that accepts create or unlink. The other Views are read-only.

Every View alias uses the asset's existing Library name and inode. The catalog stores each active directory-to-asset membership once and calculates `st_nlink` from the active aliases. Repeated album membership or face matches collapse to one alias. Listing and lookup use only this materialized local projection.

Collection directories use the stable album ID, person ID, or derived date key as identity. The first observed safe label becomes the mounted name. Empty labels use `Unnamed` plus the full stable ID. A sibling collision appends the full stable ID without renaming an existing directory. A later server rename is observed during reconciliation but does not change the persisted mounted name. A deleted collection becomes inactive but keeps its inode and name reservation. The same ID can therefore return at the same path. Empty server Albums remain visible. Derived date directories disappear when empty but keep their identity reservation.

The catalog exposes `node`, `lookup`, `children`, and `aliases`. FUSE, URI controls, and Preview handling use this interface and do not know View rules. Remote adapters validate complete Album and People facts before the catalog replaces their projection. The catalog remains the only writer and publishes each projection in one SQLite transaction.

Migration keeps every asset row, inode, Library name, Pin, and asset-ID cache key. It allocates only directory inodes and moves the visible namespace under All and by Date. URI-keyed Preview records are rebuilt for the new aliases without Hydration. A failed migration leaves the previous transaction intact and prevents the mount.
