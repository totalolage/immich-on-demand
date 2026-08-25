# Queue and retry uploads

Type: feature
Status: open
Target: 1.2
Blocked by: 05, 07

## Scope

Turn retained recovery bytes into explicit Pending uploads with status, retry, and cancel operations. Keep upload attempts idempotent through checksum duplicate checks. Never hide a failure behind a successful file close.

## Acceptance

- A close during an outage leaves one private Pending upload with its requested name and error.
- Reconnection or an explicit retry creates at most one Immich asset.
- Cancel removes only local Pending bytes after explicit confirmation.
- Status survives service restart and is visible through CLI, Nautilus, and GUI clients.
