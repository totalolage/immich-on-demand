# Immich On-Demand after 1.0

## Destination

Extend the released Flat library without weakening its safety rules. Incremental refresh, Pin, Restore, trusted offline startup, queued uploads, the first desktop controls, rich Views, Asset replacement, broader image Previews, isolated multiple Profiles, and reversible Profile retirement are implemented but await target acceptance. Later releases may add more platforms.

## Rules

- Keep the Protected library read-only in tests. Mutation tests may touch only recorded Test assets.
- Keep Pin and Favorite separate. Pin is local residency. Favorite is Immich metadata.
- Reuse one asset identity across Views. Do not copy original bytes for each path.
- Keep every remote destructive operation opt-in, owner-checked, and recoverable through Immich trash.
- Add a release only when its acceptance check passes on the Reference system.

## Release sequence

### 1.0.x: distribute and refine

- [Publish the Arch package to the AUR](issues/01-publish-the-arch-package-to-the-aur.md) remains blocked while the AUR does not accept account signups. The released recipe remains version 1.0.0.
- [Follow Nautilus sort changes without restart](issues/02-follow-nautilus-sort-changes.md) is resolved.
- [Bounded incremental refresh](issues/03-add-incremental-refresh.md) is implemented. Target package and service acceptance remain.

### 1.1: control local residency

- [Nautilus actions and a settings GUI](issues/05-add-desktop-controls.md) are implemented with a development Arch recipe. Target acceptance remains.
- [Pinning](issues/04-add-pinning.md), including CLI `pin-status`, is implemented. Target acceptance remains.
- [Explicit trash Restore](issues/06-add-trash-restore.md) is implemented. Target Test-asset acceptance remains.

### 1.2: work through outages

- [Start from trusted cached state while Immich is offline](issues/07-start-offline.md) is implemented. Target acceptance remains.
- [Queue and retry uploads](issues/08-queue-and-retry-uploads.md) is implemented. Crash and Reference-system acceptance remain.

### 1.3: add rich library Views

- [The multi-View namespace](issues/09-define-the-multi-view-namespace.md) is resolved.
- [All, Album, People, Date, and Favorite Views](issues/10-add-rich-library-views.md) are implemented. The Reference namespace, hardlinks, link counts, and no-Hydration traversal passed; server-inventory and Favorite-versus-Pin checks remain.

### 1.4: expand mutation and media behavior

- [Asset replacement](issues/11-add-asset-replacement.md) is implemented. Reference-system acceptance with project-owned Test assets remains.
- [RAW, HEIF, and Live Photo Previews](issues/12-broaden-preview-support.md) are implemented. The Reference Live Photo inventory and one still Preview passed the read-only probe; mounted persistence and representative RAW and HEIF acceptance remain.
- [Partial Hydration](issues/13-evaluate-partial-hydration.md) was evaluated and deferred. Complete-file Hydration remains until the reopening gates pass.

### 2.0: support more environments

- [Support multiple Profiles](issues/14-support-multiple-profiles.md) has implemented isolation, routing, and reversible retirement in source. Concurrent target acceptance remains.
- [Support more file managers and Linux distributions](issues/15-support-more-platforms.md) selected Ubuntu Desktop 26.04 with Nautilus 50 as the next acceptance target and now has a native `.deb` source recipe. Support remains unclaimed until the runtime and package matrices pass.

## Deferred until evidence exists

- Do not add a plugin framework, network control service, or second daemon.
- Do not implement partial Hydration until every reopening criterion in issue 13 passes, including measured benefit, stable public-origin behavior, a strong byte validator or chunk hashes, and crash-safe whole-file fallback.
- Do not infer the user's visible Nautilus viewport. Saved folder sort is the available supported priority signal.
