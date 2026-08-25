# Populate the FreeDesktop thumbnail cache

Write Immich Previews into the URI-keyed FreeDesktop thumbnail cache before Nautilus can request the mounted originals, and create GNOME failure records for entries without a supported Preview. Nautilus 50 has no public thumbnail-provider extension, while a global MIME thumbnailer would affect unrelated files and could Hydrate originals on cache misses.
