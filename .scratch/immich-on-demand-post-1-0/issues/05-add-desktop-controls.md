# Add Nautilus actions and a settings GUI

Type: feature
Status: open
Target: 1.1
Blocked by: none

## Scope

Extend the private control protocol only for operations the clients need. Add mount-scoped Nautilus actions and emblems for Eviction, refresh, and recovery state. Add a GUI over the existing settings and Secret Service code. Keep credentials and remote policy out of both clients.

## Acceptance

- Nautilus loads the extension only for configured Immich mounts.
- The GUI configures one Profile, stores keys in Secret Service, and controls the running service without importing FUSE code.
- Both clients use bounded local requests and display service errors without secrets.
- Removing either client leaves the daemon and CLI functional.
