from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, call, patch

import trio

from immich_on_demand.desktop import run_action


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
                    await run_action("refresh"),
                    {"scheduled": True},
                )
                uri = "file:///home/user/Immich/photo.jpg"
                self.assertEqual(
                    await run_action("evict", uri),
                    {"scheduled": True},
                )

            self.assertEqual(
                request.await_args_list,
                [
                    call(runtime / "control.sock", 1, "refresh", {}),
                    call(
                        runtime / "control.sock",
                        1,
                        "evict",
                        {"uri": uri},
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
                ("unknown", None),
            ):
                with self.subTest(action=action, uri=uri), self.assertRaises(ValueError):
                    await run_action(action, uri)

        trio.run(scenario)


if __name__ == "__main__":
    unittest.main()
