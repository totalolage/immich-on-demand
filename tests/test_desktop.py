from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, call, patch

import trio

from immich_on_demand.desktop import run_action
from immich_on_demand.profiles import Profile


ASSET_ID = "12345678-1234-4234-8234-123456789abc"


class DesktopTest(unittest.TestCase):
    def test_actions_use_only_the_bounded_local_control_client(self) -> None:
        async def scenario(runtime: Path) -> None:
            profile = Profile("home", runtime, runtime, runtime, runtime, runtime)
            request = AsyncMock(return_value={"scheduled": True})
            with (
                patch("immich_on_demand.desktop.send_request", request),
                patch("immich_on_demand.desktop.secrets.randbits", return_value=0),
            ):
                self.assertEqual(
                    await run_action(profile, "status"),
                    {"scheduled": True},
                )
                self.assertEqual(
                    await run_action(profile, "refresh"),
                    {"scheduled": True},
                )
                uri = "file:///home/user/Immich/photo.jpg"
                self.assertEqual(
                    await run_action(profile, "evict", uri),
                    {"scheduled": True},
                )
                self.assertEqual(
                    await run_action(profile, "describe", [uri]),
                    {"scheduled": True},
                )
                self.assertEqual(await run_action(profile, "pin", uri), {"scheduled": True})
                self.assertEqual(await run_action(profile, "unpin", uri), {"scheduled": True})
                self.assertEqual(
                    await run_action(profile, "restore", ASSET_ID.upper()),
                    {"scheduled": True},
                )
                self.assertEqual(
                    await run_action(profile, "uploads"),
                    {"scheduled": True},
                )
                self.assertEqual(
                    await run_action(profile, "uploads", ASSET_ID.upper()),
                    {"scheduled": True},
                )
                self.assertEqual(
                    await run_action(profile, "retry-upload", ASSET_ID.upper()),
                    {"scheduled": True},
                )
                self.assertEqual(
                    await run_action(
                        profile, "cancel-upload", ASSET_ID.upper(), 7, "Test image.jpg"
                    ),
                    {"scheduled": True},
                )

            self.assertEqual(
                request.await_args_list,
                [
                    call(runtime / "control.sock", 1, "status", {}),
                    call(runtime / "control.sock", 1, "refresh", {}),
                    call(
                        runtime / "control.sock",
                        1,
                        "evict",
                        {"uri": uri},
                    ),
                    call(
                        runtime / "control.sock",
                        1,
                        "describe",
                        {"uris": [uri]},
                    ),
                    call(
                        runtime / "control.sock",
                        1,
                        "pin",
                        {"uri": uri, "pinned": True},
                    ),
                    call(
                        runtime / "control.sock",
                        1,
                        "pin",
                        {"uri": uri, "pinned": False},
                    ),
                    call(
                        runtime / "control.sock",
                        1,
                        "restore",
                        {"asset": ASSET_ID},
                    ),
                    call(
                        runtime / "control.sock",
                        1,
                        "uploads",
                        {"after": None, "limit": 32},
                    ),
                    call(
                        runtime / "control.sock",
                        1,
                        "uploads",
                        {"after": ASSET_ID, "limit": 32},
                    ),
                    call(
                        runtime / "control.sock",
                        1,
                        "retry-upload",
                        {"id": ASSET_ID},
                    ),
                    call(
                        runtime / "control.sock",
                        1,
                        "cancel-upload",
                        {
                            "id": ASSET_ID,
                            "revision": 7,
                            "confirm_name": "Test image.jpg",
                        },
                    ),
                ],
            )

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_evict_requires_a_file_uri_and_refresh_rejects_one(self) -> None:
        async def scenario() -> None:
            profile = Profile("home", Path("/c"), Path("/s"), Path("/d"), Path("/k"), Path("/r"))
            for action, uri in (
                ("evict", None),
                ("evict", "https://photos.example.test/photo.jpg"),
                ("refresh", "file:///Photos/photo.jpg"),
                ("describe", []),
                ("describe", ["https://photos.example.test/photo.jpg"]),
                ("describe", ["file:///Photos/photo.jpg"] * 65),
                ("pin", None),
                ("unpin", "https://photos.example.test/photo.jpg"),
                ("restore", None),
                ("restore", "not-a-uuid"),
                ("restore", "file:///Photos/photo.jpg"),
                ("uploads", "not-a-uuid"),
                ("retry-upload", None),
                ("retry-upload", "not-a-uuid"),
                ("unknown", None),
            ):
                with self.subTest(action=action, uri=uri), self.assertRaises(ValueError):
                    await run_action(profile, action, uri)

        trio.run(scenario)

    def test_cancel_upload_requires_an_exact_nonempty_confirmation_name(self) -> None:
        async def scenario() -> None:
            profile = Profile("home", Path("/c"), Path("/s"), Path("/d"), Path("/k"), Path("/r"))
            for upload_id, revision, confirmation in (
                (ASSET_ID, None, "Test image.jpg"),
                (ASSET_ID, -1, "Test image.jpg"),
                (ASSET_ID, 0, None),
                (ASSET_ID, 0, ""),
                ("not-a-uuid", 0, "Test image.jpg"),
            ):
                with self.subTest(
                    upload_id=upload_id,
                    revision=revision,
                    confirmation=confirmation,
                ), self.assertRaises(ValueError):
                    await run_action(
                        profile, "cancel-upload", upload_id, revision, confirmation
                    )

        trio.run(scenario)

    def test_describe_request_stays_below_the_desktop_batch_ceiling(self) -> None:
        async def scenario() -> None:
            profile = Profile("home", Path("/c"), Path("/s"), Path("/d"), Path("/k"), Path("/r"))
            with self.assertRaises(ValueError):
                # The params fit below 48 KiB, but the complete control frame does not.
                await run_action(profile, "describe", ["file:///" + "a" * 49_074])

        trio.run(scenario)


if __name__ == "__main__":
    unittest.main()
