from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
from pathlib import Path
import warnings

import gi
import trio

gi.require_version("Gio", "2.0")
with warnings.catch_warnings():
    warnings.simplefilter("ignore", gi.PyGIDeprecationWarning)
    from gi.repository import Gio, GLib

from .catalog import Catalog, CatalogAsset
from .immich import ImmichClient
from .thumbnails import (
    install_thumbnail,
    prepare_thumbnail_cache,
)


VIDEO_PREVIEW_MIME_TYPES = frozenset(
    {"video/mp4", "video/quicktime", "video/x-m4v"}
)
LOGGER = logging.getLogger(__name__)
_SORT_BY = "metadata::nautilus-icon-view-sort-by"
_SORT_REVERSED = "metadata::nautilus-icon-view-sort-reversed"
SORT_POLL_SECONDS = 1.0


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


def _order_for_nautilus(
    entries: tuple[CatalogAsset, ...], metadata: tuple[str, bool] | None
) -> tuple[tuple[CatalogAsset, ...], bool]:
    if metadata is None:
        return entries, False
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
        return entries, False
    return tuple(sorted(entries, key=key, reverse=descending)), True


@dataclass(frozen=True, slots=True)
class PreviewStats:
    total: int
    installed: int
    failed: int
    unsupported: int


async def populate_previews(
    catalog: Catalog,
    client: ImmichClient,
    mount_path: Path,
    *,
    cache_home: Path | None = None,
    concurrency: int = 4,
    downloads_enabled: bool = True,
    mount_ready: trio.Event | None = None,
    task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
) -> PreviewStats:
    """Suppress desktop fallbacks, then optionally populate supported previews."""
    if concurrency < 1:
        raise ValueError("preview concurrency must be positive")
    entries = tuple(catalog.list_visible())
    if any(entry.asset.size is None for entry in entries):
        raise ValueError("visible catalog entries must have a size")
    jobs: list[tuple[CatalogAsset, tuple[Path, ...], int, int]] = []
    current_count = 0
    for entry in entries:
        source_paths = tuple(mount_path / alias for alias in catalog.aliases(entry.asset.id))
        if not source_paths:
            raise ValueError("visible catalog entry has no namespace alias")
        mtime = entry.asset.modified_ns // 1_000_000_000
        original_size = entry.asset.size
        assert original_size is not None
        mime_type = entry.asset.mime_type.lower()
        supported = mime_type.startswith("image/") or mime_type in VIDEO_PREVIEW_MIME_TYPES
        current_thumbnails = tuple(
            prepare_thumbnail_cache(
                source_path,
                mtime,
                original_size,
                cache_home=cache_home,
                retain_size="large" if supported else None,
            )
            for source_path in source_paths
        )
        if supported and all(current_thumbnails):
            current_count += 1
            continue
        if supported:
            jobs.append((entry, source_paths, mtime, original_size))

    task_status.started()
    job_count = len(jobs)
    if not downloads_enabled:
        return PreviewStats(
            len(entries),
            current_count,
            job_count,
            len(entries) - current_count - job_count,
        )
    if mount_ready is not None:
        await mount_ready.wait()
    pending = deque(jobs)
    installed: set[str] = set()

    async def fetch(job: tuple[CatalogAsset, tuple[Path, ...], int, int]) -> None:
        entry, source_paths, mtime, original_size = job
        try:
            preview, _ = await client.thumbnail(entry.asset.id)
            for source_path in source_paths:
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
            installed.add(entry.asset.id)
        except Exception as error:
            LOGGER.warning("preview failed for asset %s: %s", entry.asset.id, error)

    unread = object()
    active_sort: object | tuple[str, bool] | None = unread
    next_sort_check = 0.0
    while pending:
        if trio.current_time() >= next_sort_check:
            metadata = await trio.to_thread.run_sync(
                _read_nautilus_sort, mount_path / "All"
            )
            if metadata != active_sort:
                ordered_entries, supported = _order_for_nautilus(
                    tuple(job[0] for job in pending), metadata
                )
                rank = {
                    entry.asset.id: index for index, entry in enumerate(ordered_entries)
                }
                pending = deque(sorted(pending, key=lambda job: rank[job[0].asset.id]))
                active_sort = metadata
                if supported:
                    assert metadata is not None
                    attribute, descending = metadata
                    LOGGER.info(
                        "preview queue follows Nautilus %s sort (%s)",
                        attribute,
                        "descending" if descending else "ascending",
                    )
            next_sort_check = trio.current_time() + SORT_POLL_SECONDS

        batch = [pending.popleft() for _ in range(min(concurrency, len(pending)))]
        async with trio.open_nursery() as nursery:
            for job in batch:
                nursery.start_soon(fetch, job)

    successes = len(installed)
    return PreviewStats(
        len(entries),
        current_count + successes,
        job_count - successes,
        len(entries) - current_count - job_count,
    )
