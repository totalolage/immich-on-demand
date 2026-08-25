# Support multiple Profiles

Type: feature
Status: open
Target: 2.0
Blocked by: 05, 07, 09

## Scope

Namespace configuration, Secret Service items, catalog, cache, recovery bytes, control sockets, logs, and systemd units by Profile. Keep each Profile limited to one server, user, and mount.

## Acceptance

- Two Profiles run concurrently without sharing credentials, inodes, cache entries, control requests, or recovery files.
- The service rejects duplicate mount paths and conflicting Profile identifiers.
- A GUI and CLI select a Profile explicitly.
- Removing one Profile cannot alter another Profile's local or remote state.
