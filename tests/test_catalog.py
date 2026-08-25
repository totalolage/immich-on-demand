from dataclasses import replace
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from immich_on_demand.catalog import Catalog
from immich_on_demand.model import Asset


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
LITERAL_ID = "17345678-1234-4234-8234-123456789abc"
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
    def test_rejects_an_unsafe_state_directory_before_opening_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target"
            target.mkdir(mode=0o755)
            state = base / "state"
            state.symlink_to(target, target_is_directory=True)

            with patch(
                "immich_on_demand.catalog.sqlite3.connect",
                side_effect=AssertionError("opened an unsafe database"),
            ) as connect:
                with self.assertRaisesRegex(PermissionError, "state directory"):
                    Catalog(state / "catalog.db")

            connect.assert_not_called()
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(target.iterdir()), [])

            owned = base / "wrong-owner"
            owned.mkdir(mode=0o755)
            with (
                patch(
                    "immich_on_demand.catalog.os.getuid", return_value=os.getuid() + 1
                ),
                patch(
                    "immich_on_demand.catalog.sqlite3.connect",
                    side_effect=AssertionError("opened an unsafe database"),
                ) as connect,
            ):
                with self.assertRaisesRegex(PermissionError, "state directory"):
                    Catalog(owned / "catalog.db")

            connect.assert_not_called()
            self.assertEqual(owned.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(owned.iterdir()), [])

    def test_rejects_an_unsafe_database_before_opening_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            target = Path(directory) / "target"
            target.write_bytes(b"do not touch")
            database = state / "catalog.db"
            database.symlink_to(target)

            with patch(
                "immich_on_demand.catalog.sqlite3.connect",
                side_effect=AssertionError("opened an unsafe database"),
            ) as connect:
                with self.assertRaisesRegex(PermissionError, "catalog database"):
                    Catalog(database)

            connect.assert_not_called()
            self.assertTrue(database.is_symlink())
            self.assertEqual(target.read_bytes(), b"do not touch")

            database.unlink()
            database.mkdir()
            with patch(
                "immich_on_demand.catalog.sqlite3.connect",
                side_effect=AssertionError("opened an unsafe database"),
            ) as connect:
                with self.assertRaisesRegex(PermissionError, "catalog database"):
                    Catalog(database)
            connect.assert_not_called()
            self.assertTrue(database.is_dir())

            database.rmdir()
            database.write_bytes(b"")
            real_lstat = os.lstat

            def wrong_owner(candidate: os.PathLike[str]) -> os.stat_result | SimpleNamespace:
                info = real_lstat(candidate)
                if Path(candidate) == database:
                    return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                return info

            with (
                patch("immich_on_demand.catalog.os.lstat", side_effect=wrong_owner),
                patch(
                    "immich_on_demand.catalog.sqlite3.connect",
                    side_effect=AssertionError("opened an unsafe database"),
                ) as connect,
            ):
                with self.assertRaisesRegex(PermissionError, "catalog database"):
                    Catalog(database)
            connect.assert_not_called()

    def test_creates_a_private_catalog_and_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            database = state / "catalog.db"

            with Catalog(database):
                pass

            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)

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

    def test_refresh_retries_when_a_literal_name_matches_the_collision_name(self) -> None:
        generated = f"photo__{OTHER_ID}.jpg"
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage(
                    [
                        asset(ASSET_ID, "photo.jpg"),
                        asset(LITERAL_ID, generated),
                        asset(OTHER_ID, "photo.jpg"),
                    ]
                )
                catalog.finish_refresh()

                names = {entry.asset.id: entry.name for entry in catalog.list_visible()}
                self.assertEqual(names[ASSET_ID], "photo.jpg")
                self.assertEqual(names[LITERAL_ID], generated)
                self.assertEqual(names[OTHER_ID], f"photo__{OTHER_ID}__2.jpg")

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

    def test_uploaded_asset_retries_an_occupied_generated_name(self) -> None:
        generated = f"photo__{OTHER_ID}.jpg"
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage(
                    [asset(ASSET_ID, "photo.jpg"), asset(LITERAL_ID, generated)]
                )
                catalog.finish_refresh()

                added = catalog.add_uploaded(asset(OTHER_ID), "photo.jpg")

                self.assertEqual(added.name, f"photo__{OTHER_ID}__2.jpg")

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
