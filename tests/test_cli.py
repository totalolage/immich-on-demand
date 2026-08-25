import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import trio

from immich_on_demand.cli import _auth_check, _print_result, main
from immich_on_demand.immich import UPLOAD_PERMISSIONS
from immich_on_demand.settings import Settings


ASSET_ID = "12345678-1234-4234-8234-123456789abc"


class CliTest(unittest.TestCase):
    def test_mutation_auth_check_uses_the_upload_only_secret_and_scope(self) -> None:
        seen: list[object] = []

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

            async def validate(self, permissions: frozenset[str]):
                seen.append(permissions)
                return type("Session", (), {"version": "3.0.3"})()

        configured = Settings("https://photos.example.test", Path("/Photos"))
        with (
            patch("immich_on_demand.cli.load_api_key", return_value="secret") as load_key,
            patch("immich_on_demand.cli.ImmichClient", return_value=Client()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(trio.run(_auth_check, configured, True), 0)

        load_key.assert_called_once_with(configured, "mutation")
        self.assertEqual(seen, [UPLOAD_PERMISSIONS])

    def test_version(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["--version"])

        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(output.getvalue(), "0.1.0\n")

    def test_configure_writes_only_non_secret_settings(self) -> None:
        import json

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
                        "--enable-remote-delete",
                    ]
                )

            self.assertEqual(result, 0)
            value = json.loads(config.read_text())
            self.assertEqual(value["server_url"], "https://photos.example.test")
            self.assertTrue(value["remote_delete"])
            self.assertNotIn("key", value)

    def test_prints_flat_results_in_key_order(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_result({"visible": 2, "total": 3})
        self.assertEqual(output.getvalue(), "total=3 visible=2\n")

    def test_control_commands_route_without_catalog_settings_or_secrets(self) -> None:
        cases = (
            (["status"], "status", {}),
            (["refresh"], "refresh", {}),
            (["evict"], "evict", {}),
            (["evict", "--asset", ASSET_ID], "evict", {"asset": ASSET_ID}),
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            for arguments, method, params in cases:
                with self.subTest(command=arguments):
                    request = AsyncMock(return_value={"z": 2, "a": 1})
                    output = io.StringIO()
                    with (
                        patch("immich_on_demand.cli.send_request", request),
                        patch("immich_on_demand.cli.runtime_path", return_value=runtime),
                        patch("immich_on_demand.cli.secrets.randbits", return_value=0),
                        patch("immich_on_demand.cli.load") as load,
                        patch("immich_on_demand.cli.load_api_key") as load_api_key,
                        patch("immich_on_demand.cli.Catalog", create=True) as catalog,
                        contextlib.redirect_stdout(output),
                    ):
                        self.assertEqual(main(arguments), 0)

                    request.assert_awaited_once_with(
                        runtime / "control.sock", 1, method, params
                    )
                    load.assert_not_called()
                    load_api_key.assert_not_called()
                    catalog.assert_not_called()
                    self.assertEqual(output.getvalue(), "a=1 z=2\n")
