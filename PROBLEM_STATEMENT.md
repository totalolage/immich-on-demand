# Immich On-Demand for Linux

## Document Status

- Status: Initial problem statement
- Date: 2026-08-25
- Target desktop: Linux, Nautilus, Wayland
- Initial machine: Arch Linux, Niri, Nautilus 50, FUSE 3
- Initial Immich server: Immich 3.0.3 at `https://photos.nas.kalny.net`

## Summary

Immich is an effective self-hosted photo and video manager, but it does not expose a user's library as a Linux filesystem with Dropbox-style files on demand. The desired experience is a directory such as `~/Photos` in which remote Immich assets are immediately visible, Nautilus displays their thumbnails without downloading the original media, and any normal application can open a file to transparently download the original. Downloaded originals should be cached locally and automatically evicted after a configurable period of inactivity or when a cache size limit is reached. Eviction must preserve the asset's directory entry, metadata, and thumbnail.

Immich already exposes the APIs needed to enumerate assets, retrieve metadata, retrieve small server-generated thumbnails, and download originals. Linux FUSE can expose a userspace filesystem and defer content reads. The missing piece is a robust integration between those APIs, a local metadata and content cache, FUSE, and Nautilus's thumbnail behavior.

This project will investigate and implement that integration as an Immich utility and Nautilus integration. It must avoid direct access to Immich's database or storage layout and use only supported Immich APIs.

## User Problem

A user's Immich library can be much larger than the available storage on a laptop. The user nevertheless wants the library to participate in ordinary desktop workflows:

- Browse photos and videos in Nautilus.
- See useful thumbnails before deciding which asset to open.
- Open an asset from any application through a normal filesystem path.
- Attach an Immich asset in a browser or email file picker without first exporting it manually.
- Copy an original to another location using normal filesystem tools.
- Avoid storing the full library locally.
- Keep recently used originals available for a while.
- Reclaim local storage automatically without making assets disappear.
- Optionally add new local media through a well-defined upload workflow.

The Immich web application already provides an excellent gallery, but it does not satisfy workflows that require a filesystem path. Existing upload tools solve ingestion but do not provide a browsable remote filesystem. Generic remote mounts provide on-demand reads but do not use Immich's thumbnails, organization, or API semantics.

## Desired Experience

The intended user-visible behavior is similar to Dropbox Smart Sync, OneDrive Files On-Demand, or macOS File Provider:

1. The user logs in once with an Immich API key.
2. A user service mounts the Immich library at a stable path such as `~/Photos`.
3. Directory listings are built from locally cached Immich metadata and do not download original media.
4. Nautilus requests and displays Immich-generated thumbnails.
5. Thumbnail generation never opens or downloads the original asset.
6. Opening, copying, or otherwise reading a file downloads the original transparently.
7. The downloaded original is cached so repeated access does not require another download.
8. Cached originals are evicted after a configurable period of inactivity or under storage pressure.
9. Eviction leaves the virtual file, metadata, and thumbnail visible.
10. A manual "free up space" action can evict selected assets or the entire content cache.
11. Restarting the service or laptop preserves the catalog, thumbnail cache, and safe portions of the content cache.
12. Loss of network access produces clear errors without corrupting local state.

The result should work as a normal filesystem for applications that are not aware of Immich. Nautilus-specific integration should improve thumbnailing, state display, and user controls without being required for basic reads.

## Core Technical Problem

### Linux Has No General Cloud Placeholder API

Windows provides the Cloud Files API and macOS provides File Provider. These APIs distinguish placeholders from hydrated files and provide dedicated thumbnail hooks. Linux does not currently provide an equivalent desktop-wide cloud placeholder API.

FUSE is the practical Linux mechanism for exposing remote assets as normal filesystem entries. It can defer network reads until an application accesses file content, but it does not inherently provide:

- A standardized placeholder state visible to desktop applications.
- A separate thumbnail data channel.
- Hydrate, pin, and evict shell actions.
- Operating-system-managed cache lifecycle.
- Standardized cloud availability or synchronization emblems.

