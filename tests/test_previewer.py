from functools import partial
from inspect import signature
from io import BytesIO
from pathlib import Path, PurePosixPath
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import trio

from immich_on_demand.catalog import CatalogAsset
from immich_on_demand.model import Asset
from immich_on_demand.previewer import (
    PreviewStats,
    _read_nautilus_sort,
    populate_previews,
)
from immich_on_demand.thumbnails import (
    THUMBNAIL_SIZES,
    failed_thumbnail_path,
    install_failed_thumbnail,
    install_thumbnail,
    thumbnail_cache_path,
)


def entry(
    index: int,
    mime_type: str = "image/jpeg",
    modified_ns: int = 4_999_999_999,
) -> CatalogAsset:
    asset_id = f"{index:08d}-1234-4234-8234-123456789abc"
    return CatalogAsset(
        Asset(
            asset_id,
            "87654321-4321-4321-8321-cba987654321",
            f"asset-{index}.jpg",
            mime_type,
            123 + index,
            1,
            modified_ns,
            "2026-08-25T12:00:00Z",
            "abc=",
            "timeline",
            False,
            False,
            None,
        ),
        index + 2,
        f"asset-{index}.jpg",
    )


def preview_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (400, 200), "blue").save(output, "JPEG")
    return output.getvalue()


class PreviewCatalog:
    def __init__(
        self,
        entries: list[CatalogAsset],
        aliases: dict[str, tuple[PurePosixPath, ...]] | None = None,
    ) -> None:
        self._entries = entries
        self._aliases = aliases or {
            item.asset.id: (PurePosixPath(item.name),) for item in entries
        }

    def list_visible(self) -> list[CatalogAsset]:
        return self._entries

    def aliases(self, asset_id: str) -> tuple[PurePosixPath, ...]:
        return self._aliases[asset_id]


