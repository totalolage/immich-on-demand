# Define the mutation contract

Type: grilling
Status: resolved
Blocked by: 01

## Question

What must create, close, failed upload, rename, overwrite, unlink, trash, restore, and cache Eviction mean in the Flat library, given that existing assets are immutable and Remote deletion is disabled until explicitly enabled?

## Answer

Existing assets are read-only: open-for-write, overwrite, truncate, rename, link, and metadata changes return `EROFS`. Creating a safe new name stages private bytes. Flush only syncs staging. The last release makes exactly one upload attempt. A successful upload removes staging. A failed or duplicate upload is logged and retained under `uploads/`. FUSE cannot return release errors, so the closing application cannot observe that remote failure. Unlink remains disabled unless configuration opts in, the exact mutation key includes `asset.delete`, the asset belongs to the configured user, and Immich trash is enabled. A successful unlink sends `force: false` before the catalog hides the asset. Restore is an explicit client operation, not a filesystem side effect, and Eviction never changes Immich.
