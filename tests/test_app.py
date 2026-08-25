from pathlib import Path
import tempfile
import unittest

import trio

from immich_on_demand.app import refresh_catalog
from immich_on_demand.catalog import Catalog
from immich_on_demand.immich import ServerSession
from test_catalog import ASSET_ID, OWNER_ID, asset


class FakeClient:
    async def asset_pages(self, owner_id: str):
        assert owner_id == OWNER_ID
        yield [asset()]


class AppTest(unittest.TestCase):
    def test_refresh_commits_after_last_page(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                with Catalog(Path(directory) / "catalog.db") as catalog:
                    session = ServerSession(OWNER_ID, "3.0.3", frozenset(), True)
                    stats = await refresh_catalog(catalog, FakeClient(), session)  # type: ignore[arg-type]
                    self.assertEqual(stats.visible, 1)
                    self.assertEqual(catalog.list_visible()[0].asset.id, ASSET_ID)

        trio.run(scenario)
