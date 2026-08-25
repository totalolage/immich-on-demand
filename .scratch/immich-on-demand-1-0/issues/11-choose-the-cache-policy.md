# Choose the cache policy

Type: grilling
Status: resolved
Blocked by: 10

## Question

Which complete-file or ranged-cache model, access definition, age limit, size limit, free-space floor, concurrent-read rule, interrupted-download rule, and manual Eviction behavior should 1.0 adopt?

## Answer

Cache only complete originals as private UUID-named files. Explicitly update file access time on every Hydration or cached read, then evict least-recently used complete files for age, total-size, or free-space pressure. Defaults are 30 days, 10 GiB, and a 5 GiB free-space floor. Open and in-flight assets are never eligible. Per-asset manual Eviction refuses them, while whole-cache manual Eviction removes every eligible original. Downloads use private same-directory temporary files, `fsync`, exact-size validation, managed-asset Base64 SHA-1 validation, and atomic rename. Incomplete files are never entries and safe stale temporaries are cleaned at startup.
