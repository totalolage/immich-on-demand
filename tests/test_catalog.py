from dataclasses import replace
import hmac
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from immich_on_demand.catalog import Catalog, TrustedProfile
from immich_on_demand.model import Asset


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
LITERAL_ID = "17345678-1234-4234-8234-123456789abc"
OTHER_ID = "22345678-1234-4234-8234-123456789abc"
OWNER_ID = "87654321-4321-4321-8321-cba987654321"
READ_SCOPES = frozenset({"asset.download", "asset.read", "asset.view", "user.read"})


def trusted_profile(**changes: object) -> TrustedProfile:
    values: dict[str, object] = {
        "server_origin": "https://photos.example.test",
        "owner_id": OWNER_ID,
        "server_version": "3.0.3",
        "read_permissions": READ_SCOPES,
        "read_key_sha256": "a" * 64,
    }
    values.update(changes)
    return TrustedProfile(**values)  # type: ignore[arg-type]


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
    def test_requires_an_exact_complete_offline_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            profile = trusted_profile()
            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                    trusted_profile=profile,
                )

                with patch(
                    "immich_on_demand.catalog.hmac.compare_digest",
                    wraps=hmac.compare_digest,
                ) as compare_digest:
                    self.assertIsNone(catalog.require_offline_profile(profile))

                compare_digest.assert_called_once_with("a" * 64, "a" * 64)

    def test_offline_profile_rejects_every_authority_mismatch_with_one_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            profile = trusted_profile()
            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                    trusted_profile=profile,
                )

                mismatches = (
                    trusted_profile(server_origin="https://other.example.test"),
                    trusted_profile(
                        owner_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                    ),
                    trusted_profile(read_key_sha256="b" * 64),
                )
                for expected in mismatches:
                    with self.subTest(expected=expected), self.assertRaisesRegex(
                        ValueError,
                        r"^catalog is not trusted for offline use$",
                    ):
                        catalog.require_offline_profile(expected)

    def test_offline_profile_requires_a_completed_nonempty_full_refresh(self) -> None:
        profile = trusted_profile()
        for condition in ("missing_profile", "empty", "incomplete"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "catalog.db"
                with Catalog(database) as catalog:
                    catalog.begin_refresh()
                    catalog.stage([] if condition == "empty" else [asset()])
                    catalog.finish_refresh(
                        high_water_ms=1,
                        page_count=1,
                        trusted_profile=(
                            None if condition == "missing_profile" else profile
                        ),
                    )
                if condition == "incomplete":
                    connection = sqlite3.connect(database)
                    try:
                        connection.execute(
                            "UPDATE metadata SET value = 0 "
                            "WHERE key = 'full_refresh_pages'"
                        )
                        connection.commit()
                    finally:
                        connection.close()

                with Catalog(database) as catalog, self.assertRaisesRegex(
                    ValueError,
                    r"^catalog is not trusted for offline use$",
                ):
                    catalog.require_offline_profile(profile)

    def test_offline_profile_requires_sqlite_quick_check_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            profile = trusted_profile()
            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                    trusted_profile=profile,
                )
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE damaged(value INTEGER CHECK(value = 1));
                    PRAGMA ignore_check_constraints = ON;
                    INSERT INTO damaged VALUES (2);
                    PRAGMA ignore_check_constraints = OFF;
                    """
                )
                connection.commit()
                self.assertNotEqual(
                    connection.execute("PRAGMA quick_check").fetchall(), [("ok",)]
                )
            finally:
                connection.close()

            with Catalog(database) as catalog, self.assertRaisesRegex(
                ValueError,
                r"^catalog is not trusted for offline use$",
            ):
                catalog.require_offline_profile(profile)

    def test_trusted_profile_schema_migrates_an_existing_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            with Catalog(database) as catalog:
                catalog.add_uploaded(asset(), "photo.jpg")
            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP TABLE trusted_profile")
                connection.commit()
            finally:
                connection.close()

            with Catalog(database) as catalog:
                self.assertIsNone(catalog.trusted_profile())
                self.assertEqual(
                    [entry.asset.id for entry in catalog.list_visible()], [ASSET_ID]
                )

    def test_full_refresh_persists_a_normalized_trusted_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            profile = trusted_profile(
                server_origin="HTTPS://PHOTOS.EXAMPLE.TEST:443/",
            )
            self.assertEqual(profile.server_origin, "https://photos.example.test")
            self.assertEqual(profile.format_version, 1)

            with Catalog(database) as catalog:
                self.assertIsNone(catalog.trusted_profile())
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                    trusted_profile=profile,
                )
                self.assertEqual(catalog.trusted_profile(), profile)

            with Catalog(database) as catalog:
                self.assertEqual(catalog.trusted_profile(), profile)
                catalog.begin_refresh()
                catalog.stage([asset(OTHER_ID, "other.jpg")])
                catalog.finish_refresh(high_water_ms=2, page_count=1)
                self.assertEqual(catalog.trusted_profile(), profile)

    def test_trusted_profile_rejects_invalid_authority_fields(self) -> None:
        invalid = (
            {"format_version": 2},
            {"server_origin": "http://photos.example.test"},
            {"server_origin": "https://user@photos.example.test"},
            {"server_origin": "https://photos.example.test/path"},
            {"server_origin": "https://bad host"},
            {"owner_id": "not-a-uuid"},
            {"server_version": "3.0.4"},
            {"read_permissions": set(READ_SCOPES)},
            {"read_permissions": frozenset()},
            {"read_permissions": READ_SCOPES - {"asset.download"}},
            {"read_permissions": READ_SCOPES | {"asset.delete"}},
            {"read_permissions": frozenset({"asset.read\n"})},
            {"read_key_sha256": "A" * 64},
            {"read_key_sha256": "a" * 63},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(
                (TypeError, ValueError)
            ):
                trusted_profile(**changes)

    def test_rejects_malformed_persisted_trusted_profile(self) -> None:
        corruptions = (
            ("format_version", 2),
            ("server_origin", "http://photos.example.test"),
            ("owner_id", "not-a-uuid"),
            ("server_version", "3.0.4"),
            ("read_permissions", '["user.read","user.read"]'),
            (
                "read_permissions",
                sqlite3.Binary(
                    b'["asset.download","asset.read","asset.view","user.read"]'
                ),
            ),
            ("read_permissions", "not-json"),
            ("read_key_sha256", "A" * 64),
        )
        for column, value in corruptions:
            with self.subTest(column=column), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "catalog.db"
                with Catalog(database) as catalog:
                    catalog.begin_refresh()
                    catalog.stage([asset()])
                    catalog.finish_refresh(
                        high_water_ms=1,
                        page_count=1,
                        trusted_profile=trusted_profile(),
                    )
                connection = sqlite3.connect(database)
                try:
                    connection.execute(
                        f"UPDATE trusted_profile SET {column} = ?", (value,)
                    )
                    connection.commit()
                finally:
                    connection.close()

                with Catalog(database) as catalog:
                    with self.assertRaisesRegex(ValueError, "invalid trusted profile"):
                        catalog.trusted_profile()
                    with self.assertRaisesRegex(
                        ValueError,
                        r"^catalog is not trusted for offline use$",
                    ):
                        catalog.require_offline_profile(trusted_profile())

    def test_full_refresh_rejects_a_profile_that_does_not_own_staged_assets(self) -> None:
        other_owner = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])

                with self.assertRaisesRegex(ValueError, "does not own"):
                    catalog.finish_refresh(
                        high_water_ms=1,
                        page_count=1,
                        trusted_profile=trusted_profile(owner_id=other_owner),
                    )

                self.assertIsNone(catalog.trusted_profile())
                self.assertEqual(catalog.list_visible(), [])

    def test_offline_profile_requires_every_asset_to_have_the_expected_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                    trusted_profile=trusted_profile(),
                )
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE assets SET owner_id = ?",
                    ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",),
                )
                connection.commit()
            finally:
                connection.close()

            with Catalog(database) as catalog, self.assertRaisesRegex(
                ValueError,
                r"^catalog is not trusted for offline use$",
            ):
                catalog.require_offline_profile(trusted_profile())

    def test_profile_and_asset_refresh_roll_back_together_on_sql_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            original_profile = trusted_profile()
            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                    trusted_profile=original_profile,
                )

            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """
                    CREATE TRIGGER reject_trusted_profile
                    BEFORE INSERT ON trusted_profile
                    BEGIN
                        SELECT RAISE(ABORT, 'forced trust failure');
                    END
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([asset(OTHER_ID, "other.jpg")])
                with self.assertRaisesRegex(sqlite3.IntegrityError, "forced trust"):
                    catalog.finish_refresh(
                        high_water_ms=2,
                        page_count=1,
                        trusted_profile=trusted_profile(read_key_sha256="c" * 64),
                    )

                self.assertEqual(catalog.trusted_profile(), original_profile)
                self.assertEqual(
                    [entry.asset.id for entry in catalog.list_visible()], [ASSET_ID]
                )
                self.assertEqual(catalog.refresh_state(), (1, 1))

    def test_pin_schema_migrates_in_place_and_uses_existing_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            database = state / "catalog.db"
            with Catalog(database):
                pass

            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP TABLE pins")
                connection.commit()
            finally:
                connection.close()

            with Catalog(database) as catalog:
                self.assertEqual(catalog.pinned_ids(), frozenset())
                catalog.pin(ASSET_ID)

            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            suffixes = {
                path.name.removeprefix("catalog.db") for path in state.iterdir()
            }
            self.assertIn("", suffixes)
            self.assertLessEqual(suffixes, {"", "-journal", "-shm", "-wal"})

    def test_pins_persist_by_asset_id_independently_of_catalog_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(high_water_ms=1, page_count=1)
                catalog.pin(ASSET_ID)
                catalog.pin(ASSET_ID)
                self.assertEqual(catalog.pinned_ids(), frozenset({ASSET_ID}))
                with self.assertRaises(ValueError):
                    catalog.pin("not-an-asset-id")

                catalog.begin_refresh()
                catalog.finish_refresh(high_water_ms=2, page_count=1)
                self.assertEqual(catalog.list_visible(), [])
                self.assertEqual(catalog.pinned_ids(), frozenset({ASSET_ID}))

            with Catalog(database) as catalog:
                self.assertEqual(catalog.pinned_ids(), frozenset({ASSET_ID}))
                catalog.unpin(ASSET_ID)
                catalog.unpin(ASSET_ID)
                self.assertEqual(catalog.pinned_ids(), frozenset())

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
            state.mkdir(mode=0o700)
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
            real_fstat = os.fstat

            def wrong_owner(descriptor: int) -> os.stat_result | SimpleNamespace:
                info = real_fstat(descriptor)
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)

            with (
                patch("immich_on_demand.catalog.os.fstat", side_effect=wrong_owner),
                patch(
                    "immich_on_demand.catalog.sqlite3.connect",
                    side_effect=AssertionError("opened an unsafe database"),
                ) as connect,
            ):
                with self.assertRaisesRegex(PermissionError, "catalog database"):
                    Catalog(database)
            connect.assert_not_called()

    def test_sqlite_opens_the_checked_database_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state" / "catalog.db"
            real_connect = __import__("sqlite3").connect

            def checked_connect(path: str):
                self.assertTrue(path.startswith("/proc/self/fd/"))
                self.assertTrue(os.path.samefile(path, database))
                return real_connect(path)

            with patch("immich_on_demand.catalog.sqlite3.connect", side_effect=checked_connect):
                with Catalog(database):
                    pass

    @unittest.skipUnless(hasattr(os, "O_CLOEXEC"), "platform lacks O_CLOEXEC")
    def test_database_descriptor_is_close_on_exec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            real_open = os.open
            opened_flags: list[int] = []

            def checked_open(path: Path, flags: int, mode: int = 0o777) -> int:
                opened_flags.append(flags)
                return real_open(path, flags, mode)

            with patch(
                "immich_on_demand.catalog.os.open", side_effect=checked_open
            ):
                with Catalog(database):
                    pass

            self.assertTrue(opened_flags)
            self.assertTrue(all(flags & os.O_CLOEXEC for flags in opened_flags))

    def test_rejects_nonprivate_catalog_state_before_sqlite_opens(self) -> None:
        for case in ("directory", "database"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "state"
                state.mkdir(mode=0o700 if case == "database" else 0o755)
                state.chmod(0o700 if case == "database" else 0o755)
                database = state / "catalog.db"
                if case == "database":
                    database.write_bytes(b"")
                    database.chmod(0o644)

                with patch(
                    "immich_on_demand.catalog.sqlite3.connect",
                    side_effect=AssertionError("opened unsafe SQLite state"),
                ) as connect:
                    with self.assertRaisesRegex(PermissionError, case):
                        Catalog(database)

                connect.assert_not_called()

    def test_rejects_hardlinked_database_before_sqlite_opens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir(mode=0o700)
            target = Path(directory) / "target"
            target.write_bytes(b"")
            target.chmod(0o600)
            database = state / "catalog.db"
            os.link(target, database)

            with patch(
                "immich_on_demand.catalog.sqlite3.connect",
                side_effect=AssertionError("opened hardlinked database"),
            ) as connect:
                with self.assertRaisesRegex(PermissionError, "database"):
                    Catalog(database)

            connect.assert_not_called()

    def test_rejects_nonprivate_or_hardlinked_sqlite_auxiliary_files(self) -> None:
        for condition in ("mode", "hardlink"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "state"
                state.mkdir(mode=0o700)
                database = state / "catalog.db"
                database.write_bytes(b"")
                database.chmod(0o600)
                auxiliary = Path(f"{database}-wal")
                if condition == "mode":
                    auxiliary.write_bytes(b"")
                    auxiliary.chmod(0o644)
                else:
                    target = Path(directory) / "target"
                    target.write_bytes(b"")
                    target.chmod(0o600)
                    os.link(target, auxiliary)

                with patch(
                    "immich_on_demand.catalog.sqlite3.connect",
                    side_effect=AssertionError("opened unsafe SQLite state"),
                ) as connect:
                    with self.assertRaisesRegex(PermissionError, "auxiliary files"):
                        Catalog(database)

                connect.assert_not_called()

    def test_rejects_unsafe_sqlite_auxiliary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir(mode=0o700)
            database = state / "catalog.db"
            database.write_bytes(b"")
            database.chmod(0o600)
            target = Path(directory) / "target"
            target.write_bytes(b"do not touch")
            Path(f"{database}-wal").symlink_to(target)

            with patch(
                "immich_on_demand.catalog.sqlite3.connect",
                side_effect=AssertionError("opened unsafe SQLite state"),
            ) as connect:
                with self.assertRaisesRegex(PermissionError, "auxiliary files"):
                    Catalog(database)

            connect.assert_not_called()
            self.assertEqual(target.read_bytes(), b"do not touch")

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
                catalog.finish_refresh(high_water_ms=1_787_659_200_000, page_count=1)
                first = catalog.by_name("photo.jpg")
                assert first is not None

                catalog.begin_refresh()
                catalog.stage([asset(), asset(OTHER_ID)])
                catalog.finish_refresh(high_water_ms=1_787_659_200_000, page_count=1)
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
                catalog.finish_refresh(high_water_ms=1_787_659_200_000, page_count=1)

                names = {entry.asset.id: entry.name for entry in catalog.list_visible()}
                self.assertEqual(names[ASSET_ID], "photo.jpg")
                self.assertEqual(names[LITERAL_ID], generated)
                self.assertEqual(names[OTHER_ID], f"photo__{OTHER_ID}__2.jpg")

    def test_incomplete_refresh_does_not_change_live_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(high_water_ms=1_787_659_200_000, page_count=1)
                catalog.begin_refresh()
                catalog.stage([asset(OTHER_ID)])

                self.assertEqual([entry.asset.id for entry in catalog.list_visible()], [ASSET_ID])

    def test_incremental_refresh_upserts_without_removing_absent_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset(), asset(OTHER_ID, "other.jpg")])
                catalog.finish_refresh(high_water_ms=1000, page_count=2)
                before = catalog.by_name("photo.jpg")
                assert before is not None

                catalog.begin_refresh()
                catalog.stage(
                    [
                        replace(
                            asset(),
                            original_name="renamed.jpg",
                            updated_at="2026-08-25T12:05:00Z",
                        )
                    ]
                )
                catalog.finish_incremental(high_water_ms=2000)

                after = catalog.by_inode(before.inode)
                self.assertEqual(after and after.name, "photo.jpg")
                self.assertEqual(after and after.asset.original_name, "renamed.jpg")
                self.assertIsNotNone(catalog.by_name("other.jpg"))
                self.assertEqual(catalog.refresh_state(), (2000, 2))

    def test_complete_refresh_replaces_a_newer_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(high_water_ms=2000, page_count=2)

                catalog.begin_refresh()
                catalog.finish_refresh(high_water_ms=0, page_count=1)

                self.assertEqual(catalog.refresh_state(), (0, 1))
                self.assertEqual(catalog.list_visible(), [])

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
                stats = catalog.finish_refresh(
                    high_water_ms=1_787_659_200_000,
                    page_count=1,
                )

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
                catalog.finish_refresh(high_water_ms=1_787_659_200_000, page_count=1)

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
                catalog.finish_refresh(high_water_ms=1_787_659_200_000, page_count=1)

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

    def test_restores_only_a_known_asset_without_changing_its_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                entry = catalog.add_uploaded(
                    replace(asset(), is_trashed=True), "stable-photo.jpg"
                )

                self.assertEqual(catalog.by_id(entry.asset.id), entry)
                catalog.mark_restored(entry.asset.id)

                restored = catalog.by_id(entry.asset.id)
                assert restored is not None
                self.assertFalse(restored.asset.is_trashed)
                self.assertEqual(
                    (restored.inode, restored.name), (entry.inode, entry.name)
                )
                self.assertEqual(catalog.list_visible(), [restored])
                self.assertIsNone(catalog.by_id(OTHER_ID))
                with self.assertRaises(KeyError):
                    catalog.mark_restored(OTHER_ID)
