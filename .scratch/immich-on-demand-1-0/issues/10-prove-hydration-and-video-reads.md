# Prove Hydration and video reads

Type: prototype
Status: resolved
Blocked by: 01, 04, 06

## Question

What read, seek, concurrency, interruption, and completion behavior do Nautilus and common image and video applications require from a FUSE file, and can 1.0 use a complete-file cache without sparse range storage?

## Answer

Use a complete-file cache. FUSE open acquires the asset against Eviction, and the first read waits for one shared Hydration per asset. After the full original passes size and available checksum validation, FUSE serves offset reads and seeks from the atomically published local file with kernel caching enabled. Concurrent readers share the download. Repeated reads stay local, and interruption never publishes partial content. If Immich fails after the mount starts, catalog operations and cached reads continue while uncached reads fail. Immich does not guarantee Range requests for originals, so 1.0 has no sparse-range layer. Live image-viewer and video-player checks remain part of Reference-system release acceptance.
