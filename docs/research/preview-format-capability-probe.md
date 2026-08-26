# Reference Preview capability probe

Paste this probe into a terminal on the configured Reference system after you install the current development package. The probe uses the installed Python environment, the stored read-only key, `ImmichClient.asset_pages`, and `ImmichClient.thumbnail`.

The probe calls no original-download, playback, upload, trash, restore, or other mutation route. It fetches at most two RAW Previews, two HEIF or HEIC Previews, and one Live Photo still Preview. It prints one JSON line with aggregate counts. It prints no UUID, filename, URL, path, exception, API key, or media bytes.

The `APP` override is optional. Without it, the wrapper resolves `immich-on-demand` from `PATH`. The Python child returns status 1 on failure. The block contains no shell `exit`, so it does not terminate the calling terminal.

```bash
app="$(readlink -f "${APP:-$(command -v immich-on-demand)}")"
py="${app%/*}/python"
"$py" - <<'PY'
from __future__ import annotations

from collections import Counter
from io import BytesIO
import json
from pathlib import PurePath
import warnings

from PIL import Image, ImageOps
import trio

from immich_on_demand.immich import ImmichClient, READ_PERMISSIONS
from immich_on_demand.settings import load, load_api_key
from immich_on_demand.thumbnails import THUMBNAIL_SIZES

RAW_EXTENSIONS = frozenset({".arw", ".cr3", ".dng", ".nef", ".raf"})
RAW_MIMES = frozenset({
    "image/arw", "image/cr3", "image/dng", "image/nef", "image/raf",
})
HEIF_EXTENSIONS = frozenset({".heic", ".heif"})
HEIF_MIMES = frozenset({"image/heic", "image/heif"})


def counts(values: Counter[str]) -> dict[str, int]:
    return dict(sorted(values.items()))


async def check_previews(client: ImmichClient, asset_ids: list[str], limit: int):
    selected = asset_ids[:limit]
    result = {"selected": len(selected), "response_ok": 0, "decoded": 0}
    for asset_id in selected:
        try:
            preview, _ = await client.thumbnail(asset_id)
            result["response_ok"] += 1
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(preview)) as opened:
                    opened.load()
                    image = ImageOps.exif_transpose(opened).convert("RGBA")
                    image.thumbnail(
                        (THUMBNAIL_SIZES["large"],) * 2,
                        Image.Resampling.LANCZOS,
                    )
            result["decoded"] += 1
        except Exception:
            continue
    return result


async def main() -> None:
    stage = "settings"
    try:
        settings = load()
        key = load_api_key(settings, "read-only")
        stage = "key_validation"
        async with ImmichClient(settings.server_url, key) as client:
            session = await client.validate(READ_PERMISSIONS, exact_permissions=True)
            stage = "inventory"
            assets = []
            async for page in client.asset_pages(
                session.owner_id,
                page_limit=10_000,
            ):
                assets.extend(page)
            by_id = {asset.id: asset for asset in assets}

            raw_extensions: Counter[str] = Counter()
            raw_mimes: Counter[str] = Counter()
            heif_extensions: Counter[str] = Counter()
            heif_mimes: Counter[str] = Counter()
            raw_candidates: list[str] = []
            heif_candidates: list[str] = []
            for asset in assets:
                extension = PurePath(asset.original_name).suffix.lower()
                mime = asset.mime_type.lower()
                is_raw = extension in RAW_EXTENSIONS or mime in RAW_MIMES
                is_heif = extension in HEIF_EXTENSIONS or mime in HEIF_MIMES
                if extension in RAW_EXTENSIONS:
                    raw_extensions[extension] += 1
                if mime in RAW_MIMES:
                    raw_mimes[mime] += 1
                if extension in HEIF_EXTENSIONS:
                    heif_extensions[extension] += 1
                if mime in HEIF_MIMES:
                    heif_mimes[mime] += 1
                if asset.visible and is_raw:
                    raw_candidates.append(asset.id)
                if asset.visible and is_heif:
                    heif_candidates.append(asset.id)

            declared = [asset for asset in assets if asset.live_photo_video_id]
            resolved = [
                (still, by_id.get(still.live_photo_video_id))
                for still in declared
            ]
            videos = [
                (still, motion)
                for still, motion in resolved
                if motion is not None and motion.mime_type.lower().startswith("video/")
            ]
            motion_ids = {motion.id for _, motion in videos}
            hidden_motion_ids = {
                motion.id for _, motion in videos if motion.visibility == "hidden"
            }
            still_candidates = [still.id for still, _ in videos if still.visible]

            stage = "preview"
            preview_checks = {
                "raw": await check_previews(client, raw_candidates, 2),
                "heif_heic": await check_previews(client, heif_candidates, 2),
                "live_photo_still": await check_previews(
                    client,
                    still_candidates,
                    1,
                ),
            }

        report = {
            "probe_version": 2,
            "probe_status": "ok",
            "server_contract": "3.0.3",
            "exact_read_key": True,
            "raw_extension_counts": counts(raw_extensions),
            "raw_mime_counts": counts(raw_mimes),
            "heif_heic_extension_counts": counts(heif_extensions),
            "heif_heic_mime_counts": counts(heif_mimes),
            "live_photo_relationship_counts": {
                "declared_links": len(declared),
                "resolved_owned_links": sum(motion is not None for _, motion in resolved),
                "resolved_video_links": len(videos),
                "distinct_motion_assets": len(motion_ids),
                "hidden_motion_assets": len(hidden_motion_ids),
                "non_hidden_motion_assets": len(motion_ids - hidden_motion_ids),
                "broken_or_non_video_links": len(declared) - len(videos),
            },
            "preview_checks": preview_checks,
        }
    except Exception:
        report = {
            "probe_version": 2,
            "probe_status": "failed",
            "failure_stage": stage,
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    if report["probe_status"] != "ok":
        raise SystemExit(1)


trio.run(main)
PY
```

## Output contract

Success is one compact JSON object with these fields:

- `probe_version` is `2`.
- `probe_status` is `"ok"`.
- `server_contract` is `"3.0.3"` after production-client version validation.
- `exact_read_key` is `true` after exact `READ_PERMISSIONS` validation.
- The four format-count objects contain only the listed extension or MIME keys and integer counts.
- `live_photo_relationship_counts` contains integer `declared_links`, `resolved_owned_links`, `resolved_video_links`, `distinct_motion_assets`, `hidden_motion_assets`, `non_hidden_motion_assets`, and `broken_or_non_video_links`.
- `preview_checks` contains `raw`, `heif_heic`, and `live_photo_still`. Each value contains integer `selected`, `response_ok`, and `decoded` counts.

Failure is one compact JSON object, and the Python child returns status 1:

```json
{"failure_stage":"settings|key_validation|inventory|preview","probe_status":"failed","probe_version":2}
```

The `failure_stage` value is one member of the displayed set. The probe suppresses free-form exceptions, so you can paste its output into a development conversation.

## Interpretation

A zero `selected` value means that the inventory has no active representative asset in that class. For a selected class, equal `selected`, `response_ok`, and `decoded` counts prove that every selected generated derivative passed the same load, EXIF transpose, RGBA conversion, and large-thumbnail resize used by the production path. `ImmichClient.thumbnail` caps each response at 32 MiB. Visual inspection in Nautilus must still establish that each Preview is useful.

`broken_or_non_video_links` must be zero. `non_hidden_motion_assets` records components whose server visibility is not `hidden`; the product must still suppress every referenced motion component from its namespace. The probe never fetches a motion Preview. Catalog acceptance must verify relationship persistence and component suppression after full and incremental refreshes.
