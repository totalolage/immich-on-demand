from dataclasses import replace
import hmac
import os
from pathlib import Path, PurePosixPath
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from immich_on_demand.catalog import ROOT_INODE, Catalog, CatalogDirectory, TrustedProfile
from immich_on_demand.model import Album, Asset, Person


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
LITERAL_ID = "17345678-1234-4234-8234-123456789abc"
OTHER_ID = "22345678-1234-4234-8234-123456789abc"
ALBUM_ID = "32345678-1234-4234-8234-123456789abc"
OTHER_ALBUM_ID = "42345678-1234-4234-8234-123456789abc"
PERSON_ID = "52345678-1234-4234-8234-123456789abc"
OTHER_PERSON_ID = "62345678-1234-4234-8234-123456789abc"
OWNER_ID = "87654321-4321-4321-8321-cba987654321"
READ_SCOPES = frozenset({"asset.download", "asset.read", "asset.view", "user.read"})
RICH_READ_SCOPES = READ_SCOPES | {"album.read", "person.read"}


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


def rich_profile(**changes: object) -> TrustedProfile:
    return trusted_profile(
        format_version=2,
        read_permissions=RICH_READ_SCOPES,
        **changes,
    )


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


def viewed_asset(
    asset_id: str = ASSET_ID,
    name: str = "photo.jpg",
    *,
    local_date: str | None = "2026-08-25",
    is_favorite: bool = False,
) -> Asset:
    return replace(
        asset(asset_id, name),
        local_date=local_date,
        is_favorite=is_favorite,
    )


def album(
    album_id: str = ALBUM_ID, name: str = "Trips", *, asset_count: int = 1
) -> Album:
    return Album(album_id, name, "2026-08-25T12:00:00Z", asset_count)


def person(person_id: str = PERSON_ID, name: str = "Alice") -> Person:
    return Person(person_id, name, False, None)


