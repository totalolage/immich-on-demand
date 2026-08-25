# Define Flat library names

Type: grilling
Status: resolved
Blocked by: 05

## Question

How should the Flat library derive stable, deterministic, collision-free filenames from untrusted original names and Immich asset IDs without renaming an existing entry when another asset arrives?

## Answer

Sanitize every untrusted basename and preserve its extension. The first asset in creation-time and asset-ID order keeps the basename; later collisions add `__<full-asset-id>` before the extension. Persist the assigned Library name so future arrivals never rename an existing entry.
