# Add Nautilus actions and a settings GUI

Type: feature
Status: in progress
Target: 1.1
Blocked by: none

## Scope

Extend the private control protocol only for operations the clients need. Add a globally loaded Nautilus provider that returns actions and emblems only within the configured mount. Add a GUI over the existing settings and Secret Service code. Keep credentials and remote policy out of the control protocol.

## Acceptance

- The loaded Nautilus provider returns no actions or emblems outside the configured Immich mount.
- The GUI configures one Profile, stores keys in Secret Service, and controls the running service without importing FUSE code.
- Both clients use bounded local requests and display service errors without secrets.
- Removing either client leaves the daemon and CLI functional.