class CatalogTest(unittest.TestCase):
    def test_namespace_always_exposes_the_five_fixed_views(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                root = catalog.node(1)
                self.assertEqual((root.inode, root.nlink), (1, 7))
                self.assertFalse(root.mutation_root)

                entries = catalog.children(1)
                self.assertEqual(
                    [entry.name for entry in entries],
                    ["Albums", "All", "Favorites", "People", "by Date"],
                )
                for entry in entries:
                    self.assertEqual(entry.node, catalog.lookup(1, entry.name))
                    self.assertEqual(entry.node.nlink, 2)
                    self.assertEqual(
                        entry.node.mutation_root, entry.name == "All"
                    )

                all_directory = catalog.lookup(1, "All")
                by_date = catalog.lookup(1, "by Date")
                assert all_directory is not None
                assert by_date is not None
                self.assertEqual(catalog.lookup(1, "."), root)
                self.assertEqual(catalog.lookup(1, ".."), root)
                self.assertEqual(
                    catalog.lookup(all_directory.inode, ".."), root
                )
                self.assertEqual(
                    catalog.lookup(by_date.inode, "."), by_date
                )

    def test_replace_album_people_materializes_populated_and_empty_collections(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                )
                self.assertIsNone(catalog.trusted_profile())

                self.assertIsNone(
                    catalog.replace_album_people(
                        albums=(
                            album(),
                            album(OTHER_ALBUM_ID, "Empty", asset_count=0),
                        ),
                        album_memberships=((ALBUM_ID, ASSET_ID),),
                        people=(person(), person(OTHER_PERSON_ID, "Nobody")),
                        person_memberships=((PERSON_ID, ASSET_ID),),
                        trusted_profile=rich_profile(),
                    )
                )

                self.assertEqual(catalog.trusted_profile(), rich_profile())

                albums = catalog.lookup(1, "Albums")
                people = catalog.lookup(1, "People")
                assert albums is not None
                assert people is not None
                self.assertEqual(
                    [entry.name for entry in catalog.children(albums.inode)],
                    ["Empty", "Trips"],
                )
                self.assertEqual(
                    [entry.name for entry in catalog.children(people.inode)],
                    ["Alice", "Nobody"],
                )
                trip = catalog.lookup(albums.inode, "Trips")
                alice = catalog.lookup(people.inode, "Alice")
                assert trip is not None
                assert alice is not None
                aliases = (
                    catalog.lookup(trip.inode, "photo.jpg"),
                    catalog.lookup(alice.inode, "photo.jpg"),
                )
                self.assertTrue(all(alias is not None for alias in aliases))
                self.assertEqual({alias.inode for alias in aliases}, {aliases[0].inode})
                self.assertEqual({alias.nlink for alias in aliases}, {3})
                self.assertEqual(
                    catalog.aliases(ASSET_ID),
                    (
                        PurePosixPath("Albums/Trips/photo.jpg"),
                        PurePosixPath("All/photo.jpg"),
                        PurePosixPath("People/Alice/photo.jpg"),
                    ),
                )

    def test_collection_names_and_inodes_survive_collisions_renames_and_tombstones(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                )
                catalog.replace_album_people(
                    albums=(
                        album(OTHER_ALBUM_ID, "Trips"),
                        album(ALBUM_ID, "Trips"),
                    ),
                    album_memberships=((OTHER_ALBUM_ID, ASSET_ID),),
                    people=(person(PERSON_ID, ""),),
                    person_memberships=(),
                    trusted_profile=rich_profile(),
                )

                albums = catalog.lookup(1, "Albums")
                people = catalog.lookup(1, "People")
                assert albums is not None
                assert people is not None
                original = {
                    entry.name: entry.node.inode
                    for entry in catalog.children(albums.inode)
                }
                self.assertEqual(
                    set(original),
                    {"Trips", f"Trips__{OTHER_ALBUM_ID}"},
                )
                unnamed = catalog.children(people.inode)
                self.assertEqual(
                    [entry.name for entry in unnamed],
                    [f"Unnamed__{PERSON_ID}"],
                )
                unnamed_inode = unnamed[0].node.inode

                catalog.replace_album_people(
                    albums=(album(OTHER_ALBUM_ID, "Renamed"),),
                    album_memberships=((OTHER_ALBUM_ID, ASSET_ID),),
                    people=(),
                    person_memberships=(),
                )
                remaining = catalog.children(albums.inode)
                self.assertEqual(
                    [(entry.name, entry.node.inode) for entry in remaining],
                    [
                        (
                            f"Trips__{OTHER_ALBUM_ID}",
                            original[f"Trips__{OTHER_ALBUM_ID}"],
                        )
                    ],
                )
                self.assertEqual(catalog.children(people.inode), ())

                catalog.replace_album_people(
                    albums=(
                        album(ALBUM_ID, "A later rename"),
                        album(OTHER_ALBUM_ID, "Renamed again"),
                    ),
                    album_memberships=((OTHER_ALBUM_ID, ASSET_ID),),
                    people=(person(PERSON_ID, "A name now"),),
                    person_memberships=(),
                )
                self.assertEqual(
                    {
                        entry.name: entry.node.inode
                        for entry in catalog.children(albums.inode)
                    },
                    original,
                )
                self.assertEqual(
                    catalog.children(people.inode)[0].node.inode,
                    unnamed_inode,
                )

    def test_collection_names_sanitize_slashes_dots_and_controls_before_collisions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.replace_album_people(
                    albums=(
                        album(ALBUM_ID, "Trips/2026"),
                        album(OTHER_ALBUM_ID, "Trips_2026"),
                    ),
                    album_memberships=(),
                    people=(person(PERSON_ID, ".Alice\n"),),
                    person_memberships=(),
                    trusted_profile=rich_profile(),
                )

                albums = catalog.lookup(1, "Albums")
                people = catalog.lookup(1, "People")
                assert albums is not None
                assert people is not None
                original = {
                    entry.name: entry.node.inode
                    for entry in catalog.children(albums.inode)
                }
                self.assertEqual(
                    set(original),
                    {"Trips_2026", f"Trips_2026__{OTHER_ALBUM_ID}"},
                )
                self.assertEqual(
                    [entry.name for entry in catalog.children(people.inode)],
                    ["_Alice_"],
                )

                catalog.replace_album_people(
                    albums=(
                        album(ALBUM_ID, "Changed"),
                        album(OTHER_ALBUM_ID, "Changed"),
                    ),
                    album_memberships=(),
                    people=(),
                    person_memberships=(),
                )
                self.assertEqual(
                    {
                        entry.name: entry.node.inode
                        for entry in catalog.children(albums.inode)
                    },
                    original,
                )

    def test_asset_refreshes_preserve_relation_inventory_and_reproject_visibility(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                )
                catalog.replace_album_people(
                    albums=(album(),),
                    album_memberships=((ALBUM_ID, ASSET_ID),),
                    people=(person(),),
                    person_memberships=((PERSON_ID, ASSET_ID),),
                    trusted_profile=rich_profile(),
                )
                expected = (
                    PurePosixPath("Albums/Trips/photo.jpg"),
                    PurePosixPath("All/photo.jpg"),
                    PurePosixPath("People/Alice/photo.jpg"),
                )

                catalog.begin_refresh()
                catalog.stage([replace(asset(), updated_at="2026-08-26T12:00:00Z")])
                catalog.finish_refresh(
                    high_water_ms=2,
                    page_count=1,
                )
                self.assertEqual(catalog.aliases(ASSET_ID), expected)

                catalog.begin_refresh()
                catalog.stage([replace(asset(), is_trashed=True)])
                catalog.finish_incremental(high_water_ms=3)
                self.assertEqual(catalog.aliases(ASSET_ID), ())

                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_incremental(high_water_ms=4)
                self.assertEqual(catalog.aliases(ASSET_ID), expected)

                catalog.add_uploaded(
                    replace(asset(), updated_at="2026-08-27T12:00:00Z"),
                    "ignored.jpg",
                )
                self.assertEqual(catalog.aliases(ASSET_ID), expected)

                catalog.begin_refresh()
                catalog.finish_refresh(
                    high_water_ms=5,
                    page_count=1,
                )
                self.assertEqual(catalog.aliases(ASSET_ID), ())

                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=6,
                    page_count=1,
                )
                self.assertEqual(
                    catalog.aliases(ASSET_ID),
                    (PurePosixPath("All/photo.jpg"),),
                )

    def test_replace_album_people_rejects_untrusted_or_invalid_inventory(self) -> None:
        invalid_asset_ids = (
            LITERAL_ID,
            "72345678-1234-4234-8234-123456789abc",
            "82345678-1234-4234-8234-123456789abc",
            "92345678-1234-4234-8234-123456789abc",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                )
                catalog.add_uploaded(
                    replace(asset(invalid_asset_ids[0], "foreign.jpg"), owner_id=OTHER_ID),
                    "foreign.jpg",
                )
                catalog.add_uploaded(
                    replace(asset(invalid_asset_ids[1], "hidden.jpg"), visibility="hidden"),
                    "hidden.jpg",
                )
                catalog.add_uploaded(
                    replace(asset(invalid_asset_ids[2], "offline.jpg"), is_offline=True),
                    "offline.jpg",
                )
                catalog.add_uploaded(
                    replace(asset(invalid_asset_ids[3], "unknown.jpg"), size=None),
                    "unknown.jpg",
                )
                catalog.replace_album_people(
                    albums=(album(),),
                    album_memberships=((ALBUM_ID, ASSET_ID),),
                    people=(person(),),
                    person_memberships=((PERSON_ID, ASSET_ID),),
                    trusted_profile=rich_profile(),
                )
                expected_aliases = catalog.aliases(ASSET_ID)
                albums_root = catalog.lookup(1, "Albums")
                assert albums_root is not None
                expected_albums = catalog.children(albums_root.inode)

                cases = (
                    {
                        "albums": (album(), album()),
                        "album_memberships": (),
                        "people": (),
                        "person_memberships": (),
                    },
                    {
                        "albums": (album(),),
                        "album_memberships": (
                            (ALBUM_ID, ASSET_ID),
                            (ALBUM_ID, ASSET_ID),
                        ),
                        "people": (),
                        "person_memberships": (),
                    },
                    {
                        "albums": (album(),),
                        "album_memberships": ((OTHER_ALBUM_ID, ASSET_ID),),
                        "people": (),
                        "person_memberships": (),
                    },
                    {
                        "albums": (album(ALBUM_ID.upper()),),
                        "album_memberships": (),
                        "people": (),
                        "person_memberships": (),
                    },
                    {
                        "albums": (album(),),
                        "album_memberships": ((ALBUM_ID, OTHER_ID),),
                        "people": (),
                        "person_memberships": (),
                    },
                    {
                        "albums": (album(),),
                        "album_memberships": ((ALBUM_ID, invalid_asset_ids[0]),),
                        "people": (),
                        "person_memberships": (),
                    },
                    {
                        "albums": (album(),),
                        "album_memberships": ((ALBUM_ID, invalid_asset_ids[1]),),
                        "people": (),
                        "person_memberships": (),
                    },
                    {
                        "albums": (album(),),
                        "album_memberships": ((ALBUM_ID, invalid_asset_ids[2]),),
                        "people": (),
                        "person_memberships": (),
                    },
                    {
                        "albums": (album(),),
                        "album_memberships": ((ALBUM_ID, invalid_asset_ids[3]),),
                        "people": (),
                        "person_memberships": (),
                    },
                    {
                        "albums": (),
                        "album_memberships": (),
                        "people": (replace(person(), is_hidden=True),),
                        "person_memberships": (),
                    },
                )
                for values in cases:
                    with self.subTest(values=values), self.assertRaises(ValueError):
                        catalog.replace_album_people(**values)
                    self.assertEqual(catalog.aliases(ASSET_ID), expected_aliases)
                    self.assertEqual(catalog.children(albums_root.inode), expected_albums)

                with self.assertRaisesRegex(ValueError, "version 2"):
                    catalog.replace_album_people(
                        albums=(),
                        album_memberships=(),
                        people=(),
                        person_memberships=(),
                        trusted_profile=trusted_profile(),
                    )
                self.assertEqual(catalog.trusted_profile(), rich_profile())

    def test_replace_album_people_rolls_back_directories_memberships_and_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                )
                catalog.replace_album_people(
                    albums=(album(),),
                    album_memberships=((ALBUM_ID, ASSET_ID),),
                    people=(person(),),
                    person_memberships=((PERSON_ID, ASSET_ID),),
                    trusted_profile=rich_profile(),
                )

            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TRIGGER reject_relation_projection
                    BEFORE INSERT ON namespace_links
                    WHEN NEW.directory_inode IN (
                        SELECT inode FROM namespace_directories
                         WHERE identity LIKE 'album:%'
                            OR identity LIKE 'person:%'
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'forced relation failure');
                    END;
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with Catalog(database) as catalog:
                before_aliases = catalog.aliases(ASSET_ID)
                albums_root = catalog.lookup(1, "Albums")
                assert albums_root is not None
                before_albums = catalog.children(albums_root.inode)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "forced relation"):
                    catalog.replace_album_people(
                        albums=(album(OTHER_ALBUM_ID, "Replacement"),),
                        album_memberships=((OTHER_ALBUM_ID, ASSET_ID),),
                        people=(),
                        person_memberships=(),
                        trusted_profile=rich_profile(read_key_sha256="b" * 64),
                    )

                self.assertEqual(catalog.aliases(ASSET_ID), before_aliases)
                self.assertEqual(catalog.children(albums_root.inode), before_albums)
                self.assertEqual(catalog.trusted_profile(), rich_profile())

    def test_namespace_migrates_a_legacy_catalog_without_changing_asset_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE assets (
                        id TEXT PRIMARY KEY,
                        inode INTEGER NOT NULL UNIQUE,
                        name TEXT NOT NULL UNIQUE,
                        owner_id TEXT NOT NULL,
                        original_name TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        size INTEGER,
                        created_ns INTEGER NOT NULL,
                        modified_ns INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        checksum TEXT NOT NULL,
                        visibility TEXT NOT NULL,
                        is_trashed INTEGER NOT NULL,
                        is_offline INTEGER NOT NULL,
                        library_id TEXT
                    );
                    CREATE TABLE incoming_assets (
                        id TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        original_name TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        size INTEGER,
                        created_ns INTEGER NOT NULL,
                        modified_ns INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        checksum TEXT NOT NULL,
                        visibility TEXT NOT NULL,
                        is_trashed INTEGER NOT NULL,
                        is_offline INTEGER NOT NULL,
                        library_id TEXT
                    );
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                    CREATE TABLE pins (asset_id TEXT PRIMARY KEY) WITHOUT ROWID;
                    INSERT INTO metadata VALUES ('next_inode', 43);
                    INSERT INTO metadata VALUES ('high_water_ms', 1);
                    INSERT INTO metadata VALUES ('full_refresh_pages', 1);
                    """
                )
                old = asset()
                connection.execute(
                    "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        old.id,
                        42,
                        "stable-photo.jpg",
                        old.owner_id,
                        old.original_name,
                        old.mime_type,
                        old.size,
                        old.created_ns,
                        old.modified_ns,
                        old.updated_at,
                        old.checksum,
                        old.visibility,
                        int(old.is_trashed),
                        int(old.is_offline),
                        old.library_id,
                    ),
                )
                connection.execute("INSERT INTO pins VALUES (?)", (old.id,))
                connection.commit()
            finally:
                connection.close()
            database.chmod(0o600)

            with Catalog(database) as catalog:
                migrated = catalog.by_id(ASSET_ID)
                self.assertEqual(
                    migrated and (migrated.inode, migrated.name),
                    (42, "stable-photo.jpg"),
                )
                self.assertEqual(catalog.pinned_ids(), frozenset({ASSET_ID}))
                self.assertEqual(
                    catalog.aliases(ASSET_ID),
                    (PurePosixPath("All/stable-photo.jpg"),),
                )
                assert migrated is not None
                self.assertIsNone(migrated.asset.local_date)
                self.assertFalse(migrated.asset.is_favorite)
                self.assertIsNone(migrated.asset.live_photo_video_id)
                for table in ("assets", "incoming_assets"):
                    columns = {
                        row["name"]
                        for row in catalog._connection.execute(
                            f"PRAGMA table_info({table})"
                        )
                    }
                    self.assertIn("live_photo_video_id", columns)
                indexes = {
                    row["name"]
                    for row in catalog._connection.execute(
                        "PRAGMA index_list(assets)"
                    )
                }
                self.assertIn("assets_live_photo_video", indexes)

    def test_failed_namespace_migration_stays_unpublished_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            with Catalog(database) as catalog:
                original = catalog.add_uploaded(asset(), "photo.jpg")
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    DELETE FROM metadata WHERE key = 'namespace_format';
                    DELETE FROM namespace_links;
                    CREATE TRIGGER reject_namespace_migration
                    BEFORE INSERT ON namespace_links
                    BEGIN
                        SELECT RAISE(ABORT, 'forced migration failure');
                    END;
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "forced migration failure"
            ):
                Catalog(database)
            connection = sqlite3.connect(database)
            try:
                self.assertIsNone(
                    connection.execute(
                        "SELECT value FROM metadata WHERE key = 'namespace_format'"
                    ).fetchone()
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT inode, name FROM assets WHERE id = ?", (ASSET_ID,)
                    ).fetchone(),
                    (original.inode, original.name),
                )
                connection.execute("DROP TRIGGER reject_namespace_migration")
                connection.commit()
            finally:
                connection.close()

            with Catalog(database) as catalog:
                self.assertEqual(
                    catalog.aliases(ASSET_ID),
                    (PurePosixPath("All/photo.jpg"),),
                )

    def test_namespace_reuses_one_asset_inode_and_reports_exact_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([viewed_asset(is_favorite=True)])
                catalog.finish_refresh(high_water_ms=1, page_count=1)

                all_directory = catalog.lookup(1, "All")
                date_directory = catalog.lookup(1, "by Date")
                favorites = catalog.lookup(1, "Favorites")
                assert all_directory is not None
                assert date_directory is not None
                assert favorites is not None
                year = catalog.lookup(date_directory.inode, "2026")
                assert year is not None
                month = catalog.lookup(year.inode, "08")
                assert month is not None
                day = catalog.lookup(month.inode, "25")
                assert day is not None

                aliases = (
                    catalog.lookup(all_directory.inode, "photo.jpg"),
                    catalog.lookup(day.inode, "photo.jpg"),
                    catalog.lookup(favorites.inode, "photo.jpg"),
                )
                self.assertTrue(all(alias is not None for alias in aliases))
                self.assertEqual({alias.inode for alias in aliases}, {aliases[0].inode})
                self.assertEqual({alias.nlink for alias in aliases}, {3})
                self.assertEqual(
                    catalog.aliases(ASSET_ID),
                    (
                        PurePosixPath("All/photo.jpg"),
                        PurePosixPath("Favorites/photo.jpg"),
                        PurePosixPath("by Date/2026/08/25/photo.jpg"),
                    ),
                )
                self.assertEqual(
                    [entry.name for entry in catalog.children(day.inode)],
                    ["photo.jpg"],
                )

    def test_alias_walk_is_bounded_by_the_namespace_depth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([viewed_asset()])
                catalog.finish_refresh(high_water_ms=1, page_count=1)
                catalog.replace_album_people(
                    albums=(album(),),
                    album_memberships=((ALBUM_ID, ASSET_ID),),
                    people=(),
                    person_memberships=(),
                    trusted_profile=rich_profile(),
                )
                albums = catalog.lookup(ROOT_INODE, "Albums")
                assert isinstance(albums, CatalogDirectory)
                trips = catalog.lookup(albums.inode, "Trips")
                assert isinstance(trips, CatalogDirectory)
                catalog._connection.execute(
                    "UPDATE namespace_directories SET parent_inode = ? WHERE inode = ?",
                    (trips.inode, trips.inode),
                )
                catalog._connection.set_progress_handler(lambda: 1, 10_000)
                try:
                    aliases = catalog.aliases(ASSET_ID)
                finally:
                    catalog._connection.set_progress_handler(None, 0)

                self.assertEqual(
                    aliases,
                    (
                        PurePosixPath("All/photo.jpg"),
                        PurePosixPath("by Date/2026/08/25/photo.jpg"),
                    ),
                )

    def test_namespace_hides_every_nonvisible_asset_from_every_view(self) -> None:
        hidden_ids = (
            OTHER_ID,
            "32345678-1234-4234-8234-123456789abc",
            "42345678-1234-4234-8234-123456789abc",
            "52345678-1234-4234-8234-123456789abc",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage(
                    [
                        viewed_asset(),
                        replace(viewed_asset(hidden_ids[0], "trashed.jpg"), is_trashed=True),
                        replace(viewed_asset(hidden_ids[1], "offline.jpg"), is_offline=True),
                        replace(viewed_asset(hidden_ids[2], "hidden.jpg"), visibility="hidden"),
                        replace(viewed_asset(hidden_ids[3], "unknown.jpg"), size=None),
                    ]
                )
                catalog.finish_refresh(high_water_ms=1, page_count=1)

                all_directory = catalog.lookup(1, "All")
                assert all_directory is not None
                self.assertEqual(
                    [entry.name for entry in catalog.children(all_directory.inode)],
                    ["photo.jpg"],
                )
                for asset_id in hidden_ids:
                    self.assertEqual(catalog.aliases(asset_id), ())
                    entry = catalog.by_id(asset_id)
                    assert entry is not None
                    self.assertIsNone(catalog.node(entry.inode))

    def test_namespace_date_directory_identity_survives_empty_and_return(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([viewed_asset()])
                catalog.finish_refresh(high_water_ms=1, page_count=1)
                by_date = catalog.lookup(1, "by Date")
                assert by_date is not None
                year = catalog.lookup(by_date.inode, "2026")
                assert year is not None
                month = catalog.lookup(year.inode, "08")
                assert month is not None
                original = catalog.lookup(month.inode, "25")
                assert original is not None

                catalog.begin_refresh()
                catalog.stage([viewed_asset(local_date=None)])
                catalog.finish_incremental(high_water_ms=2)
                self.assertIsNone(catalog.lookup(month.inode, "25"))

                catalog.begin_refresh()
                catalog.stage([viewed_asset()])
                catalog.finish_incremental(high_water_ms=3)
                returned = catalog.lookup(month.inode, "25")
                self.assertEqual(returned and returned.inode, original.inode)

    def test_incremental_upload_trash_and_restore_reproject_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([viewed_asset(local_date="2026-08-25")])
                catalog.finish_refresh(high_water_ms=1, page_count=1)

                catalog.begin_refresh()
                catalog.stage(
                    [viewed_asset(local_date="2026-08-26", is_favorite=True)]
                )
                catalog.finish_incremental(high_water_ms=2)
                self.assertEqual(
                    catalog.aliases(ASSET_ID),
                    (
                        PurePosixPath("All/photo.jpg"),
                        PurePosixPath("Favorites/photo.jpg"),
                        PurePosixPath("by Date/2026/08/26/photo.jpg"),
                    ),
                )

                catalog.mark_trashed(ASSET_ID)
                self.assertEqual(catalog.aliases(ASSET_ID), ())
                catalog.mark_restored(ASSET_ID)
                self.assertEqual(len(catalog.aliases(ASSET_ID)), 3)

                uploaded = catalog.add_uploaded(
                    viewed_asset(OTHER_ID, "upload.jpg", local_date="2026-08-27"),
                    "upload.jpg",
                )
                self.assertEqual(
                    catalog.aliases(uploaded.asset.id),
                    (
                        PurePosixPath("All/upload.jpg"),
                        PurePosixPath("by Date/2026/08/27/upload.jpg"),
                    ),
                )

    def test_namespace_and_asset_refresh_roll_back_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([viewed_asset()])
                catalog.finish_refresh(high_water_ms=1, page_count=1)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    f"""
                    CREATE TRIGGER reject_namespace_link
                    BEFORE INSERT ON namespace_links
                    WHEN NEW.asset_id = '{OTHER_ID}'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced namespace failure');
                    END
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([viewed_asset(OTHER_ID, "other.jpg")])
                with self.assertRaisesRegex(sqlite3.IntegrityError, "forced namespace"):
                    catalog.finish_refresh(high_water_ms=2, page_count=1)

                self.assertEqual(catalog.refresh_state(), (1, 1))
                self.assertEqual(
                    catalog.aliases(ASSET_ID),
                    (
                        PurePosixPath("All/photo.jpg"),
                        PurePosixPath("by Date/2026/08/25/photo.jpg"),
                    ),
                )
                self.assertIsNone(catalog.by_id(OTHER_ID))

    def test_offline_profile_rejects_a_corrupt_namespace_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            profile = trusted_profile()
            with Catalog(database) as catalog:
                catalog.begin_refresh()
                catalog.stage([viewed_asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                    trusted_profile=profile,
                )
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "DELETE FROM namespace_links WHERE asset_id = ?", (ASSET_ID,)
                )
                connection.commit()
            finally:
                connection.close()

            with Catalog(database) as catalog, self.assertRaisesRegex(
                ValueError, r"^catalog is not trusted for offline use$"
            ):
                catalog.require_offline_profile(profile)

    def test_version_two_offline_profile_accepts_complete_relation_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = rich_profile()
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                )
                catalog.replace_album_people(
                    albums=(album(), album(OTHER_ALBUM_ID, "Empty")),
                    album_memberships=((ALBUM_ID, ASSET_ID),),
                    people=(person(),),
                    person_memberships=((PERSON_ID, ASSET_ID),),
                    trusted_profile=profile,
                )

                self.assertIsNone(catalog.require_offline_profile(profile))

    def test_offline_profile_rejects_corrupt_relation_inventory(self) -> None:
        corruptions = (
            f"""
            DELETE FROM namespace_links
             WHERE directory_inode = (
                SELECT inode FROM namespace_directories
                 WHERE identity = 'album:{ALBUM_ID}'
             )
            """,
            "DELETE FROM namespace_memberships",
            f"""
            UPDATE namespace_directories SET active = 0
             WHERE identity = 'album:{ALBUM_ID}'
            """,
            f"""
            UPDATE namespace_directories
               SET parent_inode = (
                    SELECT inode FROM namespace_directories
                     WHERE identity = 'view:people'
               )
             WHERE identity = 'album:{ALBUM_ID}'
            """,
            f"""
            UPDATE namespace_directories SET name = '.'
             WHERE identity = 'album:{ALBUM_ID}'
            """,
            f"""
            INSERT INTO namespace_memberships(directory_inode, asset_id)
            SELECT inode, '{OTHER_ID}' FROM namespace_directories
             WHERE identity = 'album:{ALBUM_ID}'
            """,
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "catalog.db"
                profile = rich_profile()
                with Catalog(database) as catalog:
                    catalog.begin_refresh()
                    catalog.stage([asset()])
                    catalog.finish_refresh(
                        high_water_ms=1,
                        page_count=1,
                    )
                    catalog.replace_album_people(
                        albums=(album(),),
                        album_memberships=((ALBUM_ID, ASSET_ID),),
                        people=(person(),),
                        person_memberships=((PERSON_ID, ASSET_ID),),
                        trusted_profile=profile,
                    )
                connection = sqlite3.connect(database)
                try:
                    connection.execute(corruption)
                    connection.commit()
                finally:
                    connection.close()

                with Catalog(database) as catalog, self.assertRaisesRegex(
                    ValueError, r"^catalog is not trusted for offline use$"
                ):
                    catalog.require_offline_profile(profile)

    def test_version_one_offline_profile_remains_compatible(self) -> None:
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

    def test_trusted_profile_versions_require_their_exact_permission_policy(self) -> None:
        self.assertEqual(trusted_profile().format_version, 1)
        self.assertEqual(rich_profile().format_version, 2)
        for values in (
            {"format_version": 0},
            {"format_version": 3},
            {"format_version": 1, "read_permissions": RICH_READ_SCOPES},
            {"format_version": 2, "read_permissions": READ_SCOPES},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                trusted_profile(**values)

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

    def test_offline_profile_rejects_invalid_live_photo_relationships(self) -> None:
        profile = trusted_profile()
        corruptions = (
            "noncanonical",
            "self_link",
            "missing_target",
            "wrong_owner",
            "non_video_target",
            "aliased_target",
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "catalog.db"
                still = replace(asset(), live_photo_video_id=OTHER_ID)
                motion = replace(
                    asset(OTHER_ID, "motion.mov"),
                    mime_type="video/quicktime",
                    visibility="hidden",
                )
                with Catalog(database) as catalog:
                    catalog.begin_refresh()
                    catalog.stage([still, motion])
                    catalog.finish_refresh(
                        high_water_ms=1,
                        page_count=1,
                        trusted_profile=profile,
                    )
                connection = sqlite3.connect(database)
                try:
                    if corruption == "noncanonical":
                        connection.execute(
                            "UPDATE assets SET live_photo_video_id = ? WHERE id = ?",
                            (OTHER_ID.upper(), ASSET_ID),
                        )
                    elif corruption == "self_link":
                        connection.execute(
                            "UPDATE assets SET live_photo_video_id = id WHERE id = ?",
                            (ASSET_ID,),
                        )
                    elif corruption == "missing_target":
                        connection.execute(
                            "UPDATE assets SET live_photo_video_id = ? WHERE id = ?",
                            (LITERAL_ID, ASSET_ID),
                        )
                    elif corruption == "wrong_owner":
                        connection.execute(
                            "UPDATE assets SET owner_id = ? WHERE id = ?",
                            (LITERAL_ID, OTHER_ID),
                        )
                    elif corruption == "non_video_target":
                        connection.execute(
                            "UPDATE assets SET mime_type = 'image/jpeg' WHERE id = ?",
                            (OTHER_ID,),
                        )
                    else:
                        all_inode = connection.execute(
                            "SELECT inode FROM namespace_directories "
                            "WHERE identity = 'view:all'"
                        ).fetchone()[0]
                        connection.execute(
                            "INSERT INTO namespace_links VALUES (?, ?)",
                            (all_inode, OTHER_ID),
                        )
                    connection.commit()
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

    def test_asset_refresh_rejects_version_two_trust_before_catalog_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                version_one = trusted_profile()
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(
                    high_water_ms=1,
                    page_count=1,
                    trusted_profile=version_one,
                )
                original = catalog.by_id(ASSET_ID)

                catalog.begin_refresh()
                catalog.stage(
                    [replace(asset(), updated_at="2026-08-27T12:00:00Z")]
                )
                with self.assertRaisesRegex(ValueError, "only a version 1"):
                    catalog.finish_refresh(
                        high_water_ms=2,
                        page_count=1,
                        trusted_profile=rich_profile(),
                    )

                self.assertEqual(catalog.by_id(ASSET_ID), original)
                self.assertEqual(catalog.trusted_profile(), version_one)
                self.assertEqual(catalog.refresh_state(), (1, 1))

                next_version_one = trusted_profile(read_key_sha256="b" * 64)
                catalog.finish_refresh(
                    high_water_ms=2,
                    page_count=1,
                    trusted_profile=next_version_one,
                )
                current = catalog.by_id(ASSET_ID)
                assert current is not None
                self.assertEqual(
                    current.asset.updated_at,
                    "2026-08-27T12:00:00Z",
                )
                self.assertEqual(catalog.trusted_profile(), next_version_one)

    def test_asset_refresh_cannot_downgrade_published_rich_trust(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(high_water_ms=1, page_count=1)
                catalog.replace_album_people(
                    albums=(album(),),
                    album_memberships=((ALBUM_ID, ASSET_ID),),
                    people=(),
                    person_memberships=(),
                    trusted_profile=rich_profile(),
                )
                original = catalog.by_id(ASSET_ID)

                catalog.begin_refresh()
                catalog.stage(
                    [replace(asset(), updated_at="2026-08-27T12:00:00Z")]
                )
                with self.assertRaisesRegex(ValueError, "downgrade"):
                    catalog.finish_refresh(
                        high_water_ms=2,
                        page_count=1,
                        trusted_profile=trusted_profile(),
                    )

                self.assertEqual(catalog.by_id(ASSET_ID), original)
                self.assertEqual(catalog.trusted_profile(), rich_profile())
                self.assertEqual(catalog.refresh_state(), (1, 1))

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
                first = catalog.by_id(ASSET_ID)
                assert first is not None

                catalog.begin_refresh()
                catalog.stage([asset(), asset(OTHER_ID)])
                catalog.finish_refresh(high_water_ms=1_787_659_200_000, page_count=1)
                existing = catalog.by_inode(first.inode)
                added = catalog.by_id(OTHER_ID)

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
                before = catalog.by_id(ASSET_ID)
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
                self.assertIsNotNone(catalog.by_id(OTHER_ID))
                self.assertEqual(catalog.refresh_state(), (2000, 2))

    def test_full_refresh_keeps_live_photo_link_and_hides_its_motion_asset(self) -> None:
        still = replace(
            viewed_asset(is_favorite=True),
            live_photo_video_id=OTHER_ID,
        )
        motion = replace(
            asset(OTHER_ID, "motion.mov"),
            mime_type="video/quicktime",
            visibility="hidden",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([still, motion])
                catalog.finish_refresh(high_water_ms=1000, page_count=1)

                stored = catalog.by_id(ASSET_ID)
                assert stored is not None
                self.assertEqual(stored.asset.live_photo_video_id, OTHER_ID)
                self.assertEqual(catalog.aliases(OTHER_ID), ())
                self.assertEqual(
                    [entry.asset.id for entry in catalog.list_visible()],
                    [ASSET_ID],
                )
                aliases = catalog.aliases(ASSET_ID)
                self.assertGreater(len(aliases), 1)
                for path in aliases:
                    parent_inode = ROOT_INODE
                    node = None
                    for name in path.parts:
                        node = catalog.lookup(parent_inode, name)
                        assert node is not None
                        parent_inode = node.inode
                    self.assertEqual(getattr(node, "asset", None), stored.asset)

    def test_live_photo_target_has_no_alias_even_when_archived(self) -> None:
        still = replace(asset(), live_photo_video_id=OTHER_ID)
        motion = replace(
            asset(OTHER_ID, "motion.mov"),
            mime_type="video/quicktime",
            visibility="archive",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([still, motion])
                catalog.finish_refresh(
                    high_water_ms=1000,
                    page_count=1,
                    trusted_profile=trusted_profile(),
                )
                catalog.replace_album_people(
                    albums=(),
                    album_memberships=(),
                    people=(),
                    person_memberships=(),
                    trusted_profile=rich_profile(),
                )

                self.assertEqual(catalog.aliases(OTHER_ID), ())
                self.assertEqual(
                    [entry.asset.id for entry in catalog.list_visible()],
                    [ASSET_ID],
                )
                self.assertEqual(catalog.stats().visible, 1)
                self.assertEqual(catalog.stats().hidden, 0)
                catalog.require_offline_profile(rich_profile())

    def test_refresh_rolls_back_missing_and_non_video_live_photo_links(self) -> None:
        for refresh_kind in ("full", "incremental"):
            for invalid_target in ("missing", "non_video"):
                with (
                    self.subTest(
                        refresh_kind=refresh_kind,
                        invalid_target=invalid_target,
                    ),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    target = asset(OTHER_ID, "not-motion.jpg")
                    initial = [asset()]
                    if invalid_target == "non_video":
                        initial.append(target)
                    with Catalog(Path(directory) / "catalog.db") as catalog:
                        catalog.begin_refresh()
                        catalog.stage(initial)
                        catalog.finish_refresh(high_water_ms=1000, page_count=1)
                        before = catalog.stats()

                        linked = replace(
                            asset(),
                            live_photo_video_id=(
                                OTHER_ID
                                if invalid_target == "non_video"
                                else LITERAL_ID
                            ),
                        )
                        catalog.begin_refresh()
                        catalog.stage(
                            [linked, target]
                            if refresh_kind == "full"
                            and invalid_target == "non_video"
                            else [linked]
                        )
                        with self.assertRaisesRegex(
                            ValueError,
                            r"^catalog contains an invalid Live Photo relationship$",
                        ):
                            if refresh_kind == "full":
                                catalog.finish_refresh(
                                    high_water_ms=2000,
                                    page_count=1,
                                )
                            else:
                                catalog.finish_incremental(high_water_ms=2000)

                        stored = catalog.by_id(ASSET_ID)
                        assert stored is not None
                        self.assertIsNone(stored.asset.live_photo_video_id)
                        self.assertEqual(catalog.stats(), before)
                        self.assertEqual(catalog.refresh_state(), (1000, 1))

    def test_incremental_refresh_updates_live_photo_link(self) -> None:
        motion = replace(
            asset(OTHER_ID, "motion.mov"),
            mime_type="video/quicktime",
            visibility="hidden",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset(), motion])
                catalog.finish_refresh(high_water_ms=1000, page_count=1)

                catalog.begin_refresh()
                catalog.stage(
                    [replace(asset(), live_photo_video_id=OTHER_ID)]
                )
                catalog.finish_incremental(high_water_ms=2000)

                stored = catalog.by_id(ASSET_ID)
                self.assertEqual(
                    stored and stored.asset.live_photo_video_id,
                    OTHER_ID,
                )
                self.assertEqual(catalog.aliases(OTHER_ID), ())

    def test_incremental_live_photo_link_relink_and_unlink_reproject_targets(self) -> None:
        still = asset()
        first_motion = replace(
            asset(OTHER_ID, "first.mov"),
            mime_type="video/quicktime",
            visibility="archive",
        )
        second_motion = replace(
            asset(LITERAL_ID, "second.mp4"),
            mime_type="video/mp4",
            visibility="archive",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([still, first_motion, second_motion])
                catalog.finish_refresh(high_water_ms=1000, page_count=1)

                self.assertTrue(catalog.aliases(OTHER_ID))
                self.assertTrue(catalog.aliases(LITERAL_ID))
                self.assertEqual(catalog.stats().visible, 3)

                for high_water_ms, motion_id, expected_visible in (
                    (2000, OTHER_ID, {ASSET_ID, LITERAL_ID}),
                    (3000, LITERAL_ID, {ASSET_ID, OTHER_ID}),
                    (4000, None, {ASSET_ID, OTHER_ID, LITERAL_ID}),
                ):
                    catalog.begin_refresh()
                    catalog.stage(
                        [replace(still, live_photo_video_id=motion_id)]
                    )
                    catalog.finish_incremental(high_water_ms=high_water_ms)

                    stored = catalog.by_id(ASSET_ID)
                    assert stored is not None
                    self.assertEqual(stored.asset.live_photo_video_id, motion_id)
                    self.assertEqual(
                        {entry.asset.id for entry in catalog.list_visible()},
                        expected_visible,
                    )
                    self.assertEqual(catalog.stats().visible, len(expected_visible))
                    self.assertEqual(
                        bool(catalog.aliases(OTHER_ID)),
                        OTHER_ID in expected_visible,
                    )
                    self.assertEqual(
                        bool(catalog.aliases(LITERAL_ID)),
                        LITERAL_ID in expected_visible,
                    )

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

    def test_uploaded_and_replacement_assets_keep_live_photo_link(self) -> None:
        motion = replace(
            asset(LITERAL_ID, "motion.mov"),
            mime_type="video/quicktime",
            visibility="archive",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.add_uploaded(motion, "motion.mov")
                uploaded = catalog.add_uploaded(
                    replace(asset(), live_photo_video_id=LITERAL_ID),
                    "photo.jpg",
                )

                self.assertEqual(uploaded.asset.live_photo_video_id, LITERAL_ID)
                replacement = catalog.publish_replacement(
                    old_asset_id=ASSET_ID,
                    candidate=replace(
                        asset(OTHER_ID),
                        live_photo_video_id=LITERAL_ID,
                    ),
                )

                self.assertEqual(replacement.asset.live_photo_video_id, LITERAL_ID)
                self.assertEqual(
                    catalog.by_id(OTHER_ID),
                    replacement,
                )
                self.assertEqual(catalog.aliases(LITERAL_ID), ())

    def test_replacement_atomically_moves_identity_name_pin_and_every_view(self) -> None:
        occupied_old_name = f"photo__{ASSET_ID}.jpg"
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage(
                    [
                        viewed_asset(is_favorite=True),
                        asset(LITERAL_ID, occupied_old_name),
                    ]
                )
                catalog.finish_refresh(high_water_ms=1, page_count=1)
                catalog.replace_album_people(
                    albums=(album(),),
                    album_memberships=((ALBUM_ID, ASSET_ID),),
                    people=(person(),),
                    person_memberships=((PERSON_ID, ASSET_ID),),
                    trusted_profile=rich_profile(),
                )
                catalog.pin(ASSET_ID)
                old = catalog.by_id(ASSET_ID)
                occupied = catalog.by_id(LITERAL_ID)
                assert old is not None
                assert occupied is not None
                old_aliases = catalog.aliases(ASSET_ID)
                self.assertEqual(catalog.album_ids(ASSET_ID), (ALBUM_ID,))

                replacement = catalog.publish_replacement(
                    old_asset_id=ASSET_ID,
                    candidate=viewed_asset(
                        OTHER_ID,
                        "replacement.jpg",
                        is_favorite=True,
                    ),
                )

                retired = catalog.by_id(ASSET_ID)
                assert retired is not None
                self.assertEqual(replacement.asset.id, OTHER_ID)
                self.assertEqual(replacement.name, old.name)
                self.assertNotEqual(replacement.inode, old.inode)
                self.assertEqual(catalog.by_inode(replacement.inode), replacement)
                self.assertEqual(catalog.by_inode(old.inode), retired)
                self.assertTrue(retired.asset.is_trashed)
                self.assertEqual(retired.inode, old.inode)
                self.assertEqual(retired.name, f"photo__{ASSET_ID}__2.jpg")
                self.assertEqual(catalog.by_id(LITERAL_ID), occupied)
                self.assertEqual(catalog.aliases(ASSET_ID), ())
                self.assertEqual(catalog.album_ids(ASSET_ID), ())
                self.assertEqual(catalog.album_ids(OTHER_ID), (ALBUM_ID,))
                self.assertEqual(
                    catalog.aliases(OTHER_ID),
                    tuple(path.with_name(old.name) for path in old_aliases),
                )
                self.assertEqual(catalog.pinned_ids(), frozenset({OTHER_ID}))

                catalog.mark_restored(ASSET_ID)

                self.assertEqual(
                    catalog.aliases(ASSET_ID),
                    (
                        PurePosixPath(f"All/{retired.name}"),
                        PurePosixPath(f"Favorites/{retired.name}"),
                        PurePosixPath(f"by Date/2026/08/25/{retired.name}"),
                    ),
                )
                self.assertEqual(
                    catalog.aliases(OTHER_ID),
                    tuple(path.with_name(old.name) for path in old_aliases),
                )

    def test_replacement_rejects_conflicts_without_changing_the_live_asset(self) -> None:
        invalid_candidates = (
            asset(ASSET_ID),
            replace(asset(OTHER_ID), owner_id=LITERAL_ID),
            replace(asset(OTHER_ID), is_trashed=True),
            replace(asset(OTHER_ID), is_offline=True),
            replace(asset(OTHER_ID), visibility="hidden"),
            replace(asset(OTHER_ID), size=None),
            replace(asset(OTHER_ID), library_id=LITERAL_ID),
            replace(asset(OTHER_ID), local_date="2026-08-26"),
            replace(asset(OTHER_ID), is_favorite=True),
            replace(asset(OTHER_ID), live_photo_video_id=LITERAL_ID),
        )
        for candidate in invalid_candidates:
            with self.subTest(candidate=candidate):
                with tempfile.TemporaryDirectory() as directory:
                    with Catalog(Path(directory) / "catalog.db") as catalog:
                        original = catalog.add_uploaded(asset(), "photo.jpg")
                        catalog.pin(ASSET_ID)
                        aliases = catalog.aliases(ASSET_ID)

                        with self.assertRaises(ValueError):
                            catalog.publish_replacement(
                                old_asset_id=ASSET_ID,
                                candidate=candidate,
                            )

                        self.assertEqual(catalog.by_id(ASSET_ID), original)
                        self.assertEqual(catalog.aliases(ASSET_ID), aliases)
                        self.assertEqual(catalog.pinned_ids(), frozenset({ASSET_ID}))
                        self.assertIsNone(catalog.by_id(OTHER_ID))

    def test_replacement_requires_distinct_live_catalog_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                old = catalog.add_uploaded(asset(), "photo.jpg")
                existing = catalog.add_uploaded(asset(OTHER_ID), "other.jpg")

                with self.assertRaisesRegex(ValueError, "already in the catalog"):
                    catalog.publish_replacement(
                        old_asset_id=ASSET_ID,
                        candidate=asset(OTHER_ID),
                    )

                self.assertEqual(catalog.by_id(ASSET_ID), old)
                self.assertEqual(catalog.by_id(OTHER_ID), existing)

                catalog.mark_trashed(ASSET_ID)
                retired = catalog.by_id(ASSET_ID)
                with self.assertRaisesRegex(ValueError, "live managed asset"):
                    catalog.publish_replacement(
                        old_asset_id=ASSET_ID,
                        candidate=asset(LITERAL_ID),
                    )

                self.assertEqual(catalog.by_id(ASSET_ID), retired)
                self.assertIsNone(catalog.by_id(LITERAL_ID))
                with self.assertRaisesRegex(ValueError, "canonical"):
                    catalog.publish_replacement(
                        old_asset_id=ASSET_ID.upper(),
                        candidate=asset(LITERAL_ID),
                    )

    def test_replacement_rolls_back_a_failure_after_the_transaction_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                original = catalog.add_uploaded(asset(), "photo.jpg")
                catalog.pin(ASSET_ID)
                aliases = catalog.aliases(ASSET_ID)
                next_inode = catalog._connection.execute(
                    "SELECT value FROM metadata WHERE key = 'next_inode'"
                ).fetchone()[0]
                catalog._connection.execute(
                    f"""
                    CREATE TEMP TRIGGER reject_replacement
                    BEFORE INSERT ON assets WHEN NEW.id = '{OTHER_ID}'
                    BEGIN SELECT RAISE(ABORT, 'injected replacement failure'); END
                    """
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    catalog.publish_replacement(
                        old_asset_id=ASSET_ID,
                        candidate=asset(OTHER_ID),
                    )

                self.assertEqual(catalog.by_id(ASSET_ID), original)
                self.assertEqual(catalog.aliases(ASSET_ID), aliases)
                self.assertEqual(catalog.pinned_ids(), frozenset({ASSET_ID}))
                self.assertIsNone(catalog.by_id(OTHER_ID))
                self.assertEqual(
                    catalog._connection.execute(
                        "SELECT value FROM metadata WHERE key = 'next_inode'"
                    ).fetchone()[0],
                    next_inode,
                )

    def test_album_ids_returns_sorted_canonical_active_memberships(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Catalog(Path(directory) / "catalog.db") as catalog:
                catalog.begin_refresh()
                catalog.stage([asset()])
                catalog.finish_refresh(high_water_ms=1, page_count=1)
                catalog.replace_album_people(
                    albums=(
                        album(OTHER_ALBUM_ID, "Other"),
                        album(ALBUM_ID, "Trips"),
                    ),
                    album_memberships=(
                        (OTHER_ALBUM_ID, ASSET_ID),
                        (ALBUM_ID, ASSET_ID),
                    ),
                    people=(person(),),
                    person_memberships=((PERSON_ID, ASSET_ID),),
                    trusted_profile=rich_profile(),
                )

                self.assertEqual(
                    catalog.album_ids(ASSET_ID),
                    tuple(sorted((ALBUM_ID, OTHER_ALBUM_ID))),
                )
                self.assertEqual(catalog.album_ids(OTHER_ID), ())
                with self.assertRaises(ValueError):
                    catalog.album_ids(ASSET_ID.upper())

                catalog._connection.execute(
                    "UPDATE namespace_directories SET identity = ? WHERE identity = ?",
                    (f"album:{ALBUM_ID.upper()}", f"album:{ALBUM_ID}"),
                )
                with self.assertRaisesRegex(ValueError, "album identity"):
                    catalog.album_ids(ASSET_ID)

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