Those behaviors must be implemented by this project and integrated with Nautilus where possible.

### Nautilus Thumbnailing Can Accidentally Hydrate Originals

A generic Nautilus thumbnailer receives a file URI or local path and opens the file to render a thumbnail. On a FUSE mount, that read is indistinguishable from a user opening the file. A naive Immich FUSE implementation therefore downloads originals merely because a directory became visible in Nautilus.

This is the central correctness requirement: obtaining a thumbnail must use Immich's thumbnail endpoint and must not trigger the FUSE original-content read path.

The FreeDesktop thumbnail cache provides useful prior art. Cached thumbnails are PNG files keyed by the MD5 hash of the canonical source URI and contain metadata such as `Thumb::URI`, `Thumb::MTime`, and optionally `Thumb::Size`. A viable integration may pre-populate this cache from Immich thumbnails, provide a Nautilus-aware thumbnail service, expose suitable GIO attributes, or combine these approaches. The exact mechanism must be validated against current Nautilus and GNOME APIs.

The implementation must not globally replace JPEG or video thumbnailing for unrelated local files.

### Immich Is Not Fundamentally A Filesystem

Immich's model does not map one-to-one onto a directory hierarchy:

- An asset can belong to zero, one, or many albums.
- Albums are not nested.
- The timeline is date-based rather than path-based.
- Multiple assets can have the same original filename.
- The same asset may appear through timeline, album, person, favorite, place, or tag views.
- A single original can therefore have multiple virtual paths.
- Some assets are external-library assets with different write semantics.
- Live Photos consist of related image and video assets.
- Edited representations, previews, transcoded video, sidecars, and originals are distinct concepts.

The filesystem namespace must provide stable, collision-free paths without pretending that every virtual path is an independent remote file. It must retain the Immich asset ID as the canonical identity even when names or organizational views change.

### Cache Semantics Need To Be Explicit

The system needs at least three distinct local stores:

| Store | Purpose | Expected lifetime |
| --- | --- | --- |
| Catalog | Asset IDs, names, sizes, timestamps, view membership, synchronization state | Persistent |
| Thumbnail cache | Small server-generated visual previews | Persistent and inexpensive |
| Original-content cache | Full or partial original media downloaded on access | Bounded and evictable |

These stores must not be conflated. Clearing originals must not clear the catalog or thumbnails. Thumbnail requests must not update original-content access times. Merely listing or statting a file must not hydrate it.

The cache manager must define:

- What counts as access for eviction purposes.
- Age-based eviction.
- Size-based least-recently-used eviction.
- Minimum free-space protection.
- Behavior for open files.
- Atomic completion of downloads.
- Cleanup of interrupted downloads.
- Whether partial and ranged downloads are retained.
- Manual pin and unpin behavior, if implemented.
- Integrity verification before a cached file is treated as complete.

### Synchronization And Mutation Semantics Are Ambiguous

"Sync `~/Photos`" can mean several different things:

- Present a read-only remote library through a local path.
- Upload local files placed in a directory.
- Mirror remote additions and deletions into a local catalog.
- Reflect local deletes as Immich trash operations.
- Reflect local moves as album membership changes.
- Keep a byte-for-byte offline copy.

These are materially different products. The initial implementation should prioritize a safe read-only, on-demand remote view. Write support should be added only after each filesystem operation has an explicit Immich meaning and recovery behavior. A local `unlink` must never become a destructive server operation by accident.

## Constraints

### Immich Constraints

- All communication must use the supported Immich HTTP API.
- The client must not read or modify the Immich database directly.
- The client must not depend on Immich's internal upload directory layout.
- The client must support scoped API keys and request the minimum permissions needed.
- The client must tolerate compatible API evolution across supported Immich versions.
- Asset identity must be based on Immich asset IDs, not filenames.
- Remote trash, album membership, archived state, visibility, and external-library restrictions must be respected.

