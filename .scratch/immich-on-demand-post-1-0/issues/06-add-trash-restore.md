# Add an explicit trash restore command

Type: feature
Status: implemented
Target: 1.1
Blocked by: 05
Target acceptance: pending

## Scope

Expose the existing Immich restore operation through the service and CLI, then add it to the GUI. Require the exact deletion permission and an explicit asset identity. Do not make Restore a filesystem side effect.

## Acceptance

- Restore acts on one recorded Test asset and verifies that Immich restored exactly one asset.
- A catalog refresh makes the restored entry visible with its stable Library name and inode.
- Missing permission, wrong ownership, disabled trash, and unknown asset identity fail closed.

## Answer

The CLI exposes `immich-on-demand restore --asset UUID`. Restore requires the `--enable-remote-delete` opt-in. At startup, the service requires the mutation key to have exactly `user.read`, `asset.read`, `asset.view`, `asset.download`, `asset.upload`, and `asset.delete`. The service accepts only a canonical UUID for a known, trashed asset owned by the mutation user.

Before every Restore, the Immich client fetches the current server features and requires literal `trash: true`. It sends one asset UUID to `POST /trash/restore/assets` and accepts only an integer response count of one. Generic client and control errors do not include response bodies or credentials.

After remote success, the catalog clears the row's trashed state without replacing the row. The Library name and inode remain stable. The service then schedules an incremental refresh. Restore has no filesystem operation or Nautilus menu.

The settings GUI provides a transient asset UUID field and a Restore button. It canonicalizes the UUID, uses the bounded desktop worker, does not save the UUID, and displays fixed result text.

## Remaining acceptance

Run the installed CLI and GUI against only the recorded trashed Test asset. Verify that the restored asset returns with its previous Library name and inode. Never Restore a Protected-library asset. The missing-permission, wrong-owner, disabled-trash, unknown-asset, response-schema, and control-error cases have automated coverage but have not completed target-system acceptance.
