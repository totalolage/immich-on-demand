# Set the component boundaries

Type: grilling
Status: resolved
Blocked by: 06, 08, 09, 11

## Question

Which responsibilities belong to the FUSE mount, long-running service, local control API, CLI, thumbnail integration, catalog, and cache so credentials and remote mutations have one owner and a future GUI needs no core rewrite?

## Answer

Keep one Python process with narrow modules. The Immich client owns REST contracts. The SQLite catalog owns stable names, inodes, and visibility. The content cache owns Hydration, integrity, access, and Eviction. Thumbnail modules own FreeDesktop entries directly. `Library` enforces immutable reads, close-time upload ordering, and guarded trash. The pyfuse3 adapter translates those operations into the Flat library without holding credentials. The foreground service alone loads and validates Secret Service keys, performs the required online refresh, prepares Preview suppression, owns clients, mounts FUSE, applies cache policy, and serves the private Unix control socket. The CLI writes validated setup settings or calls that socket for live `status`, `refresh`, and `evict` operations. A future GUI can use the same settings functions and control protocol without moving core policy.
