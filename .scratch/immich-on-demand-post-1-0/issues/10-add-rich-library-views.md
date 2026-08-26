# Add All, Album, People, Date, and Favorite Views

Type: feature
Status: implemented
Target: 1.3
Blocked by: target read-only acceptance

## Scope

Implement the namespace decision with `All`, `by Date`, Albums, People, and Favorites. Preserve one asset identity across every View.

## Implementation

The root exposes `All`, `Albums`, `People`, `by Date`, and `Favorites`. Date aliases use `by Date/YYYY/MM/DD`. Each asset keeps one inode across every alias, and its link count equals its visible aliases. Only `All` accepts create or unlink.

The service fetches complete Album and People inventories as a pair and publishes neither until both validate. Incremental asset refresh never infers a relation removal. Preview preparation covers every alias and fetches at most one server Preview per asset.

## Target acceptance

- Each View matches a server-side inventory on the Reference system.
- Assets with several albums or people appear in each relevant directory as hardlinks.
- Favorite reflects Immich metadata and never implies Pin.
- Listing every View performs no Hydration.