Relevant API capabilities include:

- Asset and timeline enumeration.
- Album enumeration and membership.
- Asset metadata retrieval.
- `GET /assets/{id}/thumbnail` for generated thumbnails and previews.
- `GET /assets/{id}/original` for original downloads.
- Video playback endpoints with byte-range support where appropriate.
- Asset upload and album APIs for future write support.
- Immich event or polling mechanisms for catalog refresh, subject to API support.

Expected read-only API key permissions include `asset.read`, `asset.view`, and `asset.download`. Additional views require permissions such as `album.read`, `person.read`, and `tag.read`. Upload or mutation features must request their additional permissions separately.

### Filesystem Constraints

- Directory entries must have stable inode and path behavior within practical limits.
- `stat` and directory listing must not download content.
- Reported file sizes and timestamps should match Immich metadata.
- Reads must return bytes identical to the selected Immich representation.
- Concurrent readers of one asset should share a download rather than duplicate it.
- Interrupted reads must not expose a corrupt file as complete.
- Seek behavior must be defined and efficient enough for image viewers, video tools, and file copies.
- Duplicate filenames require deterministic collision handling.
- The mount must fail safely when the daemon is unavailable.
- Local cache paths must not be presented as authoritative user files.

### Desktop Constraints

- Nautilus must show thumbnails without reading originals.
- The solution must work in file choosers and arbitrary applications through normal paths.
- The service should run as an unprivileged user.
- Mounting and unmounting should be managed by a systemd user service or an equivalent desktop session mechanism.
- API keys must be stored in Secret Service or another appropriate credential store, not plaintext configuration.
- The project should work under Wayland and must not depend on X11 behavior.
- The core mount must remain usable without the Nautilus extension, although thumbnails and shell actions may be reduced.

## Existing Solutions And Reusable Prior Art

No existing project found during the initial investigation satisfies all requirements on Linux. Several projects solve important parts of the problem and should be studied rather than reimplemented blindly.

### Immich Web And Mobile Clients

Links:

- <https://immich.app/>
- <https://github.com/immich-app/immich>

What they do well:

- Correct, current Immich API usage.
- Efficient timeline and album enumeration.
- Server-generated thumbnail and preview use.
- Lazy loading of visible assets.
- Correct handling of media types, Live Photos, edits, playback, and permissions.
- Realtime update behavior and API compatibility patterns.

What to leverage:

- API endpoint selection and generated client definitions.
- Timeline pagination and bucketing strategies.
- Thumbnail-size selection.
- Current asset naming, permission, and error semantics.
- Tests and fixtures for Immich API behavior.

Limitations for this project:

- They do not expose filesystem paths on Linux.
- Browser caching is not a general-purpose original-content cache.

### Official Immich CLI

Link: <https://docs.immich.app/features/command-line-interface>

What it does well:

- Officially supported authentication and upload API use.
- Recursive initial upload.
- SHA-based duplicate checks.
- `--watch` support for new and changed files.
- Album assignment and folder-derived albums.
- Safe dry-run and structured output options.
- Optional deletion only after upload processing.

What to leverage:

- Upload request construction.
- Server-side duplicate handling.
- Supported-media discovery.
- File stability and batching behavior.
- API permission checks.

Limitations for this project:

- It is an ingestion tool, not a download or mount client.
- It does not create placeholders or thumbnails.
- Watch mode does not provide a general two-way filesystem synchronization model.

### Immich External Libraries

Link: <https://docs.immich.app/features/libraries>

What they do well:

- Index an existing server-visible filesystem without duplicating originals into normal Immich-managed storage.
- Preserve a filesystem as the source of truth.
- Support scheduled scans and experimental file watching.
- Provide folder-oriented browsing for curated libraries.

What to leverage:

- Understanding of external asset restrictions and metadata lifecycle.
- Folder-view expectations and path-derived identity issues.

Limitations for this project:

