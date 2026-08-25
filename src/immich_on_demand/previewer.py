from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import batched
import logging
from pathlib import Path

import trio

from .catalog import CatalogAsset
from .immich import ImmichClient
from .thumbnails import (
    install_thumbnail,
    prepare_thumbnail_cache,
)


PREVIEW_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "video/mp4", "video/quicktime", "video/x-m4v"}
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreviewStats:
    total: int
    installed: int
    failed: int
    unsupported: int


async def populate_previews(
    entries: Iterable[CatalogAsset],
    client: ImmichClient,
    mount_path: Path,
    *,
    cache_home: Path | None = None,
    concurrency: int = 4,
    task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
) -> PreviewStats:
    """Suppress desktop fallbacks, then populate supported previews concurrently."""
    if concurrency < 1:
        raise ValueError("preview concurrency must be positive")
    entries = tuple(entries)
    if any(entry.asset.size is None for entry in entries):
        raise ValueError("visible catalog entries must have a size")

    jobs: list[tuple[CatalogAsset, Path, int, int]] = []
    current_count = 0
    for entry in entries:
        source_path = mount_path / entry.name
        mtime = entry.asset.modified_ns // 1_000_000_000
        original_size = entry.asset.size
        assert original_size is not None
        supported = entry.asset.mime_type.lower() in PREVIEW_MIME_TYPES
        current_thumbnail = prepare_thumbnail_cache(
            source_path,
            mtime,
            original_size,
            cache_home=cache_home,
            retain_size="large" if supported else None,
        )
        if current_thumbnail:
            current_count += 1
            continue
        if supported:
            jobs.append((entry, source_path, mtime, original_size))

    installed = [False] * len(jobs)
    task_status.started()

    async def fetch(index: int, job: tuple[CatalogAsset, Path, int, int]) -> None:
        entry, source_path, mtime, original_size = job
        try:
            preview, _ = await client.thumbnail(entry.asset.id)
            install_thumbnail(
                preview,
                source_path,
                mtime,
                original_size,
                cache_home=cache_home,
                size="large",
            )
            prepare_thumbnail_cache(
                source_path,
                mtime,
                original_size,
                cache_home=cache_home,
            )
            installed[index] = True
        except Exception as error:
            LOGGER.warning("preview failed for asset %s: %s", entry.asset.id, error)

    for batch in batched(enumerate(jobs), concurrency):
        async with trio.open_nursery() as nursery:
            for index, job in batch:
                nursery.start_soon(fetch, index, job)

    successes = sum(installed)
    return PreviewStats(
        len(entries),
        current_count + successes,
        len(jobs) - successes,
        len(entries) - current_count - len(jobs),
    )
