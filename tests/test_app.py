from dataclasses import dataclass, replace
from pathlib import Path
import tempfile
import unittest

import trio

from immich_on_demand.app import (
    reconcile_album_people,
    refresh_catalog,
    refresh_catalog_incremental,
)
from immich_on_demand.catalog import Catalog
from immich_on_demand.immich import ImmichError, ServerSession
from immich_on_demand.model import Album, Asset, Person
from test_catalog import ASSET_ID, OTHER_ID, OWNER_ID, asset, rich_profile, trusted_profile


class FakeClient:
    def __init__(self, sweeps: list[list[list[Asset]]]) -> None:
        self.sweeps = sweeps
        self.calls = 0

    async def asset_pages(self, owner_id: str):
        assert owner_id == OWNER_ID
        sweep = self.sweeps[self.calls]
        self.calls += 1
        for page in sweep:
            yield page


class IncrementalClient:
    def __init__(self, pages: list[list[Asset]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    async def asset_pages(self, owner_id: str, **kwargs: object):
        assert owner_id == OWNER_ID
        self.calls.append(kwargs)
        for page in self.pages:
            yield page


@dataclass(frozen=True)
class AlbumPeopleSweep:
    albums: tuple[Album, ...]
    album_pages: dict[str, list[list[Asset]]]
    people: tuple[Person, ...]
    people_pages: list[list[Asset]]


@dataclass(frozen=True)
class VisibleEntry:
    asset: Asset


class AlbumPeopleClient:
    def __init__(self, sweeps: list[AlbumPeopleSweep], lock: trio.Lock) -> None:
        self.sweeps = sweeps
        self.lock = lock
        self.sweep = 0
        self.page_calls: list[dict[str, object]] = []

    async def albums(self) -> list[Album]:
        assert self.lock.locked()
        return list(self.sweeps[self.sweep].albums)

    async def people(self) -> list[Person]:
        assert self.lock.locked()
        return list(self.sweeps[self.sweep].people)

    async def asset_pages(
        self,
        owner_id: str,
        *,
        album_id: str | None = None,
        with_people: bool = False,
    ):
        assert self.lock.locked()
        assert owner_id == OWNER_ID
        self.page_calls.append(
            {"album_id": album_id, "with_people": with_people}
        )
        current = self.sweeps[self.sweep]
        pages = (
            current.people_pages
            if with_people
            else current.album_pages[album_id or ""]
        )
        for page in pages:
            yield page
        if with_people:
            self.sweep += 1


class AlbumPeopleCatalog:
    def __init__(self, visible: list[Asset], lock: trio.Lock) -> None:
        self.visible = visible
        self.lock = lock
        self.published: list[dict[str, object]] = []

    def list_visible(self):
        assert self.lock.locked()
        return [VisibleEntry(item) for item in self.visible]

    def replace_album_people(
        self,
        *,
        albums: tuple[Album, ...],
        album_memberships: tuple[tuple[str, str], ...],
        people: tuple[Person, ...],
        person_memberships: tuple[tuple[str, str], ...],
        trusted_profile: object = None,
    ) -> None:
        assert self.lock.locked()
        self.published.append(
            {
                "albums": albums,
                "album_memberships": album_memberships,
                "people": people,
                "person_memberships": person_memberships,
                "trusted_profile": trusted_profile,
            }
        )


class AppTest(unittest.TestCase):
    def test_album_people_reconcile_publishes_one_filtered_stable_snapshot(self) -> None:
        album_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
        empty_album_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
        person_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
        empty_person_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
        unknown_person_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3"
        unknown_asset_id = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
        foreign_owner_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        albums = (
            Album(album_id, "Family", "2026-08-25T12:00:00Z", 4),
            Album(empty_album_id, "Empty", "2026-08-25T12:00:00Z", 0),
        )
        people = (
            Person(person_id, "Alice", False, "2026-08-25T12:00:00Z"),
            Person(empty_person_id, "Bob", False, None),
        )
        album_pages = {
            album_id: [
                [asset(), asset(), asset(unknown_asset_id, "unknown.jpg")],
                [replace(asset(OTHER_ID, "other.jpg"), owner_id=foreign_owner_id)],
            ],
            empty_album_id: [[]],
        }
        people_pages = [
            [
                replace(asset(), person_ids=(person_id, person_id, unknown_person_id)),
                replace(
                    asset(OTHER_ID, "other.jpg"),
                    owner_id=foreign_owner_id,
                    person_ids=(empty_person_id,),
                ),
                replace(
                    asset(unknown_asset_id, "unknown.jpg"),
                    person_ids=(empty_person_id,),
                ),
            ]
        ]
        stable = AlbumPeopleSweep(albums, album_pages, people, people_pages)

        async def scenario() -> None:
            lock = trio.Lock()
            catalog = AlbumPeopleCatalog(
                [asset(), asset(OTHER_ID, "other.jpg")], lock
            )
            client = AlbumPeopleClient([stable, stable], lock)
            session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)
            profile = rich_profile()

            await reconcile_album_people(
                catalog,  # type: ignore[arg-type]
                client,  # type: ignore[arg-type]
                session,
                lock,
                trusted_profile=profile,
            )

            self.assertEqual(
                catalog.published,
                [
                    {
                        "albums": albums,
                        "album_memberships": ((album_id, ASSET_ID),),
                        "people": people,
                        "person_memberships": ((person_id, ASSET_ID),),
                        "trusted_profile": profile,
                    }
                ],
            )
            self.assertEqual(
                client.page_calls,
                [
                    {"album_id": album_id, "with_people": False},
                    {"album_id": empty_album_id, "with_people": False},
                    {"album_id": None, "with_people": True},
                ]
                * 2,
            )

        trio.run(scenario)

    def test_album_people_reconcile_retries_a_changed_snapshot(self) -> None:
        album_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
        album = Album(album_id, "Family", "2026-08-25T12:00:00Z", 1)
        empty_people: tuple[Person, ...] = ()
        first = AlbumPeopleSweep(
            (album,), {album_id: [[asset()]]}, empty_people, [[]]
        )
        stable = AlbumPeopleSweep(
            (album,),
            {album_id: [[asset(OTHER_ID, "other.jpg")]]},
            empty_people,
            [[]],
        )

        async def scenario() -> None:
            lock = trio.Lock()
            catalog = AlbumPeopleCatalog(
                [asset(), asset(OTHER_ID, "other.jpg")], lock
            )
            client = AlbumPeopleClient([first, stable, stable], lock)
            session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)

            await reconcile_album_people(
                catalog,  # type: ignore[arg-type]
                client,  # type: ignore[arg-type]
                session,
                lock,
            )

            self.assertEqual(client.sweep, 3)
            self.assertEqual(len(catalog.published), 1)
            self.assertEqual(
                catalog.published[0]["album_memberships"],
                ((album_id, OTHER_ID),),
            )

        trio.run(scenario)

    def test_album_people_stability_ignores_unmounted_metadata_churn(self) -> None:
        album_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
        person_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
        first = AlbumPeopleSweep(
            (Album(album_id, "Family", "2026-08-25T12:00:00Z", 1),),
            {album_id: [[asset()]]},
            (Person(person_id, "Alice", False, "2026-08-25T12:00:00Z"),),
            [[replace(asset(), person_ids=(person_id,))]],
        )
        second = AlbumPeopleSweep(
            (Album(album_id, "Family", "2026-08-26T12:00:00Z", 2),),
            {album_id: [[asset()]]},
            (Person(person_id, "Alice", False, "2026-08-26T12:00:00Z"),),
            [[replace(asset(), person_ids=(person_id,))]],
        )

        async def scenario() -> None:
            lock = trio.Lock()
            catalog = AlbumPeopleCatalog([asset()], lock)
            client = AlbumPeopleClient([first, second], lock)
            session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)

            await reconcile_album_people(
                catalog,  # type: ignore[arg-type]
                client,  # type: ignore[arg-type]
                session,
                lock,
            )

            self.assertEqual(client.sweep, 2)
            self.assertEqual(catalog.published[0]["albums"], second.albums)
            self.assertEqual(catalog.published[0]["people"], second.people)

        trio.run(scenario)

    def test_album_people_reconcile_fails_closed_when_unstable(self) -> None:
        album_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
        album = Album(album_id, "Family", "2026-08-25T12:00:00Z", 1)

        def sweep(item: Asset) -> AlbumPeopleSweep:
            return AlbumPeopleSweep(
                (album,), {album_id: [[item]]}, (), [[]]
            )

        async def scenario() -> None:
            lock = trio.Lock()
            catalog = AlbumPeopleCatalog(
                [asset(), asset(OTHER_ID, "other.jpg")], lock
            )
            client = AlbumPeopleClient(
                [
                    sweep(asset()),
                    sweep(asset(OTHER_ID, "other.jpg")),
                    sweep(asset()),
                ],
                lock,
            )
            session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)

            with self.assertRaisesRegex(ImmichError, "did not stabilize"):
                await reconcile_album_people(
                    catalog,  # type: ignore[arg-type]
                    client,  # type: ignore[arg-type]
                    session,
                    lock,
                )

            self.assertEqual(client.sweep, 3)
            self.assertEqual(catalog.published, [])

        trio.run(scenario)

    def test_album_people_reconcile_publishes_nothing_after_client_error(self) -> None:
        album_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
        album = Album(album_id, "Family", "2026-08-25T12:00:00Z", 1)
        stable = AlbumPeopleSweep(
            (album,), {album_id: [[asset()]]}, (), [[]]
        )

        class FailingClient(AlbumPeopleClient):
            async def people(self) -> list[Person]:
                if self.sweep == 1:
                    raise ImmichError("invalid people response")
                return await super().people()

        async def scenario() -> None:
            lock = trio.Lock()
            catalog = AlbumPeopleCatalog([asset()], lock)
            client = FailingClient([stable, stable], lock)
            session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)

            with self.assertRaisesRegex(ImmichError, "invalid people"):
                await reconcile_album_people(
                    catalog,  # type: ignore[arg-type]
                    client,  # type: ignore[arg-type]
                    session,
                    lock,
                )

            self.assertEqual(catalog.published, [])

        trio.run(scenario)

    def test_complete_refresh_commits_the_supplied_trusted_profile(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                with Catalog(Path(directory) / "catalog.db") as catalog:
                    profile = trusted_profile()
                    client = FakeClient([[[asset()]], [[asset()]]])
                    session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)

                    await refresh_catalog(
                        catalog,
                        client,  # type: ignore[arg-type]
                        session,
                        trio.Lock(),
                        trusted_profile=profile,
                    )

                    self.assertEqual(catalog.trusted_profile(), profile)

        trio.run(scenario)

    def test_complete_refresh_rejects_trust_for_a_different_session(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                with Catalog(Path(directory) / "catalog.db") as catalog:
                    profile = trusted_profile(
                        owner_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                    )
                    client = FakeClient([])
                    session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)

                    with self.assertRaisesRegex(ValueError, "session"):
                        await refresh_catalog(
                            catalog,
                            client,  # type: ignore[arg-type]
                            session,
                            trio.Lock(),
                            trusted_profile=profile,
                        )

                    self.assertEqual(client.calls, 0)
                    self.assertIsNone(catalog.trusted_profile())

        trio.run(scenario)

    def test_incremental_refresh_uses_overlap_and_upserts_later_duplicates(self) -> None:
        third_id = "32345678-1234-4234-8234-123456789abc"

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                with Catalog(Path(directory) / "catalog.db") as catalog:
                    catalog.begin_refresh()
                    catalog.stage([asset(), asset(OTHER_ID, "other.jpg")])
                    catalog.finish_refresh(
                        high_water_ms=1_787_659_200_000,
                        page_count=2,
                    )
                    changed = replace(
                        asset(),
                        is_trashed=True,
                        updated_at="2026-08-25T12:05:00Z",
                    )
                    later = replace(changed, original_name="latest.jpg")
                    client = IncrementalClient(
                        [[changed], [later, asset(third_id, "new.jpg")]]
                    )
                    session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)

                    stats = await refresh_catalog_incremental(
                        catalog,
                        client,  # type: ignore[arg-type]
                        session,
                        trio.Lock(),
                        refresh_seconds=300,
                    )

                    self.assertEqual(stats.total, 3)
                    self.assertIsNotNone(catalog.by_id(OTHER_ID))
                    self.assertIsNotNone(catalog.by_id(third_id))
                    updated = catalog.by_id(ASSET_ID)
                    self.assertEqual(updated and updated.asset.original_name, "latest.jpg")
                    self.assertTrue(updated and updated.asset.is_trashed)
                    self.assertEqual(catalog.refresh_state(), (1_787_659_500_000, 2))
                    self.assertEqual(
                        client.calls,
                        [
                            {
                                "updated_after_ms": 1_787_658_600_000,
                                "allow_duplicate_ids": True,
                                "page_limit": 2,
                            }
                        ],
                    )

        trio.run(scenario)

    def test_incremental_refresh_applies_same_timestamp_restore(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                with Catalog(Path(directory) / "catalog.db") as catalog:
                    catalog.begin_refresh()
                    catalog.stage([replace(asset(), is_trashed=True)])
                    catalog.finish_refresh(
                        high_water_ms=1_787_659_200_000,
                        page_count=1,
                    )
                    client = IncrementalClient([[asset()]])
                    session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)

                    await refresh_catalog_incremental(
                        catalog,
                        client,  # type: ignore[arg-type]
                        session,
                        trio.Lock(),
                        refresh_seconds=300,
                    )

                    self.assertEqual(catalog.stats().visible, 1)
                    self.assertEqual(catalog.refresh_state(), (1_787_659_200_000, 1))

        trio.run(scenario)

    def test_refresh_commits_after_two_matching_complete_sweeps(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                with Catalog(Path(directory) / "catalog.db") as catalog:
                    session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)
                    client = FakeClient([[[asset()]], [[asset()]]])
                    stats = await refresh_catalog(
                        catalog,
                        client,  # type: ignore[arg-type]
                        session,
                        trio.Lock(),
                    )
                    self.assertEqual(stats.visible, 1)
                    self.assertEqual(catalog.list_visible()[0].asset.id, ASSET_ID)
                    self.assertEqual(client.calls, 2)
                    self.assertEqual(catalog.refresh_state(), (1_787_659_200_000, 1))

        trio.run(scenario)

    def test_refresh_retries_one_changed_sweep_then_commits_a_stable_pair(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                with Catalog(Path(directory) / "catalog.db") as catalog:
                    session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)
                    stable = [[asset(), asset(OTHER_ID, "other.jpg")]]
                    client = FakeClient([[[asset()]], stable, stable])

                    stats = await refresh_catalog(
                        catalog,
                        client,  # type: ignore[arg-type]
                        session,
                        trio.Lock(),
                    )

                    self.assertEqual(stats.visible, 2)
                    self.assertEqual(client.calls, 3)

        trio.run(scenario)

    def test_refresh_fails_closed_when_three_sweeps_do_not_stabilize(self) -> None:
        third_id = "32345678-1234-4234-8234-123456789abc"

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                with Catalog(Path(directory) / "catalog.db") as catalog:
                    catalog.begin_refresh()
                    catalog.stage([asset()])
                    catalog.finish_refresh(
                        high_water_ms=1_787_659_200_000,
                        page_count=1,
                    )
                    session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)
                    profile = trusted_profile()
                    client = FakeClient(
                        [
                            [[asset(OTHER_ID, "other.jpg")]],
                            [[asset(third_id, "third.jpg")]],
                            [[asset(OTHER_ID, "other.jpg")]],
                        ]
                    )

                    with self.assertRaisesRegex(ImmichError, "did not stabilize"):
                        await refresh_catalog(
                            catalog,
                            client,  # type: ignore[arg-type]
                            session,
                            trio.Lock(),
                            trusted_profile=profile,
                        )

                    self.assertEqual(
                        [entry.asset.id for entry in catalog.list_visible()], [ASSET_ID]
                    )
                    self.assertIsNone(catalog.trusted_profile())

        trio.run(scenario)

    def test_duplicate_ids_break_sweep_consecutiveness(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                with Catalog(Path(directory) / "catalog.db") as catalog:
                    catalog.begin_refresh()
                    catalog.stage([asset(OTHER_ID, "other.jpg")])
                    catalog.finish_refresh(
                        high_water_ms=1_787_659_200_000,
                        page_count=1,
                    )
                    session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)
                    client = FakeClient(
                        [
                            [[asset()]],
                            [[asset()], [asset()]],
                            [[asset()]],
                        ]
                    )

                    with self.assertRaisesRegex(ImmichError, "did not stabilize"):
                        await refresh_catalog(
                            catalog,
                            client,  # type: ignore[arg-type]
                            session,
                            trio.Lock(),
                        )

                    self.assertEqual(
                        [entry.asset.id for entry in catalog.list_visible()], [OTHER_ID]
                    )

        trio.run(scenario)
