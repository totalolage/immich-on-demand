from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

import trio

from immich_on_demand.app import (
    refresh_catalog,
    refresh_catalog_incremental,
)
from immich_on_demand.catalog import Catalog
from immich_on_demand.immich import ImmichError, ServerSession
from immich_on_demand.model import Asset
from test_catalog import ASSET_ID, OTHER_ID, OWNER_ID, asset, trusted_profile


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


class AppTest(unittest.TestCase):
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
                    self.assertIsNotNone(catalog.by_name("other.jpg"))
                    self.assertIsNotNone(catalog.by_name("new.jpg"))
                    updated = catalog.by_name("photo.jpg")
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
