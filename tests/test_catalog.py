from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from immich_on_demand.catalog import Catalog
from immich_on_demand.model import Asset


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
OTHER_ID = "22345678-1234-4234-8234-123456789abc"
OWNER_ID = "87654321-4321-4321-8321-cba987654321"


def asset(asset_id: str = ASSET_ID, name: str = "photo.jpg") -> Asset:
    return Asset(
        id=asset_id,
        owner_id=OWNER_ID,
        original_name=name,
        mime_type="image/jpeg",
        size=123,
        created_ns=1,
        modified_ns=2,
        updated_at="2026-08-25T12:00:00Z",
        checksum="abc=",
        visibility="timeline",
        is_trashed=False,
        is_offline=False,
        library_id=None,
    )


class CatalogTest(unittest.TestCase):
    def test_refresh_preserves_existing_name_and_inode_on_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh()
                first = catalog.by_name("photo.jpg")
                assert first is not None

                catalog.begin_refresh()
                catalog.stage([asset(), asset(OTHER_ID)])
                catalog.finish_refresh()
                existing = catalog.by_inode(first.inode)
                added = catalog.by_name(f"photo__{OTHER_ID}.jpg")

                self.assertEqual(existing and existing.name, "photo.jpg")
                self.assertEqual(existing and existing.asset.id, ASSET_ID)
                self.assertEqual(added and added.asset.id, OTHER_ID)

    def test_incomplete_refresh_does_not_change_live_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh()
                catalog.begin_refresh()
                catalog.stage([asset(OTHER_ID)])

                self.assertEqual([entry.asset.id for entry in catalog.list_visible()], [ASSET_ID])

    def test_hides_non_filesystem_assets_and_reports_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage(
                    [
                        asset(),
                        replace(asset(OTHER_ID), size=None),
                        replace(asset("32345678-1234-4234-8234-123456789abc"), is_trashed=True),
                        replace(asset("42345678-1234-4234-8234-123456789abc"), visibility="hidden"),
                    ]
                )
                stats = catalog.finish_refresh()

                self.assertEqual(stats.total, 4)
                self.assertEqual(stats.visible, 1)
                self.assertEqual(stats.missing_size, 1)
                self.assertEqual(stats.trashed, 1)
                self.assertEqual(stats.hidden, 1)

    def test_uploaded_asset_gets_stable_inode_and_deterministic_collision_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh()

                added = catalog.add_uploaded(asset(OTHER_ID), "photo.jpg")
                refreshed = catalog.add_uploaded(
                    replace(asset(OTHER_ID), modified_ns=99), "ignored.jpg"
                )

                self.assertEqual(added.name, f"photo__{OTHER_ID}.jpg")
                self.assertEqual((added.inode, added.name), (refreshed.inode, refreshed.name))
                self.assertEqual(refreshed.asset.modified_ns, 99)

    def test_marks_only_a_known_asset_trashed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                entry = catalog.add_uploaded(asset(), "photo.jpg")
                catalog.mark_trashed(entry.asset.id)

                self.assertEqual(catalog.list_visible(), [])
                trashed = catalog.by_inode(entry.inode)
                self.assertTrue(trashed and trashed.asset.is_trashed)
                with self.assertRaises(KeyError):
                    catalog.mark_trashed(OTHER_ID)