- They operate in the opposite direction: Immich reads a server-side filesystem.
- They do not make Immich assets available as a client-side filesystem.
- Pointing an external library at an intermittently connected laptop would produce incorrect offline and deletion behavior.

### Mimick

Link: <https://github.com/nicx17/mimick>

What it does well:

- Native Linux GTK4 and libadwaita client.
- Explicit Immich v3 support.
- Background local-folder upload synchronization.
- Built-in thumbnail library browser, albums, people, search, and lightbox.
- Secure API-key storage through Secret Service or a Flatpak-safe encrypted store.
- Local state, retry handling, endpoint failover, and desktop integration.
- Flatpak packaging and portal integration.

What to leverage:

- Linux credential storage patterns.
- Immich v3 API integration.
- GTK, libadwaita, Flatpak, and desktop-session packaging lessons.
- Local catalog and retry design.
- Upload watcher behavior if write support is added.

Limitations for this project:

- It provides an application gallery, not a mounted filesystem.
- Dragging or exporting creates ordinary local copies rather than on-demand placeholders.

### Immich FUSE

Link: <https://github.com/AlessandroLorenzi/immich-fuse>

What it does well:

- Demonstrates a direct mapping from Immich API objects to FUSE paths.
- Provides useful virtual views for date, favorites, and people.
- Uses Immich IDs in generated filenames to avoid identity ambiguity.
- Caches some API metadata with bounded TTL caches.
- Shows that a minimal read-only Immich FUSE prototype is straightforward.

What to leverage:

- Basic path-to-asset-ID mapping.
- Virtual-view concepts.
- Simple FUSE operation flow for early prototypes.

Limitations for this project:

- Small proof of concept with no release or declared license at the time of investigation.
- Last code activity was in April 2025.
- Downloads an entire original into memory when a read starts.
- Does not stream or perform ranged reads.
- Has no persistent catalog or disk content cache.
- Has no cache eviction, thumbnail endpoint integration, write support, or production lifecycle management.
- Its API assumptions need validation against Immich v3.

### Immich SFTP Server

Link: <https://github.com/Demian98/immich-sftp-server>

What it does well:

- Exposes Immich albums and assets through a standard file protocol.
- Uses only official Immich APIs.
- Implements coherent upload, deduplication, album creation, removal, trash, and restore semantics.
- Supports Immich v3.
- Keeps no authoritative asset data in the bridge.
- Can be consumed by mature SFTP clients and generic mount tools.

What to leverage:

- Mapping filesystem mutations to Immich album and trash operations.
- Duplicate and restore behavior.
- Filename and album edge-case handling.
- Integration tests around SFTP-like filesystem operations.

Limitations for this project:

- Exposes albums only, so unalbumed timeline assets are absent.
- Immich has no nested albums, so subdirectories cannot be represented naturally.
- Duplicate album names and duplicate asset filenames are problematic.
- Downloads provide originals, not a separate thumbnail channel.
- Authentication uses Immich email and password rather than a narrowly scoped API key.
- A generic SFTP mount does not solve Nautilus thumbnail hydration.

### rclone Mount

Links:

- <https://rclone.org/commands/rclone_mount/>
- <https://rclone.org/sftp/>

What it does well:

- Mature cross-platform FUSE implementation.
- On-demand remote reads.
- Full VFS disk cache mode.
- Age-based cache eviction through `--vfs-cache-max-age`.
- Size-based least-recently-used eviction through `--vfs-cache-max-size`.
- Minimum free-space protection.
- Read chunking, seeking, buffering, write-back, and concurrency controls.
- Operational experience across many remote storage providers.

What to leverage:

- Cache lifecycle semantics and configuration model.
- Sparse and ranged cache design.
- Open-file eviction protection.
- Metrics, logs, remote-control operations, and service management.
- Tests for difficult FUSE and application I/O patterns.

Limitations for this project:

