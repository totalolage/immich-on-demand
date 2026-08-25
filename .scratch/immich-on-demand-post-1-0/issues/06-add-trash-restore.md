# Add an explicit trash restore command

Type: feature
Status: open
Target: 1.1
Blocked by: 05

## Scope

Expose the existing Immich restore operation through the service and CLI, then add it to the GUI. Require the exact deletion permission and an explicit asset identity. Do not make Restore a filesystem side effect.

## Acceptance

- Restore acts on one recorded Test asset and verifies that Immich restored exactly one asset.
- A catalog refresh makes the restored entry visible with its stable Library name and inode.
- Missing permission, wrong ownership, disabled trash, and unknown asset identity fail closed.
