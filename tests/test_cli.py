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
from immich_on_demand.profiles import Profile, ProfileBusyError, ProfileError
from immich_on_demand.service import run_service
from immich_on_demand.settings import Settings


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
NEXT_ID = "87654321-4321-4321-8321-cba987654321"


def profile(root: Path, profile_id: str = "home") -> Profile:
    return Profile(
        profile_id,
        root / "config",
        root / "state",
        root / "data",
        root / "cache",
        root / "runtime",
    )


class CliTest(unittest.TestCase):
    def test_mutation_auth_check_uses_the_upload_only_secret_and_scope(self) -> None:
        selected = profile(Path("/profile"))
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
            self.assertEqual(trio.run(_auth_check, selected, configured, True), 0)

        load_key.assert_called_once_with(
            configured, "mutation", profile_id=selected.id
        )
        validate.assert_awaited_once_with(configured, "mutation", "secret")

    def test_version(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["--version"])

        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(output.getvalue(), "2.0.0.dev0\n")

    def test_profile_failures_use_sysexits_statuses(self) -> None:
        for error, expected in (
            (ProfileBusyError("busy"), 75),
            (ProfileError("invalid"), 78),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch("immich_on_demand.cli.select_profile", side_effect=error),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    main(["--profile", "home", "status"]), expected
                )

    def test_strict_profile_config_failures_use_nonrestartable_status(self) -> None:
        selected = profile(Path("/profile"))
        with (
            patch("immich_on_demand.cli.select_profile", return_value=selected),
            patch("immich_on_demand.cli.load", side_effect=RuntimeError("unsafe")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["--profile", "home", "auth-check"]), 78)

        configured = Settings("https://photos.example.test", Path("/Photos"))
        with (
            patch("immich_on_demand.cli.select_profile", return_value=selected),
            patch(
                "immich_on_demand.cli.manage_profile",
                return_value=contextlib.nullcontext(selected),
            ),
            patch("immich_on_demand.cli.Settings", return_value=configured),
            patch("immich_on_demand.cli.save", side_effect=RuntimeError("unsafe")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                main(
                    [
                        "--profile",
                        "home",
                        "configure",
                        "--server",
                        configured.server_url,
                        "--mount",
                        str(configured.mount_path),
                    ]
                ),
                78,
            )

    def test_configure_writes_only_non_secret_settings(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = profile(root)
            selected.config.mkdir(mode=0o700)
            config = selected.config / "config.json"
            mount = root / "Photos"
            with (
                patch("immich_on_demand.cli.select_profile", return_value=selected),
                patch(
                    "immich_on_demand.cli.manage_profile",
                    return_value=contextlib.nullcontext(selected),
                ) as manage,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = main(
                    [
                        "--profile",
                        "home",
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
            manage.assert_called_once_with(selected, mount.resolve())
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
                    "--profile",
                    "home",
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
            (["pin", "--asset", ASSET_ID], "pin", {"asset": ASSET_ID, "pinned": True}),
            (
                ["unpin", "--asset", ASSET_ID],
                "pin",
                {"asset": ASSET_ID, "pinned": False},
            ),
            (["pin-status", "--asset", ASSET_ID], "pin", {"asset": ASSET_ID}),
            (
                ["restore", "--asset", ASSET_ID.upper()],
                "restore",
                {"asset": ASSET_ID},
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            selected = profile(Path(directory))
            for arguments, method, params in cases:
                with self.subTest(command=arguments):
                    request = AsyncMock(return_value={"z": 2, "a": 1})
                    output = io.StringIO()
                    with (
                        patch("immich_on_demand.cli.send_request", request),
                        patch(
                            "immich_on_demand.cli.select_profile",
                            return_value=selected,
                        ),
                        patch("immich_on_demand.cli.secrets.randbits", return_value=0),
                        patch("immich_on_demand.cli.load") as load,
                        patch("immich_on_demand.cli.load_api_key") as load_api_key,
                        patch("immich_on_demand.cli.Catalog", create=True) as catalog,
                        contextlib.redirect_stdout(output),
                    ):
                        self.assertEqual(main(["--profile", "home", *arguments]), 0)

                    request.assert_awaited_once_with(
                        selected.runtime / "control.sock", 1, method, params
                    )
                    load.assert_not_called()
                    load_api_key.assert_not_called()
                    catalog.assert_not_called()
                    self.assertEqual(output.getvalue(), "a=1 z=2\n")

    def test_upload_mutations_route_canonical_ids_and_exact_confirmation_name(self) -> None:
        cases = (
            (
                ["retry-upload", "--id", ASSET_ID.upper()],
                "retry-upload",
                {"id": ASSET_ID},
            ),
            (
                [
                    "cancel-upload",
                    "--id",
                    ASSET_ID.upper(),
                    "--revision",
                    "7",
                    "--confirm-name",
                    "Test image.jpg",
                ],
                "cancel-upload",
                {
                    "id": ASSET_ID,
                    "revision": 7,
                    "confirm_name": "Test image.jpg",
                },
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            selected = profile(Path(directory))
            for arguments, method, params in cases:
                with self.subTest(command=arguments):
                    request = AsyncMock(return_value={"scheduled": True, "id": ASSET_ID})
                    with (
                        patch("immich_on_demand.cli.send_request", request),
                        patch(
                            "immich_on_demand.cli.select_profile",
                            return_value=selected,
                        ),
                        patch("immich_on_demand.cli.secrets.randbits", return_value=0),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        self.assertEqual(main(["--profile", "home", *arguments]), 0)
                    request.assert_awaited_once_with(
                        selected.runtime / "control.sock", 1, method, params
                    )

    def test_uploads_prints_valid_pages_as_json_lines(self) -> None:
        pages = (
            {
                "items": [
                    {
                        "id": ASSET_ID,
                        "name": "First image.jpg",
                        "state": "blocked",
                        "size": 123,
                        "error": "upload-unavailable",
                        "revision": 1,
                    }
                ],
                "next": NEXT_ID,
            },
            {
                "items": [
                    {
                        "id": NEXT_ID,
                        "name": "second.png",
                        "state": "pending",
                        "size": None,
                        "error": None,
                        "revision": 2,
                    }
                ],
                "next": None,
            },
        )
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            selected = profile(Path(directory))
            request = AsyncMock(side_effect=pages)
            with (
                patch("immich_on_demand.cli.send_request", request),
                patch(
                    "immich_on_demand.cli.select_profile", return_value=selected
                ),
                patch("immich_on_demand.cli.secrets.randbits", return_value=0),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(["--profile", "home", "uploads"]), 0)

        self.assertEqual(
            request.await_args_list,
            [
                unittest.mock.call(
                    selected.runtime / "control.sock",
                    1,
                    "uploads",
                    {"after": None, "limit": 32},
                ),
                unittest.mock.call(
                    selected.runtime / "control.sock",
                    1,
                    "uploads",
                    {"after": NEXT_ID, "limit": 32},
                ),
            ],
        )
        self.assertEqual(
            output.getvalue(),
            '{"error": "upload-unavailable", "id": "12345678-1234-4234-8234-123456789abc", "name": "First image.jpg", "revision": 1, "size": 123, "state": "blocked"}\n'
            '{"error": null, "id": "87654321-4321-4321-8321-cba987654321", "name": "second.png", "revision": 2, "size": null, "state": "pending"}\n',
        )

    def test_uploads_rejects_a_malformed_page_with_a_fixed_error(self) -> None:
        error = io.StringIO()
        request = AsyncMock(return_value={"items": [], "next": "not-a-uuid"})
        selected = profile(Path("/profile"))
        with (
            patch("immich_on_demand.cli.send_request", request),
            patch("immich_on_demand.cli.select_profile", return_value=selected),
            contextlib.redirect_stderr(error),
        ):
            self.assertEqual(main(["--profile", "home", "uploads"]), 1)
        self.assertEqual(
            error.getvalue(),
            "immich-on-demand: control returned an invalid uploads page\n",
        )

    def test_upload_mutations_require_an_id_and_cancel_confirmation(self) -> None:
        for arguments in (
            ["retry-upload"],
            ["retry-upload", "--id", "not-a-uuid"],
            ["cancel-upload", "--id", ASSET_ID],
            [
                "cancel-upload",
                "--id",
                ASSET_ID,
                "--revision",
                "0",
                "--confirm-name",
                "",
            ],
            [
                "cancel-upload",
                "--id",
                ASSET_ID,
                "--revision",
                "-1",
                "--confirm-name",
                "Test image.jpg",
            ],
        ):
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exit,
            ):
                main(["--profile", "home", *arguments])
            self.assertEqual(exit.exception.code, 2)

    def test_restore_requires_an_asset_uuid(self) -> None:
        for arguments in (["restore"], ["restore", "--asset", "not-a-uuid"]):
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exit,
            ):
                main(["--profile", "home", *arguments])
            self.assertEqual(exit.exception.code, 2)

    def test_status_before_service_start_is_a_concise_unavailable_error(self) -> None:
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            selected = profile(Path(directory) / "secret-runtime")
            with (
                patch(
                    "immich_on_demand.cli.select_profile", return_value=selected
                ),
                contextlib.redirect_stderr(error),
            ):
                self.assertEqual(main(["--profile", "home", "status"]), 1)

        self.assertEqual(
            error.getvalue(), "immich-on-demand: control service is unavailable\n"
        )

    def test_mount_routes_through_the_service(self) -> None:
        selected = profile(Path("/profile"))
        events: list[str] = []
        with (
            patch("immich_on_demand.cli.select_profile", return_value=selected) as select,
            patch("immich_on_demand.cli.load") as load,
            patch(
                "immich_on_demand.cli.logging.basicConfig",
                side_effect=lambda **kwargs: events.append("logging"),
            ) as configure_logging,
            patch(
                "immich_on_demand.cli.trio.run",
                side_effect=lambda *args: events.append("service"),
            ) as run,
        ):
            self.assertEqual(main(["--profile", "home", "mount"]), 0)
        select.assert_called_once_with("home")
        load.assert_not_called()
        configure_logging.assert_called_once_with(level=logging.INFO)
        run.assert_called_once_with(run_service, selected)
        self.assertEqual(events, ["logging", "service"])

    def test_network_failure_is_a_concise_cli_error(self) -> None:
        selected = profile(Path("/profile"))
        error = io.StringIO()
        with (
            patch("immich_on_demand.cli.select_profile", return_value=selected),
            patch(
                "immich_on_demand.cli.trio.run",
                side_effect=httpx.ConnectError("network unavailable"),
            ),
            contextlib.redirect_stderr(error),
        ):
            self.assertEqual(main(["--profile", "home", "mount"]), 1)
        self.assertEqual(error.getvalue(), "immich-on-demand: network unavailable\n")
