# Ship Immich On-Demand 1.0

## Destination

Ship a personal 1.0 Arch package for the Reference system. Its Flat library previews common media without Hydration, downloads and evicts originals on demand, uploads new files, and optionally maps deletion to Immich trash without touching the Protected library during development.

## Notes

- This map includes execution through the 1.0 release, not planning alone.
- Use `grilling` and `domain-modeling` for product decisions, `research` for external facts, and `prototype` for behavior that must be proved on the Reference system.
- Apply Ponytail throughout: reuse platform and installed-library behavior before adding project code.
- The Flat library exposes every asset in one directory. Upload and download are format-agnostic within Immich's own API limits; the 1.0 Preview allowlist is intentionally small.
- Existing assets are immutable except for explicitly enabled Remote deletion. New files may be uploaded. Offline write queuing is not part of 1.0.
- The CLI must call a local control API rather than own settings or secrets. Store credentials in Secret Service. Support one server, user, and mount.
- Live tests may read the Protected library. Only Test assets may be changed or deleted.
- Release under GPL-3.0-or-later with an Arch package. Publishing to the AUR is not required for 1.0.

## Decisions so far

## Not yet specified

- The exact MVP and 1.0 implementation sequence after the stack and component boundaries are chosen.
- Catalog refresh, reconciliation, and persistence after the Immich API contract and real library shape are known.
- Upload recovery and Remote deletion safeguards after mutation semantics are fixed.
- The control API shape after process ownership is decided.
- Automated verification, migration, packaging, and release work after the architecture is proved.

## Out of scope

- Album, person, favorite, date, and other virtual views, including hardlinks between views. Revisit after the Flat library ships.
- Preview guarantees for RAW, HEIF, Live Photos, and uncommon media types. Add formats from real demand.
- Pinning and queued offline writes. Add them when the local state model has a demonstrated need.
- Multiple servers, users, mounts, other file managers, and other Linux distributions.
- Publishing the Arch package to the AUR.
