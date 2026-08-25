# Find the Nautilus 50 thumbnail route

Type: research
Status: resolved
Blocked by:

## Question

Which supported Nautilus 50, GIO, and FreeDesktop mechanisms can display an Immich Preview for a FUSE path without opening the original, without intercepting unrelated local media, and with correct cache invalidation?

## Answer

Install a GNOME failure record for every entry before exposing it. Retain that record alongside any successful Preview. Keep only a current `large` success PNG for supported Previews. Remove stale or competing success records from every standard size directory. [Nautilus 50 thumbnail route](../../../docs/research/nautilus-50-thumbnail-route.md) records the exact cache and invalidation contract.
