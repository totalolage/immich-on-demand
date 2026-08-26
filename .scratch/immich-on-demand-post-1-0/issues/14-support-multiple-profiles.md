# Support multiple Profiles

Type: feature
Status: in progress
Design: resolved
Target: 2.0
Target acceptance: pending

## Answer

Run one existing filesystem process per explicitly selected Profile. A small Profile module validates one immutable lowercase ID, derives its paths, and owns management and service lock lifetimes. `run_service(Profile)` locks the instance before loading config. Keep `Settings`, `Catalog`, `TrustedProfile`, `ContentCache`, `UploadQueue`, and the control protocol Profile-agnostic behind those selected paths; do not add a registry database, central daemon, or combined catalog.

Use literal `immich-on-demand@ID.service` instances, one private control socket and journal unit per ID, exact Profile-tagged Secret Service attributes, and separate XDG roots. CLI commands require `--profile ID`; the GUI exposes a visible selector; every Nautilus batch and action carries one explicit ID. A Profile lock is acquired before config load. After strict config read, hierarchical mount-path `flock` locks reject equal or nested mounts before credentials, catalog, cache, uploads, FUSE, or Immich are touched. Deterministic conflicts exit `78`; short global-lock contention remains retryable.

Require migration of the unprofiled installation to `default` before creating another Profile. Preflight and move only the exact catalog files, cache, and Pending uploads inside their XDG filesystems; copy and compare exact legacy secrets with `replace=False`; and move strict config last as the completion marker. New-Profile config is instead published before requested key writes so an interrupted key write leaves an active editable Profile.

The source implementation now covers selection, strict paths, locking, legacy migration, isolated credentials and local roots, CLI/service routing, the desktop selector, Nautilus routing, and systemd template instances. Reversible Profile retirement remains unimplemented, and the concurrent Reference-system matrix remains pending. The exact design and acceptance boundaries are in [the design](../../../docs/research/multiple-profile-boundaries.md).

## Scope

Namespace configuration, Secret Service items, catalog, cache, recovery bytes, control sockets, logs, and systemd units by Profile. Keep each Profile limited to one server, user, and mount.

## Acceptance

- Two Profiles run concurrently without sharing credentials, inodes, cache entries, control requests, or recovery files.
- The service rejects duplicate mount paths and conflicting Profile identifiers.
- A GUI and CLI select a Profile explicitly.
- A future reversible retirement operation cannot alter another Profile's local or remote state.
