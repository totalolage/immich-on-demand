import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from immich_on_demand.settings import Settings, load, save


class SettingsTest(unittest.TestCase):
    def test_round_trip_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            expected = Settings("https://photos.example.test", Path(directory) / "Photos")
            save(expected, path)

            self.assertEqual(load(path), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn("api", json.loads(path.read_text()))

    def test_rejects_an_unsafe_server_url(self) -> None:
        for server_url in (
            "http://photos.example.test",
            "https://user:password@photos.example.test",
            "https://@photos.example.test",
            "https://photos.example.test/immich",
            "https://photos.example.test?key=value",
        ):
            with self.subTest(server_url=server_url), self.assertRaises(ValueError):
                Settings(server_url, Path("/tmp/Photos"))
