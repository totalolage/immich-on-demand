#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import tempfile

import trio

from immich_on_demand.catalog import Catalog
from immich_on_demand.immich import ImmichClient
from immich_on_demand.model import safe_filename
from immich_on_demand.settings import Settings, load_api_key


async def inventory(server: str) -> dict[str, object]:
    settings = Settings(server, Path("/tmp/immich-on-demand-inventory"))
    names: Counter[str] = Counter()
    mime_types: Counter[str] = Counter()
    visible_mime_types: Counter[str] = Counter()
    size_buckets: Counter[str] = Counter()
    unsafe_names = 0
    with tempfile.TemporaryDirectory(prefix="immich-on-demand-inventory-") as directory:
        with Catalog(Path(directory) / "catalog.db") as catalog:
            async with ImmichClient(server, load_api_key(settings)) as client:
                session = await client.validate()
                catalog.begin_refresh()
                async for page in client.asset_pages(session.owner_id):
                    catalog.stage(page)
                    for asset in page:
                        mime_types[asset.mime_type] += 1
                        unsafe_names += safe_filename(asset.original_name, asset.id) != asset.original_name
                        if asset.visible:
                            names[asset.original_name] += 1
                            visible_mime_types[asset.mime_type] += 1
                        if asset.size is None:
                            size_buckets["unknown"] += 1
                        elif asset.size < 1024**2:
                            size_buckets["under_1_mib"] += 1
                        elif asset.size < 10 * 1024**2:
                            size_buckets["1_to_10_mib"] += 1
                        elif asset.size < 100 * 1024**2:
                            size_buckets["10_to_100_mib"] += 1
                        else:
                            size_buckets["100_mib_or_more"] += 1
                stats = catalog.finish_refresh()

    # ponytail: one name per asset in memory; aggregate in SQLite if a million-asset library makes this costly.
    collision_groups = [count for count in names.values() if count > 1]
    return {
        "server_version": session.version,
        "total_assets": stats.total,
        "visible_assets": stats.visible,
        "missing_size": stats.missing_size,
        "trashed": stats.trashed,
        "hidden": stats.hidden,
        "offline": stats.offline,
        "filename_collision_groups": len(collision_groups),
        "assets_in_collision_groups": sum(collision_groups),
        "unsafe_filenames": unsafe_names,
        "all_mime_types": dict(sorted(mime_types.items())),
        "visible_mime_types": dict(sorted(visible_mime_types.items())),
        "size_buckets": dict(size_buckets),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    arguments = parser.parse_args()
    print(json.dumps(trio.run(inventory, arguments.server), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
