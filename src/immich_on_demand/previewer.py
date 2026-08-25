from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import batched
import logging
from pathlib import Path
import warnings

import gi
import trio

gi.require_version("Gio", "2.0")
with warnings.catch_warnings():
    warnings.simplefilter("ignore", gi.PyGIDeprecationWarning)
    from gi.repository import Gio, GLib

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
_SORT_BY = "metadata::nautilus-icon-view-sort-by"
_SORT_REVERSED = "metadata::nautilus-icon-view-sort-reversed"


def _read_nautilus_sort(mount_path: Path) -> tuple[str, bool] | None:
    try:
        info = Gio.File.new_for_path(str(mount_path)).query_info(
            f"{_SORT_BY},{_SORT_REVERSED}",
            Gio.FileQueryInfoFlags.NONE,
            None,
        )
    except GLib.Error:
        return None
    attribute = info.get_attribute_string(_SORT_BY)
    reversed_value = info.get_attribute_string(_SORT_REVERSED)
    return (attribute, reversed_value == "true") if attribute else None


async def _sort_for_nautilus(
    entries: tuple[CatalogAsset, ...], mount_path: Path
) -> tuple[CatalogAsset, ...]:
    metadata = await trio.to_thread.run_sync(_read_nautilus_sort, mount_path)
    if metadata is None:
        return entries
    attribute, descending = metadata
    filename_key = lambda entry: GLib.utf8_collate_key_for_filename(entry.name, -1)
    modified_key = lambda entry: (entry.asset.modified_ns, filename_key(entry))
    created_key = lambda entry: (entry.asset.created_ns, filename_key(entry))
    key = {
        "name": filename_key,
        "size": lambda entry: (entry.asset.size, filename_key(entry)),
        "type": lambda entry: (
            GLib.utf8_collate_key(
                Gio.content_type_get_description(entry.asset.mime_type), -1
            ),
            GLib.utf8_collate_key(entry.asset.mime_type, -1),
            filename_key(entry),
        ),
        "date_modified": modified_key,
        "modification date": modified_key,
        "date_created": created_key,
        "creation date": created_key,
    }.get(attribute)
    if key is None:
        return entries
    LOGGER.info(
        "preview queue follows Nautilus %s sort (%s)",
        attribute,
        "descending" if descending else "ascending",
    )
    return tuple(sorted(entries, key=key, reverse=descending))


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
    mount_ready: trio.Event | None = None,
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

    task_status.started()
    if mount_ready is not None:
        await mount_ready.wait()
    if jobs:
        ordered_entries = await _sort_for_nautilus(
            tuple(job[0] for job in jobs), mount_path
        )
        rank = {entry.asset.id: index for index, entry in enumerate(ordered_entries)}
        jobs.sort(key=lambda job: rank[job[0].asset.id])

    installed = [False] * len(jobs)

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
