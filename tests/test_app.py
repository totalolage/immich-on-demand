from pathlib import Path
import tempfile
import unittest

import trio

from immich_on_demand.app import refresh_catalog
from immich_on_demand.catalog import Catalog
from immich_on_demand.immich import ImmichError, ServerSession
from immich_on_demand.model import Asset
from test_catalog import ASSET_ID, OTHER_ID, OWNER_ID, asset


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


class AppTest(unittest.TestCase):
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
                    catalog.finish_refresh()
                    session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)
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
                        )

                    self.assertEqual(
                        [entry.asset.id for entry in catalog.list_visible()], [ASSET_ID]
                    )

        trio.run(scenario)

    def test_duplicate_ids_break_sweep_consecutiveness(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                with Catalog(Path(directory) / "catalog.db") as catalog:
                    catalog.begin_refresh()
                    catalog.stage([asset(OTHER_ID, "other.jpg")])
                    catalog.finish_refresh()
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
