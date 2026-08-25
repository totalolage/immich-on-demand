import contextlib
import io
import unittest

from immich_on_demand.catalog import CatalogStats
from immich_on_demand.cli import _print_stats, main


class CliTest(unittest.TestCase):
    def test_version(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["--version"])

        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(output.getvalue(), "0.1.0\n")

    def test_configure_writes_only_non_secret_settings(self) -> None:
        import json
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            mount = Path(directory) / "Photos"
            with contextlib.redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "--config",
                        str(config),
                        "configure",
                        "--server",
                        "https://photos.example.test",
                        "--mount",
                        str(mount),
                    ]
                )

            self.assertEqual(result, 0)
            value = json.loads(config.read_text())
            self.assertEqual(value["server_url"], "https://photos.example.test")
            self.assertNotIn("key", value)

    def test_prints_slotted_catalog_stats(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_stats(CatalogStats(3, 2, 1, 0, 0, 0))
        self.assertEqual(
            output.getvalue(),
            "total=3 visible=2 missing_size=1 trashed=0 hidden=0 offline=0\n",
        )
