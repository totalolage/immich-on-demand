# Add RAW, HEIF, and Live Photo Previews

Type: research
Status: resolved
Target: 1.4
Blocked by: 09

## Scope

Use Immich's bounded generated Preview path for image formats without Hydration, but claim RAW or HEIF support only after representative files prove those Previews useful. Model Live Photos as related image and video assets without exposing hidden components as unrelated files.

## Acceptance

- Representative RAW and HEIF assets display useful Previews without Hydration.
- Live Photo components retain their relationship across Views and refreshes.
- Missing or invalid server Previews keep the existing failure-record isolation.
- Download and upload remain format-agnostic.

## Answer

Use Immich's generated `size=preview` derivative for every asset whose original MIME type starts with `image/`, while retaining the existing explicit video MIME types. The Preview stream remains capped at 32 MiB and decoded under the existing per-asset failure isolation. Representative `.heic`, `.heif`, `.dng`, and one camera-native RAW must pass Reference-system acceptance before claiming support.

Persist nullable `livePhotoVideoId` through API validation, catalog storage, full and incremental refresh, and every View projection. Treat it as a relation from the visible still to a separate video asset, and suppress the component from every View regardless of its server visibility. Do not expose that video as a neighboring file or model it as an XMP sidecar. This slice previews and opens the still only; composite playback, paired export, and paired upload remain separate work.

The pinned Immich 3.0.3 contracts and acceptance boundary are recorded in `docs/research/immich-3-raw-heif-live-photo-previews.md`. Use the sanitized, read-only Reference capability probe in `docs/research/preview-format-capability-probe.md` before visual acceptance.
