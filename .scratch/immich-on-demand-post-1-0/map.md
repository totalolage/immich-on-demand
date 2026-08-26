# Immich On-Demand after 1.0

## Destination

Extend the released Flat library without weakening its safety rules. Incremental refresh, Pin, Restore, trusted offline startup, queued uploads, and the first desktop controls are implemented but await target acceptance. Later releases may add Views, broader media support, Asset replacement, multiple Profiles, and more platforms.

## Rules

- Keep the Protected library read-only in tests. Mutation tests may touch only recorded Test assets.
- Keep Pin and Favorite separate. Pin is local residency. Favorite is Immich metadata.
- Reuse one asset identity across Views. Do not copy original bytes for each path.
- Keep every remote destructive operation opt-in, owner-checked, and recoverable through Immich trash.
- Add a release only when its acceptance check passes on the Reference system.

## Release sequence

### 1.0.x: distribute and refine

- [Publish the Arch package to the AUR](issues/01-publish-the-arch-package-to-the-aur.md)
- [Follow Nautilus sort changes without restart](issues/02-follow-nautilus-sort-changes.md) is resolved.
- [Bounded incremental refresh](issues/03-add-incremental-refresh.md) is implemented. Target package and service acceptance remain.

### 1.1: control local residency

- [Nautilus actions and a settings GUI](issues/05-add-desktop-controls.md) are implemented in the source tree. Package integration and target acceptance remain.
- [Pinning](issues/04-add-pinning.md), including CLI `pin-status`, is implemented. Target acceptance remains.
- [Explicit trash Restore](issues/06-add-trash-restore.md) is implemented. Target Test-asset acceptance remains.

### 1.2: work through outages

- [Start from trusted cached state while Immich is offline](issues/07-start-offline.md) is implemented. Target acceptance remains.
- [Queue and retry uploads](issues/08-queue-and-retry-uploads.md) is implemented. Crash and target-system acceptance remain.

### 1.3: add rich library Views

- [Define the multi-View namespace](issues/09-define-the-multi-view-namespace.md)
- [Add All, Album, People, Date, and Favorite Views](issues/10-add-rich-library-views.md)

### 1.4: expand mutation and media behavior

- [Add Asset replacement](issues/11-add-asset-replacement.md)
- [Add RAW, HEIF, and Live Photo Previews](issues/12-broaden-preview-support.md)
- [Evaluate partial Hydration](issues/13-evaluate-partial-hydration.md)

### 2.0: support more environments

- [Support multiple Profiles](issues/14-support-multiple-profiles.md)
- [Support more file managers and Linux distributions](issues/15-support-more-platforms.md)

## Deferred until evidence exists

- Do not add a plugin framework, network control service, or second daemon.
- Do not implement partial Hydration until Immich documents or reliably exposes byte ranges with a whole-file fallback.
- Do not infer the user's visible Nautilus viewport. Saved folder sort is the available supported priority signal.
