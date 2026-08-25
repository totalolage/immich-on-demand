# Find the Nautilus 50 thumbnail route

Type: research
Status: resolved
Blocked by:

## Question

Which supported Nautilus 50, GIO, and FreeDesktop mechanisms can display an Immich Preview for a FUSE path without opening the original, without intercepting unrelated local media, and with correct cache invalidation?

## Answer

Populate the global FreeDesktop thumbnail cache with URI-keyed PNGs before exposing entries, and install GNOME failure records for unsupported or unavailable Previews. [Nautilus 50 thumbnail route](../../../docs/research/nautilus-50-thumbnail-route.md) records the exact cache and invalidation contract.