- Immich is not an rclone backend.
- Using it through the SFTP bridge inherits the bridge's album and authentication limitations.
- Nautilus thumbnailing still reads the mounted original.
- Generic remotes cannot provide Immich-specific virtual views or mutation semantics.

### Findich And Immich Desktop For macOS

Links:

- <https://github.com/Majorfi/immich-in-finder>
- <https://github.com/Kartax/immich-desktop-app>

What they do well:

- Provide the closest existing implementation of the desired product behavior.
- Use macOS File Provider placeholders.
- Fetch Immich thumbnails separately from originals.
- Hydrate originals on demand.
- Expose albums, timeline, people, places, tags, and favorites as virtual folders.
- Handle collision-prone names and large virtual directories.
- Support manual "free up space" eviction.
- Define practical minimum API-key scopes.
- Findich implements upload and several filesystem-to-Immich mutation mappings.

What to leverage:

- Immich API client behavior.
- Virtual namespace and pagination design.
- Stable item identifiers and collision handling.
- Thumbnail and original separation.
- Hydration and eviction state machines.
- Read-only versus read-write permission behavior.
- Tests around album membership and repeated assets.

Limitations for this project:

- Their placeholder and thumbnail mechanisms depend on macOS File Provider APIs.
- The platform lifecycle cannot be ported directly to Linux or FUSE.

### Drive For Immich On Windows

Link: <https://github.com/RyanEwen/ImmichDrive>

What it does well:

- Uses Windows Cloud Files placeholders with correct zero-content local entries.
- Hydrates originals only when files are opened.
- Uses a shell thumbnail extension to fetch Immich thumbnails without hydration.
- Exposes date, album, favorite, and partner views.
- Integrates with ordinary Explorer and file-picker workflows.

What to leverage:

- Separation between the filesystem provider and thumbnail provider.
- Local index and remote change synchronization strategy.
- State transitions for placeholder, hydrated, pinned, and evicted content.
- Shell UX and error handling.

Limitations for this project:

- Windows Cloud Files and shell-extension APIs are not available on Linux.

### openVFS

Link: <https://github.com/opencloud-eu/openvfs>

What it does well:

- Implements files on demand for Linux using FUSE.
- Separates the FUSE layer from the credential-owning synchronization client.
- Models pin state using extended attributes.
- Blocks file opens while the desktop client hydrates content.
- Provides a concrete Linux architecture for cloud placeholders.

What to leverage:

- FUSE and sync-daemon separation.
- Hydration request protocol.
- Pin-state and extended-attribute conventions.
- Behavior when the remote client is unavailable.
- Packaging and mount lifecycle.

Limitations for this project:

- It is designed for OpenCloud and generic file synchronization, not Immich's asset and virtual-view model.
- Nautilus-specific server-thumbnail behavior still needs an Immich-aware solution.

### FreeDesktop Thumbnail Specification And GNOME GIO

Links:

- <https://specifications.freedesktop.org/thumbnail/latest/>
- <https://docs.gtk.org/gio/>

What they do well:

- Define a shared thumbnail cache layout and validation metadata.
- Key thumbnails by canonical source URI.
- Provide standard normal, large, extra-large, and xx-large thumbnail sizes.
- Define metadata that lets consumers validate cached thumbnails without reading originals.
- GIO exposes thumbnail and preview attributes used by GNOME applications.

What to leverage:

- Existing Nautilus-compatible cache formats.
- URI, modification-time, and size validation.
- Standard locations under the user's cache directory.
- GIO attributes if they are available to the chosen integration mechanism.

Risks to validate:

- Whether current Nautilus reliably accepts externally populated thumbnail cache entries for FUSE paths.
- Whether a Nautilus extension can provide thumbnails directly in current supported APIs.
- Whether GIO preview attributes can be exposed for a normal FUSE mount.
- How canonical URI and modification-time changes affect cache validity.

## Candidate Product Architecture

This section establishes a useful decomposition, not a final implementation mandate.

### Immich Client And Catalog