class PreviewerTest(unittest.TestCase):
    def test_reads_nautilus_sort_metadata_as_strings(self) -> None:
        info = Mock()
        info.get_attribute_string.side_effect = ["date_modified", "true"]
        location = Mock()
        location.query_info.return_value = info

        with patch(
            "immich_on_demand.previewer.Gio.File.new_for_path",
            return_value=location,
        ):
            self.assertEqual(
                _read_nautilus_sort(Path("/mount")),
                ("date_modified", True),
            )

    def test_preview_size_is_fixed_by_the_previewer(self) -> None:
        self.assertNotIn("size", signature(populate_previews).parameters)

    def test_fetches_once_and_installs_for_every_catalog_alias(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = entry(1)
                aliases = (
                    PurePosixPath("All") / item.name,
                    PurePosixPath("Albums", "Summer") / item.name,
                    PurePosixPath("Favorites") / item.name,
                )
                catalog = PreviewCatalog([item], {item.asset.id: aliases})
                fetched: list[str] = []

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        fetched.append(asset_id)
                        return preview_bytes(), "image/jpeg"

                with patch(
                    "immich_on_demand.previewer._read_nautilus_sort",
                    return_value=None,
                ) as read_sort:
                    stats = await populate_previews(
                        catalog,  # type: ignore[arg-type]
                        Client(),  # type: ignore[arg-type]
                        root / "mount",
                        cache_home=root / "cache",
                        concurrency=1,
                    )

                self.assertEqual(fetched, [item.asset.id])
                self.assertEqual(stats, PreviewStats(1, 1, 0, 0))
                read_sort.assert_called_with(root / "mount" / "All")
                for alias in aliases:
                    source = root / "mount" / alias
                    self.assertTrue(thumbnail_cache_path(source, root / "cache").exists())
                    self.assertTrue(failed_thumbnail_path(source, root / "cache").exists())

        trio.run(scenario)

    def test_fetches_canonical_raw_and_heif_previews_and_isolates_invalid_bytes(
        self,
    ) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = [
                    entry(1, "image/heic"),
                    entry(2, "image/dng"),
                    entry(3, "image/heif"),
                ]
                invalid = entries[-1]
                fetched: list[str] = []

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        fetched.append(asset_id)
                        if asset_id == invalid.asset.id:
                            return b"not an image", "application/octet-stream"
                        return preview_bytes(), "image/jpeg"

                stats = await populate_previews(
                    PreviewCatalog(entries),  # type: ignore[arg-type]
                    Client(),  # type: ignore[arg-type]
                    root / "mount",
                    cache_home=root / "cache",
                    concurrency=1,
                )

                self.assertEqual(fetched, [item.asset.id for item in entries])
                self.assertEqual(stats, PreviewStats(3, 2, 1, 0))
                for item in entries[:-1]:
                    source = root / "mount" / item.name
                    self.assertTrue(
                        thumbnail_cache_path(source, root / "cache").exists()
                    )
                invalid_source = root / "mount" / invalid.name
                self.assertFalse(
                    thumbnail_cache_path(invalid_source, root / "cache").exists()
                )
                self.assertTrue(
                    failed_thumbnail_path(invalid_source, root / "cache").exists()
                )

        trio.run(scenario)

    def test_offline_reconciles_every_alias_without_sort_or_network(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = entry(1)
                aliases = (
                    PurePosixPath("All") / item.name,
                    PurePosixPath("Favorites") / item.name,
                )
                catalog = PreviewCatalog([item], {item.asset.id: aliases})
                stale_source = root / "mount" / aliases[1]
                stale = install_thumbnail(
                    preview_bytes(),
                    stale_source,
                    3,
                    124,
                    cache_home=root / "cache",
                    size="xx-large",
                )

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        raise AssertionError("offline preview fetched")

                with patch(
                    "immich_on_demand.previewer._read_nautilus_sort",
                    side_effect=AssertionError("offline preview read Nautilus sort"),
                ):
                    stats = await populate_previews(
                        catalog,  # type: ignore[arg-type]
                        Client(),  # type: ignore[arg-type]
                        root / "mount",
                        cache_home=root / "cache",
                        downloads_enabled=False,
                    )

                self.assertEqual(stats, PreviewStats(1, 0, 1, 0))
                self.assertFalse(stale.exists())
                for alias in aliases:
                    self.assertTrue(
                        failed_thumbnail_path(
                            root / "mount" / alias, root / "cache"
                        ).exists()
                    )

        trio.run(scenario)

    def test_signals_ready_after_suppression_and_before_fetch_completion(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = entry(1)

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        await trio.sleep_forever()

                async with trio.open_nursery() as nursery:
                    await nursery.start(
                        partial(
                            populate_previews,
                            PreviewCatalog([item]),  # type: ignore[arg-type]
                            Client(),  # type: ignore[arg-type]
                            root / "mount",
                            cache_home=root / "cache",
                        )
                    )
                    self.assertTrue(
                        failed_thumbnail_path(root / "mount" / item.name, root / "cache").exists()
                    )
                    nursery.cancel_scope.cancel()

        trio.run(scenario)

    def test_disabled_downloads_suppress_without_mount_sort_or_network(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = [entry(1), entry(2), entry(3, "application/pdf")]
                cached_source = root / "mount" / entries[0].name
                missing_source = root / "mount" / entries[1].name
                install_thumbnail(
                    preview_bytes(),
                    cached_source,
                    4,
                    124,
                    cache_home=root / "cache",
                )
                stale = install_thumbnail(
                    preview_bytes(),
                    missing_source,
                    3,
                    125,
                    cache_home=root / "cache",
                    size="xx-large",
                )
                mount_ready = trio.Event()
                task_status = Mock()

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        raise AssertionError("disabled preview fetched")

                with (
                    patch(
                        "immich_on_demand.previewer._read_nautilus_sort",
                        side_effect=AssertionError("disabled preview read Nautilus sort"),
                    ),
                    patch(
                        "immich_on_demand.previewer.install_thumbnail",
                        side_effect=AssertionError("disabled preview installed a success"),
                    ),
                ):
                    stats = await populate_previews(
                        PreviewCatalog(entries),  # type: ignore[arg-type]
                        Client(),  # type: ignore[arg-type]
                        root / "mount",
                        cache_home=root / "cache",
                        mount_ready=mount_ready,
                        task_status=task_status,
                        downloads_enabled=False,
                    )

                task_status.started.assert_called_once_with()
                self.assertFalse(mount_ready.is_set())
                self.assertEqual(stats, PreviewStats(3, 1, 1, 1))
                self.assertTrue(thumbnail_cache_path(cached_source, root / "cache").exists())
                self.assertFalse(stale.exists())
                for item in entries:
                    self.assertTrue(
                        failed_thumbnail_path(
                            root / "mount" / item.name, root / "cache"
                        ).exists()
                    )

        trio.run(scenario)

    def test_installs_every_failure_record_before_first_network_call(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = [entry(1), entry(2, "application/pdf")]

                class Client:
                    calls = 0

                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        self.calls += 1
                        for item in entries:
                            source = root / "mount" / item.name
                            assert failed_thumbnail_path(source, root / "cache").exists()
                        return preview_bytes(), "image/jpeg"

                client = Client()
                stats = await populate_previews(
                    PreviewCatalog(entries),  # type: ignore[arg-type]
                    client,  # type: ignore[arg-type]
                    root / "mount",
                    cache_home=root / "cache",
                )

                self.assertEqual(client.calls, 1)
                self.assertEqual(stats, PreviewStats(2, 1, 0, 1))

        trio.run(scenario)

    def test_fetches_missing_previews_in_saved_nautilus_sort_order(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = [
                    entry(1, modified_ns=1),
                    entry(2, modified_ns=3),
                    entry(3, modified_ns=2),
                ]
                fetched: list[str] = []
                mount_ready = trio.Event()
                read_started = Event()
                release_read = Event()

                def read_sort(_: Path) -> tuple[str, bool] | None:
                    read_started.set()
                    return ("date_modified", True) if release_read.wait(0.1) else None

                async def allow_read() -> None:
                    while not read_started.is_set():
                        await trio.sleep(0)
                    release_read.set()

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        fetched.append(asset_id)
                        return preview_bytes(), "image/jpeg"

                with patch("immich_on_demand.previewer._read_nautilus_sort", read_sort):
                    async with trio.open_nursery() as nursery:
                        nursery.start_soon(allow_read)
                        await nursery.start(
                            partial(
                                populate_previews,
                                PreviewCatalog(entries),  # type: ignore[arg-type]
                                Client(),  # type: ignore[arg-type]
                                root / "mount",
                                cache_home=root / "cache",
                                concurrency=1,
                                mount_ready=mount_ready,
                            )
                        )
                        self.assertFalse(read_started.is_set())
                        mount_ready.set()
                        while len(fetched) < len(entries):
                            await trio.sleep(0)
                        nursery.cancel_scope.cancel()

                self.assertEqual(
                    fetched,
                    [entries[1].asset.id, entries[2].asset.id, entries[0].asset.id],
                )

        trio.run(scenario)

    def test_name_sort_uses_nautilus_filename_collation(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = [entry(10), entry(2)]
                fetched: list[str] = []

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        fetched.append(asset_id)
                        return preview_bytes(), "image/jpeg"

                with patch(
                    "immich_on_demand.previewer._read_nautilus_sort",
                    return_value=("name", False),
                ):
                    await populate_previews(
                        PreviewCatalog(entries),  # type: ignore[arg-type]
                        Client(),  # type: ignore[arg-type]
                        root / "mount",
                        cache_home=root / "cache",
                        concurrency=1,
                    )

                self.assertEqual(fetched, [entries[1].asset.id, entries[0].asset.id])

        trio.run(scenario)

    def test_reorders_pending_previews_after_nautilus_sort_changes(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = [entry(index, modified_ns=index) for index in range(1, 5)]
                fetched: list[str] = []

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        fetched.append(asset_id)
                        return preview_bytes(), "image/jpeg"

                with (
                    patch(
                        "immich_on_demand.previewer._read_nautilus_sort",
                        side_effect=[
                            ("date_modified", False),
                            ("date_modified", True),
                            ("date_modified", True),
                            ("date_modified", True),
                        ],
                    ),
                    patch("immich_on_demand.previewer.SORT_POLL_SECONDS", 0),
                ):
                    await populate_previews(
                        PreviewCatalog(entries),  # type: ignore[arg-type]
                        Client(),  # type: ignore[arg-type]
                        root / "mount",
                        cache_home=root / "cache",
                        concurrency=1,
                    )

                self.assertEqual(
                    fetched,
                    [
                        entries[0].asset.id,
                        entries[3].asset.id,
                        entries[2].asset.id,
                        entries[1].asset.id,
                    ],
                )

        trio.run(scenario)

    def test_unsupported_types_make_no_network_calls(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = entry(1, "application/pdf")
                source = root / "mount" / item.name
                competing = install_thumbnail(
                    preview_bytes(),
                    source,
                    4,
                    124,
                    cache_home=root / "cache",
                    size="xx-large",
                )

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        raise AssertionError("unsupported asset fetched")

                stats = await populate_previews(
                    PreviewCatalog([item]),  # type: ignore[arg-type]
                    Client(),  # type: ignore[arg-type]
                    root / "mount",
                    cache_home=root / "cache",
                )
                self.assertEqual(stats, PreviewStats(1, 0, 0, 1))
                self.assertFalse(competing.exists())
                self.assertTrue(failed_thumbnail_path(source, root / "cache").exists())

        trio.run(scenario)

    def test_success_keeps_failure_with_only_a_large_success_entry(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = entry(1)
                source = root / "mount" / item.name
                competing = install_thumbnail(
                    preview_bytes(),
                    source,
                    4,
                    124,
                    cache_home=root / "cache",
                    size="xx-large",
                )

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        self_outer.assertFalse(competing.exists())
                        self_outer.assertTrue(
                            failed_thumbnail_path(source, root / "cache").exists()
                        )
                        return preview_bytes(), "image/jpeg"

                self_outer = self
                stats = await populate_previews(
                    PreviewCatalog([item]),  # type: ignore[arg-type]
                    Client(),  # type: ignore[arg-type]
                    root / "mount",
                    cache_home=root / "cache",
                )
                success = thumbnail_cache_path(source, root / "cache")
                self.assertEqual(stats.installed, 1)
                self.assertTrue(success.exists())
                self.assertTrue(failed_thumbnail_path(source, root / "cache").exists())
                for size in THUMBNAIL_SIZES:
                    if size != "large":
                        self.assertFalse(
                            thumbnail_cache_path(source, root / "cache", size).exists()
                        )
                with Image.open(success) as image:
                    self.assertEqual(image.info["Thumb::MTime"], "4")
                    self.assertEqual(image.info["Thumb::Size"], "124")

        trio.run(scenario)

    def test_current_success_and_failure_are_reused_without_writes_or_network(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = entry(1)
                source = root / "mount" / item.name

                install_thumbnail(preview_bytes(), source, 4, 124, cache_home=root / "cache")
                for size in ("normal", "x-large", "xx-large"):
                    install_thumbnail(
                        preview_bytes(),
                        source,
                        4,
                        124,
                        cache_home=root / "cache",
                        size=size,
                    )
                install_failed_thumbnail(source, 4, 124, cache_home=root / "cache")

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        raise AssertionError("current preview fetched")

                with patch(
                    "immich_on_demand.thumbnails._atomic_png",
                    side_effect=AssertionError("current preview rewrote failure cache"),
                ):
                    stats = await populate_previews(
                        PreviewCatalog([item]),  # type: ignore[arg-type]
                        Client(),  # type: ignore[arg-type]
                        root / "mount",
                        cache_home=root / "cache",
                    )
                self.assertEqual(stats, PreviewStats(1, 1, 0, 0))
                self.assertTrue(failed_thumbnail_path(source, root / "cache").exists())
                self.assertTrue(thumbnail_cache_path(source, root / "cache").exists())
                for size in ("normal", "x-large", "xx-large"):
                    self.assertFalse(
                        thumbnail_cache_path(source, root / "cache", size).exists()
                    )

        trio.run(scenario)

    def test_removes_glib_priority_stale_success_before_signaling_ready(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = entry(1)
                source = root / "mount" / item.name
                stale = install_thumbnail(
                    preview_bytes(),
                    source,
                    3,
                    124,
                    cache_home=root / "cache",
                    size="xx-large",
                )

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        await trio.sleep_forever()

                async with trio.open_nursery() as nursery:
                    await nursery.start(
                        partial(
                            populate_previews,
                            PreviewCatalog([item]),  # type: ignore[arg-type]
                            Client(),  # type: ignore[arg-type]
                            root / "mount",
                            cache_home=root / "cache",
                        )
                    )
                    self.assertFalse(stale.exists())
                    self.assertTrue(
                        failed_thumbnail_path(source, root / "cache").exists()
                    )
                    nursery.cancel_scope.cancel()

        trio.run(scenario)

    def test_one_failure_does_not_stop_other_bounded_fetches(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = [entry(index) for index in range(1, 6)]

                class Client:
                    active = 0
                    maximum = 0

                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        self.active += 1
                        self.maximum = max(self.maximum, self.active)
                        await trio.sleep(0.01)
                        self.active -= 1
                        if asset_id == entries[0].asset.id:
                            raise RuntimeError("preview unavailable")
                        return preview_bytes(), "image/jpeg"

                client = Client()
                stats = await populate_previews(
                    PreviewCatalog(entries),  # type: ignore[arg-type]
                    client,  # type: ignore[arg-type]
                    root / "mount",
                    cache_home=root / "cache",
                    concurrency=2,
                )

                failed_source = root / "mount" / entries[0].name
                good_source = root / "mount" / entries[1].name
                self.assertEqual(stats, PreviewStats(5, 4, 1, 0))
                self.assertEqual(client.maximum, 2)
                self.assertTrue(failed_thumbnail_path(failed_source, root / "cache").exists())
                self.assertTrue(thumbnail_cache_path(good_source, root / "cache").exists())

        trio.run(scenario)


if __name__ == "__main__":
    unittest.main()
