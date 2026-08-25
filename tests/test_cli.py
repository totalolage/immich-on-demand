import contextlib
import io
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import trio

from immich_on_demand.cli import _auth_check, _print_result, main
from immich_on_demand.immich import ServerSession
from immich_on_demand.service import run_service
from immich_on_demand.settings import Settings


ASSET_ID = "12345678-1234-4234-8234-123456789abc"


class CliTest(unittest.TestCase):
    def test_mutation_auth_check_uses_the_upload_only_secret_and_scope(self) -> None:
        configured = Settings("https://photos.example.test", Path("/Photos"))
        with (
            patch("immich_on_demand.cli.load_api_key", return_value="secret") as load_key,
            patch(
                "immich_on_demand.cli.validate_api_key",
                new=AsyncMock(
                    return_value=ServerSession(
                        "87654321-4321-4321-8321-cba987654321",
                        "3.0.3",
                        frozenset(),
                        True,
                    )
                ),
            ) as validate,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(trio.run(_auth_check, configured, True), 0)

        load_key.assert_called_once_with(configured, "mutation")
        validate.assert_awaited_once_with(configured, "mutation", "secret")

    def test_version(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["--version"])

        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(output.getvalue(), "1.0.0\n")

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
                        "--cache-max-gib",
                        "20",
                        "--cache-max-age-days",
                        "7",
                        "--minimum-free-gib",
                        "3",
                        "--enable-remote-delete",
                    ]
                )

            self.assertEqual(result, 0)
            value = json.loads(config.read_text())
            self.assertEqual(value["server_url"], "https://photos.example.test")
            self.assertEqual(value["cache_max_bytes"], 20 * 1024**3)
            self.assertEqual(value["cache_max_age_seconds"], 7 * 24 * 60 * 60)
            self.assertEqual(value["minimum_free_bytes"], 3 * 1024**3)
            self.assertTrue(value["remote_delete"])
            self.assertNotIn("key", value)

    def test_configure_rejects_non_positive_cache_limits(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
            main(
                [
                    "configure",
                    "--server",
                    "https://photos.example.test",
                    "--mount",
                    "/Photos",
                    "--cache-max-gib",
                    "0",
                ]
            )
        self.assertEqual(exit.exception.code, 2)

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

    def test_status_before_service_start_is_a_concise_unavailable_error(self) -> None:
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "secret-runtime"
            with (
                patch("immich_on_demand.cli.runtime_path", return_value=runtime),
                contextlib.redirect_stderr(error),
            ):
                self.assertEqual(main(["status"]), 1)

        self.assertEqual(
            error.getvalue(), "immich-on-demand: control service is unavailable\n"
        )

    def test_mount_routes_through_the_service(self) -> None:
        configured = Settings("https://photos.example.test", Path("/Photos"))
        events: list[str] = []
        with (
            patch("immich_on_demand.cli.load", return_value=configured),
            patch(
                "immich_on_demand.cli.logging.basicConfig",
                side_effect=lambda **kwargs: events.append("logging"),
            ) as configure_logging,
            patch(
                "immich_on_demand.cli.trio.run",
                side_effect=lambda *args: events.append("service"),
            ) as run,
        ):
            self.assertEqual(main(["mount"]), 0)
        configure_logging.assert_called_once_with(level=logging.INFO)
        run.assert_called_once_with(run_service, configured)
        self.assertEqual(events, ["logging", "service"])

    def test_network_failure_is_a_concise_cli_error(self) -> None:
        configured = Settings("https://photos.example.test", Path("/Photos"))
        error = io.StringIO()
        with (
            patch("immich_on_demand.cli.load", return_value=configured),
            patch(
                "immich_on_demand.cli.trio.run",
                side_effect=httpx.ConnectError("network unavailable"),
            ),
            contextlib.redirect_stderr(error),
        ):
            self.assertEqual(main(["mount"]), 1)
        self.assertEqual(error.getvalue(), "immich-on-demand: network unavailable\n")
