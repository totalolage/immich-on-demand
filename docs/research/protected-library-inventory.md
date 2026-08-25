# Protected library inventory

`scripts/inventory.py` queried the configured Immich 3.0.3 server on 2026-08-25 with the verified read-only key. It recorded aggregates only; no filename, asset ID, timestamp, location, or media content left the process.

| Measure | Count |
| --- | ---: |
| Owned assets returned | 13,551 |
| Visible 1.0 entries | 11,237 |
| Hidden assets | 2,314 |
| Trashed or offline assets | 0 |
| Assets missing byte size | 0 |
| Visible duplicate-name groups | 1,892 |
| Visible assets in those groups | 3,784 |
| Filenames changed by the safety sanitizer | 0 |

Visible media comprises 10,350 JPEG images, 248 PNG images, and 639 MP4 videos. Across all records, 1,402 assets are under 1 MiB, 12,060 are 1 to 10 MiB, 72 are 10 to 100 MiB, and 17 are at least 100 MiB.

The duplicate rate rules out using original filenames as unique paths. The catalog must persist a collision-safe Library name while retaining the asset ID as identity.