A long-running user daemon communicates with Immich using a scoped API key. It maintains a persistent local catalog, likely SQLite, containing stable asset IDs, view membership, filenames, media metadata, remote revision information, and local cache state.

The catalog allows fast, offline-tolerant directory listings without repeatedly traversing the full remote library. Synchronization should be incremental where Immich APIs permit it and fall back to bounded polling or paginated refreshes.

### FUSE Filesystem

The FUSE layer translates virtual paths into catalog objects. It handles metadata operations without network content reads and delegates content hydration to the cache manager.

The FUSE layer should remain thin. It should not own credentials or duplicate API and cache policy logic if a daemon separation similar to openVFS is practical.

### Thumbnail Integration

A Nautilus or GNOME-aware component obtains thumbnails from Immich's thumbnail endpoint. Candidate mechanisms include:

- Pre-populating the FreeDesktop thumbnail cache with correctly keyed and annotated PNG files.
- A Nautilus extension that requests thumbnails from the daemon.
- A dedicated thumbnailer with a project-specific MIME or URI strategy that does not intercept unrelated media.
- GIO preview attributes if they can be made available for FUSE entries.

The selected approach must be demonstrated against supported Nautilus versions. It must never call the original-content read path while producing a thumbnail.

### Content Cache Manager

The cache manager owns downloads, temporary files, range state, integrity checks, access timestamps, cache limits, and eviction. It should expose explicit hydrate, pin, unpin, and evict operations even if the first UI only uses hydrate and evict.

### Desktop And Service Integration

The project should provide:

- A CLI for login, mount, unmount, refresh, status, hydrate, pin, and evict operations as they become available.
- A systemd user service for automatic startup and recovery.
- Secret Service credential storage.
- Nautilus menus or emblems for availability state and "free up space" where current extension APIs permit it.
- Structured logs and cache statistics.

## Functional Requirements

### MVP Requirements

- Authenticate to Immich 3.x using a scoped API key.
- Mount an unprivileged FUSE filesystem at a configurable stable path.
- Enumerate at least one complete view of the user's library, including assets not assigned to albums.
- Provide deterministic, collision-safe filenames and stable asset identity.
- Return file size, media type, and timestamps without downloading originals.
- Display Immich-supplied thumbnails in Nautilus without downloading originals.
- Download an original only when file content is read.
- Return original bytes correctly to arbitrary applications.
- Coalesce concurrent hydration of the same asset.
- Persist complete originals in a bounded local cache.
- Evict originals by age and total cache size.
- Preserve catalog entries and thumbnails after original eviction.
- Support manual eviction of one asset, a directory selection, or the full original cache.
- Recover safely from interrupted downloads, crashes, and network failures.
- Persist catalog and cache state across restarts.
- Never perform a destructive Immich operation in the read-only MVP.
- Store credentials outside plaintext configuration files.
- Expose enough logging to prove whether thumbnail or original endpoints were used.

### Important Follow-Up Requirements

- Album view.
- Favorites view.
- Configurable timeline hierarchy such as `Timeline/YYYY/MM`.
- Video thumbnails and efficient playback behavior.
- RAW and HEIF thumbnail behavior using Immich-generated previews.
- Live Photo representation.
- Manual pinning for offline access.
- Nautilus emblems for remote, downloading, cached, pinned, and error states.
- Background catalog updates with low latency.
- Graceful offline browsing from cached metadata and thumbnails.
- Cache size, age, and free-space controls.
- Metrics for remote calls, bytes transferred, hit rate, and eviction.

### Possible Write Features

Write support is intentionally outside the read-only MVP. Candidate later behavior includes:

- Copying a new local file into an upload-oriented virtual directory uploads it to Immich.
- Creating an album folder creates an Immich album.
- Copying an existing asset into an album folder adds album membership without re-uploading bytes.
- Removing an asset from an album view removes only that membership.
- Deleting from a dedicated destructive view moves an asset to Immich trash after explicit enablement.
- Renaming an album folder renames the album.

