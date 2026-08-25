from io import BytesIO
from functools import partial
from pathlib import Path
import tempfile
import unittest

from PIL import Image
import trio

from immich_on_demand.catalog import CatalogAsset
from immich_on_demand.model import Asset
from immich_on_demand.previewer import PreviewStats, populate_previews
from immich_on_demand.thumbnails import (
    failed_thumbnail_path,
    thumbnail_cache_path,
)


def entry(index: int, mime_type: str = "image/jpeg") -> CatalogAsset:
    asset_id = f"{index:08d}-1234-4234-8234-123456789abc"
    return CatalogAsset(
        Asset(
            asset_id,
            "87654321-4321-4321-8321-cba987654321",
            f"asset-{index}.jpg",
            mime_type,
            123 + index,
            1,
            4_999_999_999,
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


class PreviewerTest(unittest.TestCase):
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
                            [item],
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
                    entries, client, root / "mount", cache_home=root / "cache"  # type: ignore[arg-type]
                )

                self.assertEqual(client.calls, 1)
                self.assertEqual(stats, PreviewStats(2, 1, 0, 1))

        trio.run(scenario)

    def test_unsupported_types_make_no_network_calls(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = entry(1, "application/pdf")

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        raise AssertionError("unsupported asset fetched")

                stats = await populate_previews(
                    [item], Client(), root / "mount", cache_home=root / "cache"  # type: ignore[arg-type]
                )
                source = root / "mount" / item.name
                self.assertEqual(stats, PreviewStats(1, 0, 0, 1))
                self.assertTrue(failed_thumbnail_path(source, root / "cache").exists())

        trio.run(scenario)

    def test_success_replaces_failure_with_standard_cache_entry(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = entry(1)

                class Client:
                    async def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
                        return preview_bytes(), "image/jpeg"

                stats = await populate_previews(
                    [item], Client(), root / "mount", cache_home=root / "cache"  # type: ignore[arg-type]
                )
                source = root / "mount" / item.name
                success = thumbnail_cache_path(source, root / "cache")
                self.assertEqual(stats.installed, 1)
                self.assertTrue(success.exists())
                self.assertFalse(failed_thumbnail_path(source, root / "cache").exists())
                with Image.open(success) as image:
                    self.assertEqual(image.info["Thumb::MTime"], "4")
                    self.assertEqual(image.info["Thumb::Size"], "124")

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
                    entries,
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
