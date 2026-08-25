# Add RAW, HEIF, and Live Photo Previews

Type: research
Status: open
Target: 1.4
Blocked by: 09

## Scope

Add formats only after representative files prove that Immich returns useful bounded Previews. Model Live Photos as related image and video assets without exposing hidden components as unrelated files.

## Acceptance

- Representative RAW and HEIF assets display useful Previews without Hydration.
- Live Photo components retain their relationship across Views and refreshes.
- Missing or invalid server Previews keep the existing failure-record isolation.
- Download and upload remain format-agnostic.