Write behavior must be transactional where possible and must surface asynchronous upload failures. The system must not report a durable successful copy before remote upload is safely complete unless it also exposes a clear pending state and durable local queue.

## Non-Goals

- Reimplementing the Immich server or web gallery.
- Reading Immich's database directly.
- Mounting or exposing Immich's internal storage directories.
- Acting as the sole backup of an Immich installation.
- Guaranteeing that every POSIX filesystem operation has an Immich equivalent.
- Providing transparent arbitrary file editing in the first release.
- Treating an album hierarchy as if Immich supported nested albums.
- Silently propagating local deletes to the server.
- Downloading the entire library for offline use by default.
- Supporting every Linux file manager before the Nautilus integration is correct.

## Safety And Security Requirements

- Use least-privilege API keys.
- Keep API keys in Secret Service and redact them from logs.
- Validate TLS normally and do not disable certificate verification by default.
- Treat all remote filenames and metadata as untrusted input.
- Prevent path traversal and invalid path generation.
- Avoid following unsafe symlinks in cache and mount management.
- Use private per-user permissions for catalog, thumbnails, cache, sockets, and logs.
- Make destructive remote behavior opt-in and visibly distinct from local cache eviction.
- Distinguish "free up local space" from "delete from Immich" in both API and UI wording.
- Use atomic rename or equivalent commit behavior for completed cache downloads.
- Verify size and, where practical, content checksums before marking originals complete.

## Acceptance Scenarios

### Cold Thumbnail Browse

Given an empty local thumbnail and original cache, opening a Nautilus directory containing remote images should request asset metadata and thumbnail endpoints only. It must make zero original-download requests until the user or another application reads file content.

### Original Hydration

Opening a remote image in an ordinary image viewer should transparently download the original and return byte-identical content. A second open should use the cached original while it remains valid.

### Eviction

After the configured inactivity period, manual eviction, or cache pressure, the complete original should be removed. Its directory entry and Nautilus thumbnail should remain. Opening it again should transparently download it again.

### Metadata-Only Operations

Running directory listings, `stat`, file-property inspection, search indexing limited to metadata, and Nautilus thumbnail display must not hydrate originals.

### Concurrent Access

Two applications opening the same uncached asset at the same time should share one remote download and both receive correct data.

### Interrupted Download

If the network fails during hydration, no partial file should be marked as complete. A later read should resume safely if supported or restart cleanly.

### Restart

After restarting the daemon or laptop, cached metadata and thumbnails should remain useful. Complete cached originals should remain readable without redownload, and incomplete temporary downloads should be cleaned up or resumed safely.

### Server Unavailable

When Immich is unreachable, cached directory metadata and thumbnails should remain browsable. Uncached originals should fail with a clear, bounded error rather than hanging indefinitely.

### Duplicate Names

Two assets with the same original filename in the same virtual directory must both remain addressable through deterministic names. Their paths should remain stable across refreshes.

### No Destructive Confusion

Evicting a cached original must make no mutation request to Immich. In the read-only MVP, local unlink, rename, and write attempts must fail clearly rather than producing partial or unexpected remote behavior.

## Performance Expectations

Initial numeric targets should be established with a representative large library, but the implementation should be designed around these qualitative goals:

- Directory listing latency should depend primarily on the local catalog, not full remote traversal.
- Opening a directory should fetch thumbnails only for visible or near-visible items where Nautilus behavior allows it.
- Thumbnail transfer should be proportional to thumbnail size, not original size.
- A single asset should have at most one active original hydration per local client.
- Cache eviction should not block ordinary reads for unrelated assets.
- Catalog refresh should be paginated, cancelable, and bounded in memory.
- Large libraries should not require one local inode or open file descriptor per asset while unmounted.
- Original reads should stream with bounded memory use.

## Open Design Questions

