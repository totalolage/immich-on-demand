# Ship Immich On-Demand 1.0

## Destination

Ship a personal 1.0 Arch package for the Reference system. Its Flat library previews common media without Hydration, downloads and evicts originals on demand, uploads new files, and optionally maps deletion to Immich trash without touching the Protected library during development.

## Notes

- This map includes execution through the 1.0 release, not planning alone.
- Use `grilling` and `domain-modeling` for product decisions, `research` for external facts, and `prototype` for behavior that must be proved on the Reference system.
- Apply Ponytail throughout: reuse platform and installed-library behavior before adding project code.
- The Flat library exposes every asset in one directory. Upload and download are format-agnostic within Immich's own API limits. The 1.0 Preview allowlist is intentionally small.
- Existing assets are immutable except for explicitly enabled Remote deletion. New files may be uploaded. Offline write queuing is not part of 1.0.
- Startup requires online key validation, catalog refresh, and Preview suppression. A running mount keeps catalog and cached reads through later outages. Uncached reads fail until Immich returns.
- The CLI uses shared settings functions for setup and the local control API for live operations. The service alone loads Secret Service credentials. Support one server, user, and mount.
- Live tests may read the Protected library. Only Test assets may be changed or deleted.
- Release under GPL-3.0-or-later with an Arch package. Publishing to the AUR is not required for 1.0.

## Decisions so far

- [Establish the Immich 3.0.3 API contract](issues/01-establish-immich-api-contract.md): use stable REST routes with scoped keys, complete originals, checksum-safe uploads, and trash-only deletion.
- [Find the Nautilus 50 thumbnail route](issues/02-find-nautilus-thumbnail-route.md): populate the global FreeDesktop cache and suppress unsafe fallbacks per mounted URI.
- [Compare the minimal implementation stacks](issues/03-compare-minimal-implementation-stacks.md): one Python package and the FreeDesktop thumbnail cache cover the Reference system with the least project code.
- [Provision read-only live-test access](issues/04-provision-isolated-live-test-access.md): the verified read-only key lives in Secret Service and exposes no mutation permission.
- [Choose the implementation stack](issues/06-choose-the-implementation-stack.md): adopt Python, pyfuse3 with Trio, HTTPX, stdlib SQLite, SecretStorage, Unix-socket control, and the FreeDesktop thumbnail cache.
- [Inventory the real library shape](issues/05-inventory-the-real-library-shape.md): 11,237 visible entries include 1,892 duplicate-name groups, while every visible asset has byte-size metadata.
- [Define Flat library names](issues/07-define-flat-library-names.md): preserve the first safe original basename and suffix collisions with the complete asset ID.
- [Define the mutation contract](issues/08-define-the-mutation-contract.md): keep existing assets immutable, upload once on last close, retain failed staging, and allow only guarded trash.
- [Prove thumbnail isolation in Nautilus 50](issues/09-prove-thumbnail-isolation.md): install per-URI failure records before exposure, then install bounded Immich Previews for supported media.
- [Prove Hydration and video reads](issues/10-prove-hydration-and-video-reads.md): share one complete-file Hydration and serve later offset reads from the validated local original.
- [Choose the cache policy](issues/11-choose-the-cache-policy.md): use explicit access time, LRU age/size/free-space limits, atomic complete files, and busy-asset exclusion.
- [Set the component boundaries](issues/12-set-the-component-boundaries.md): keep policy in the library and service while FUSE, CLI, control, catalog, cache, and Preview modules stay narrow.
- Reference-system acceptance on 2026-08-25: Nautilus followed its saved sort with zero browsing Hydration; explicit image and video opens hydrated once; one isolated fixture passed upload, byte-for-byte readback, and guarded trash.
- Release cut on 2026-08-25: the public `v1.0.0` tag, verified wheel, pinned Arch source checksum, package install, systemd user service, and mounted status all passed.

## Result

Version 1.0.0 is released. The [post-1.0 map](../immich-on-demand-post-1-0/map.md) owns every deferred feature below.

## Out of scope

- Album, person, favorite, date, and other virtual views, including hardlinks between views. Revisit after the Flat library ships.
- Preview guarantees for RAW, HEIF, Live Photos, and uncommon media types. Add formats from real demand.
- Pinning and queued offline writes. Add them when the local state model has a demonstrated need.
- Multiple servers, users, mounts, other file managers, and other Linux distributions.
- Publishing the Arch package to the AUR.
