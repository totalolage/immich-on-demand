from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, call, patch

import trio

from immich_on_demand.desktop import run_action


ASSET_ID = "12345678-1234-4234-8234-123456789abc"


class DesktopTest(unittest.TestCase):
    def test_actions_use_only_the_bounded_local_control_client(self) -> None:
        async def scenario(runtime: Path) -> None:
            request = AsyncMock(return_value={"scheduled": True})
            with (
                patch("immich_on_demand.desktop.send_request", request),
                patch("immich_on_demand.desktop.runtime_path", return_value=runtime),
                patch("immich_on_demand.desktop.secrets.randbits", return_value=0),
            ):
                self.assertEqual(
                    await run_action("status"),
                    {"scheduled": True},
                )
                self.assertEqual(
                    await run_action("refresh"),
                    {"scheduled": True},
                )
                uri = "file:///home/user/Immich/photo.jpg"
                self.assertEqual(
                    await run_action("evict", uri),
                    {"scheduled": True},
                )
                self.assertEqual(
                    await run_action("describe", [uri]),
                    {"scheduled": True},
                )
                self.assertEqual(await run_action("pin", uri), {"scheduled": True})
                self.assertEqual(await run_action("unpin", uri), {"scheduled": True})
                self.assertEqual(
                    await run_action("restore", ASSET_ID.upper()),
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
                ],
            )

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_evict_requires_a_file_uri_and_refresh_rejects_one(self) -> None:
        async def scenario() -> None:
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
                ("unknown", None),
            ):
                with self.subTest(action=action, uri=uri), self.assertRaises(ValueError):
                    await run_action(action, uri)

        trio.run(scenario)

    def test_describe_request_stays_below_the_desktop_batch_ceiling(self) -> None:
        async def scenario() -> None:
            with self.assertRaises(ValueError):
                # The params fit below 48 KiB, but the complete control frame does not.
                await run_action("describe", ["file:///" + "a" * 49_074])

        trio.run(scenario)


if __name__ == "__main__":
    unittest.main()
