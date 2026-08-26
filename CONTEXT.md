# Immich On-Demand

Immich On-Demand exposes an Immich library as a Linux filesystem whose original media is downloaded only when read.

## Language

**Reference system**:
The initial supported environment: Arch Linux, Niri, Nautilus 50, FUSE 3, and Immich 3.0.3. Other environments matter only where support costs little.
_Avoid_: Target system, every Linux desktop

**1.0 release**:
The first complete personal release for the Reference system, available for others to install. It previews common media, downloads originals on demand, and uploads new files created in the mounted Library.
_Avoid_: MVP, prototype, read-only release

**Flat library**:
The single mounted directory containing one entry for every visible Immich asset. Version 1.0 has no album, person, favorite, or date-based views.
_Avoid_: Timeline, virtual views

**Library root**:
The mounted top directory that contains the Flat library in version 1.0 and the top-level Views from version 1.3 onward.
_Avoid_: Library, mountpoint

**View**:
A directory hierarchy that presents a selected set of assets. When one asset appears in several Views, every entry refers to the same file identity and original bytes.
_Avoid_: Copy, duplicate library

**All View**:
The View that contains one entry for every visible asset. It is the only View that accepts Upload or Remote deletion through filesystem operations.
_Avoid_: Flat library, Inbox

**View alias**:
One mounted path to an asset inside a View. Every View alias for an asset shares its inode, Hydration, Pin, Preview source, and mutation identity.
_Avoid_: Copy, duplicate asset

**Library name**:
The stable filename assigned to one asset and reused by every View alias. The first deterministic occurrence keeps its sanitized original filename. A collision adds the complete asset ID before the extension.
_Avoid_: Original filename, server path

**Hydration**:
Downloading an asset's original content when an application first reads its Library entry.
_Avoid_: Preview, synchronization

**Eviction**:
Removing a hydrated original from the local cache while preserving its Library entry and Preview.
_Avoid_: Delete, trash

**Upload**:
Creating a new Immich asset from a new file written to the version 1.0 Flat library or the All View. Upload never replaces an existing asset.
_Avoid_: Update, overwrite, synchronization

**Pending upload**:
A complete private local file whose close finished but whose Immich creation has not been confirmed. Its staged mounted name remains locally readable until publication. The user can retry it idempotently or explicitly cancel its local bytes when no remote candidate may exist.
_Avoid_: Staged file, cached original, upload recovery

**Upload recovery**:
Private local bytes retained because the write did not finish or Upload completion is unknown. An incomplete recovery file is never retried automatically. A complete closed recovery file is a Pending upload.
_Avoid_: Pending upload, backup, cached original

**Asset replacement**:
Creating and verifying a new Immich asset from a temporary file renamed over an existing All entry, then moving the old asset to trash and transferring the stable mounted name. Immich On-Demand never overwrites an original in place.
_Avoid_: Update, overwrite, synchronization

**Remote deletion**:
Moving an Immich asset to Immich trash after the user has explicitly enabled destructive operations. It never means Eviction.
_Avoid_: Free up space, eviction

**Preview**:
An Immich-generated thumbnail displayed without Hydrating the asset. Version 1.0 promises Previews only for its small common-media allowlist.
_Avoid_: Original, hydration

**Live Photo**:
One visible still asset whose `livePhotoVideoId` identifies a separate motion asset. The motion asset remains cataloged but is not exposed as a View alias, regardless of its server visibility.
_Avoid_: Sidecar, duplicate file

**Pin**:
A local instruction to keep an asset's original Hydrated and exempt from Eviction. A Pin does not change Immich metadata.
_Avoid_: Favorite, download

**Favorite**:
Immich metadata that marks an asset for the user's Favorite View. A Favorite does not keep the original Hydrated.
_Avoid_: Pin, starred local file

**Profile**:
One Immich server, one authenticated Immich user, one mount, and their local state. Version 1.0 supports one Profile.
_Avoid_: Account, instance

**Protected library**:
Every Immich asset that existed before mutation testing began. Tests may read these assets but must never change or delete them.
_Avoid_: Test data

**Test asset**:
An asset created specifically for testing write and delete behavior. Mutation tests may change or delete only these assets.
_Avoid_: Protected library