- What should the default namespace be: timeline, albums, a flat all-assets directory, or several virtual top-level views?
- Should filenames expose asset IDs, use hidden identity metadata, or add IDs only for collisions?
- Can the current FreeDesktop thumbnail cache fully satisfy Nautilus without a private extension API?
- Can a Nautilus extension provide thumbnails directly in supported Nautilus versions?
- Can useful GIO preview attributes be exposed for normal FUSE entries?
- Should the daemon and FUSE process be separate for crash isolation and credential ownership?
- Which language and FUSE binding provide the best balance of correctness, async I/O, packaging, and maintainability?
- Should original downloads be full-file only initially, or should the first version support sparse ranged caching?
- How should videos behave when an application expects seeking before the full file is available?
- Which Immich representation should a path expose when an edited version exists?
- How should Live Photos and sidecars appear in a generic filesystem?
- What refresh mechanism gives acceptable latency without expensive repeated timeline scans?
- How should partner-shared assets and external-library assets be represented?
- Should an offline pin state be represented through extended attributes?
- What default age, maximum size, and minimum-free-space policies are safe?
- Should local writes be rejected everywhere in the first release or accepted in a separate explicit `Uploads` directory?
- How will filesystem indexing services be prevented from unintentionally hydrating the whole library?
- How will backup, antivirus, and content-indexing tools distinguish placeholders from cached originals?

## Initial Investigation Tasks

1. Build a minimal read-only FUSE spike that lists a small fixed set of Immich assets and reports metadata without downloading content.
2. Instrument every Immich metadata, thumbnail, preview, and original request.
3. Validate how Nautilus 50 discovers and validates thumbnail-cache entries for a FUSE path.
4. Generate a standards-compliant FreeDesktop thumbnail from the Immich thumbnail endpoint and prove that Nautilus displays it with zero original reads.
5. Investigate current Nautilus extension points for emblems, context menus, properties, and thumbnail provision.
6. Test GIO `thumbnail::*` and `preview::*` attributes against FUSE-mounted files.
7. Trace Nautilus and common viewer I/O patterns with a synthetic FUSE file, including open, stat, seek, read-ahead, and close.
8. Study rclone's VFS cache and openVFS's hydration protocol for reusable cache-state patterns.
9. Study Findich and Drive for Immich for namespace, thumbnail, collision, and state-machine behavior.
10. Define an MVP namespace and collision policy using a representative real Immich catalog.
11. Define API-version detection and the minimum API-key permissions for each feature set.
12. Produce a threat model before implementing writes or server-side mutations.

## Definition Of MVP Success

The MVP is successful when a user can mount their Immich library, browse it in Nautilus with real Immich thumbnails, open any supported asset through a normal filesystem path, and later reclaim the original's local disk usage without losing the visible file or thumbnail. Automated tests and request logs must prove that thumbnail browsing does not download originals. The implementation must work without privileged installation beyond normal FUSE and desktop integration requirements, use a scoped API key, survive restarts, and make no destructive server mutations.

## References

- Immich documentation: <https://docs.immich.app/>
- Immich API documentation: <https://api.immich.app/>
- Immich CLI: <https://docs.immich.app/features/command-line-interface>
- Immich external libraries: <https://docs.immich.app/features/libraries>
- Immich source: <https://github.com/immich-app/immich>
- Mimick: <https://github.com/nicx17/mimick>
- Immich FUSE: <https://github.com/AlessandroLorenzi/immich-fuse>
- Immich SFTP Server: <https://github.com/Demian98/immich-sftp-server>
- Findich: <https://github.com/Majorfi/immich-in-finder>
- Immich Desktop for macOS: <https://github.com/Kartax/immich-desktop-app>
- Drive for Immich: <https://github.com/RyanEwen/ImmichDrive>
- rclone mount: <https://rclone.org/commands/rclone_mount/>
- openVFS: <https://github.com/opencloud-eu/openvfs>
- FreeDesktop Thumbnail Managing Standard: <https://specifications.freedesktop.org/thumbnail/latest/>
- GIO documentation: <https://docs.gtk.org/gio/>
