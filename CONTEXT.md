# Immich On-Demand

Immich On-Demand exposes an Immich library as a Linux filesystem whose original media is downloaded only when read.

## Language

**Reference system**:
The initial supported environment: Arch Linux, Niri, Nautilus 50, FUSE 3, and Immich 3.0.3. Other environments matter only where support costs little.
_Avoid_: Target system, every Linux desktop

**1.0 release**:
The first complete personal release for the Reference system, available for others to install. It previews common media, downloads originals on demand, and uploads new files created in the Flat library.
_Avoid_: MVP, prototype, read-only release

**Flat library**:
The single mounted directory containing one entry for every visible Immich asset. Version 1.0 has no album, person, favorite, or date-based views.
_Avoid_: Timeline, virtual views

**Library name**:
The stable filename assigned to one asset in the Flat library. The first deterministic occurrence keeps its sanitized original filename; a collision adds the complete asset ID before the extension.
_Avoid_: Original filename, server path

**Hydration**:
Downloading an asset's original content when an application first reads its Library entry.
_Avoid_: Preview, synchronization

**Eviction**:
Removing a hydrated original from the local cache while preserving its Library entry and Preview.
_Avoid_: Delete, trash

**Upload**:
Creating a new Immich asset from a new file written to the Flat library. Upload never replaces an existing asset.
_Avoid_: Update, overwrite, synchronization

**Remote deletion**:
Moving an Immich asset to Immich trash after the user has explicitly enabled destructive operations. It never means Eviction.
_Avoid_: Free up space, eviction

**Preview**:
An Immich-generated thumbnail displayed without Hydrating the asset. Version 1.0 promises Previews only for its small common-media allowlist.
_Avoid_: Original, hydration

**Protected library**:
Every Immich asset that existed before mutation testing began. Tests may read these assets but must never change or delete them.
_Avoid_: Test data

**Test asset**:
An asset created specifically for testing write and delete behavior. Mutation tests may change or delete only these assets.
_Avoid_: Protected library
