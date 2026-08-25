from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import logging
from pathlib import Path

import trio

from .catalog import CatalogAsset
from .immich import ImmichClient
from .thumbnails import failed_thumbnail_path, install_failed_thumbnail, install_thumbnail


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
    size: str = "large",
) -> PreviewStats:
    """Suppress desktop fallbacks, then populate supported previews concurrently."""
    if concurrency < 1:
        raise ValueError("preview concurrency must be positive")
    entries = tuple(entries)
    if any(entry.asset.size is None for entry in entries):
        raise ValueError("visible catalog entries must have a size")

    jobs: list[tuple[CatalogAsset, Path, int, int]] = []
    for entry in entries:
        source_path = mount_path / entry.name
        mtime = entry.asset.modified_ns // 1_000_000_000
        original_size = entry.asset.size
        assert original_size is not None
        install_failed_thumbnail(source_path, mtime, original_size, cache_home=cache_home)
        if entry.asset.mime_type.lower() in PREVIEW_MIME_TYPES:
            jobs.append((entry, source_path, mtime, original_size))

    installed = [False] * len(jobs)
    limiter = trio.CapacityLimiter(concurrency)

    async def fetch(index: int, job: tuple[CatalogAsset, Path, int, int]) -> None:
        entry, source_path, mtime, original_size = job
        try:
            async with limiter:
                preview, _ = await client.thumbnail(entry.asset.id)
                install_thumbnail(
                    preview,
                    source_path,
                    mtime,
                    original_size,
                    cache_home=cache_home,
                    size=size,
                )
            try:
                failed_thumbnail_path(source_path, cache_home).unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("could not remove stale failure thumbnail for %s", entry.asset.id)
            installed[index] = True
        except Exception as error:
            LOGGER.warning("preview failed for asset %s: %s", entry.asset.id, error)

    async with trio.open_nursery() as nursery:
        for index, job in enumerate(jobs):
            nursery.start_soon(fetch, index, job)

    successes = sum(installed)
    return PreviewStats(len(entries), successes, len(jobs) - successes, len(entries) - len(jobs))
