# Add All, Album, People, Date, and Favorite Views

Type: feature
Status: open
Target: 1.3
Blocked by: 09

## Scope

Implement the namespace decision in small read-only slices. Start with `All` and `by Date`, then add Albums, People, and Favorites after verifying their stable Immich API contracts. Preserve one asset identity across every View.

## Acceptance

- Each View matches a server-side inventory on the Reference system.
- Assets with several albums or people appear in each relevant directory as hardlinks.
- Favorite reflects Immich metadata and never implies Pin.
- Listing every View performs no Hydration.
