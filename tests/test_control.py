import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

import trio

from immich_on_demand.control import (
    ControlError,
    MAX_MESSAGE_BYTES,
    send_request,
    serve_control,
)


async def _raw_request(path: Path, data: bytes) -> tuple[dict[str, object], bytes]:
    stream = await trio.open_unix_socket(path)
    async with stream:
        await stream.send_all(data)
        response = bytearray()
        while b"\n" not in response:
            response.extend(await stream.receive_some(4096))
        try:
            closed = await stream.receive_some(1)
        except trio.BrokenResourceError:
            closed = b""
    return json.loads(bytes(response)), closed


class ControlTests(unittest.TestCase):
    def test_upload_methods_reach_registered_handlers(self) -> None:
        async def handler(params: dict[str, object]) -> object:
            return params

        async def scenario(path: Path) -> None:
            async with trio.open_nursery() as nursery:
                await nursery.start(
                    serve_control,
                    path,
                    {
                        "uploads": handler,
                        "retry-upload": handler,
                        "cancel-upload": handler,
                    },
                )
                for request_id, method in enumerate(
                    ("uploads", "retry-upload", "cancel-upload"), start=1
                ):
                    with self.subTest(method=method):
                        self.assertEqual(
                            await send_request(
                                path, request_id, method, {"request": method}
                            ),
                            {"request": method},
                        )
                nursery.cancel_scope.cancel()

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory) / "runtime" / "control.sock")

    def test_reports_an_absent_or_refused_service_without_leaking_its_path(self) -> None:
        async def scenario(root: Path) -> None:
            absent = root / "absent-secret" / "control.sock"
            stale = root / "refused-secret.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(stale))
            listener.close()

            for path in (absent, stale):
                with self.subTest(path=path.name):
                    with self.assertRaisesRegex(
                        ControlError, "^control service is unavailable$"
                    ) as unavailable:
                        await send_request(path, 1, "status", {})
                    self.assertNotIn(str(path), str(unavailable.exception))

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_round_trip_is_private_and_one_shot(self) -> None:
        async def scenario(path: Path) -> None:
            seen: list[dict[str, object]] = []

            async def status(params: dict[str, object]) -> object:
                seen.append(params)
                return {"mounted": True}

            async def describe(params: dict[str, object]) -> object:
                return {"items": []}

            async def pin(params: dict[str, object]) -> object:
                return {"pinned": params["pinned"]}

            async with trio.open_nursery() as nursery:
                await nursery.start(
                    serve_control,
                    path,
                    {"status": status, "describe": describe, "pin": pin},
                )
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(
                    await send_request(path, 7, "status", {"path": "/Photos"}),
                    {"mounted": True},
                )
                response, closed = await _raw_request(
                    path, b'{"id":8,"method":"status","params":{}}\n'
                )
                self.assertEqual(response, {"id": 8, "result": {"mounted": True}})
                self.assertEqual(closed, b"")
                self.assertEqual(
                    await send_request(path, 9, "describe", {"uris": []}),
                    {"items": []},
                )
                self.assertEqual(
                    await send_request(path, 10, "pin", {"pinned": True}),
                    {"pinned": True},
                )
                with self.assertRaisesRegex(ControlError, "^method unavailable$"):
                    await send_request(
                        path,
                        11,
                        "restore",
                        {"asset": "12345678-1234-4234-8234-123456789abc"},
                    )
                nursery.cancel_scope.cancel()
            self.assertFalse(path.exists())
            self.assertEqual(seen, [{"path": "/Photos"}, {}])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory) / "runtime" / "control.sock")

    def test_rejects_malformed_unknown_oversized_and_secret_requests(self) -> None:
        async def status(params: dict[str, object]) -> object:
            return params

        async def scenario(path: Path) -> None:
            async with trio.open_nursery() as nursery:
                await nursery.start(serve_control, path, {"status": status})
                cases = (
                    (b"not json\n", {"id": None, "error": "malformed request"}),
                    (
                        b'{"id":1,"method":"delete","params":{}}\n',
                        {"id": 1, "error": "unknown method"},
                    ),
                    (
                        b'{"id":2,"method":"status","params":{"apiKey":"no"}}\n',
                        {"id": 2, "error": "secret fields are forbidden"},
                    ),
                    (
                        b" " * MAX_MESSAGE_BYTES + b"\n",
                        {"id": None, "error": "request too large"},
                    ),
                )
                for request, expected in cases:
                    response, closed = await _raw_request(path, request)
                    self.assertEqual(response, expected)
                    self.assertEqual(closed, b"")
                nursery.cancel_scope.cancel()

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory) / "runtime" / "control.sock")

    def test_deep_request_and_result_do_not_stop_the_server(self) -> None:
        deep_request_value = b"[" * 10_000 + b"null" + b"]" * 10_000
        deep_result: object = None
        for _ in range(MAX_MESSAGE_BYTES + 1):
            deep_result = [deep_result]

        async def status(params: dict[str, object]) -> object:
            return deep_result

        async def refresh(params: dict[str, object]) -> object:
            return {"refreshed": True}

        async def scenario(path: Path) -> None:
            async with trio.open_nursery() as nursery:
                await nursery.start(
                    serve_control, path, {"status": status, "refresh": refresh}
                )
                request = (
                    b'{"id":1,"method":"refresh","params":{"value":'
                    + deep_request_value
                    + b"}}\n"
                )
                response, _ = await _raw_request(path, request)
                self.assertIn(
                    response,
                    (
                        {"id": 1, "result": {"refreshed": True}},
                        {"id": None, "error": "malformed request"},
                    ),
                )
                with self.assertRaisesRegex(ControlError, "handler result is too complex"):
                    await send_request(path, 2, "status", {})
                self.assertEqual(
                    await send_request(path, 3, "refresh", {}), {"refreshed": True}
                )
                nursery.cancel_scope.cancel()

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory) / "runtime" / "control.sock")

    def test_decoder_recursion_is_a_bounded_request_error(self) -> None:
        async def status(params: dict[str, object]) -> object:
            return {"mounted": True}

        async def scenario(path: Path) -> None:
            async with trio.open_nursery() as nursery:
                await nursery.start(serve_control, path, {"status": status})
                with mock.patch(
                    "immich_on_demand.control._decode_json",
                    side_effect=RecursionError,
                ):
                    response, _ = await _raw_request(
                        path, b'{"id":1,"method":"status","params":{}}\n'
                    )
                self.assertEqual(response, {"id": None, "error": "malformed request"})
                self.assertEqual(
                    await send_request(path, 2, "status", {}), {"mounted": True}
                )
                nursery.cancel_scope.cancel()

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory) / "runtime" / "control.sock")

    def test_rejects_boolean_response_id(self) -> None:
        async def fake_server(
            path: Path,
            *,
            task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
        ) -> None:
            raw = trio.socket.socket(trio.socket.AF_UNIX, trio.socket.SOCK_STREAM)
            try:
                await raw.bind(str(path))
                raw.listen()
                task_status.started()
                connection, _ = await raw.accept()
                stream = trio.SocketStream(connection)
                async with stream:
                    await stream.receive_some(MAX_MESSAGE_BYTES)
                    await stream.send_all(b'{"id":true,"result":{}}\n')
            finally:
                raw.close()
                path.unlink(missing_ok=True)

        async def scenario(path: Path) -> None:
            async with trio.open_nursery() as nursery:
                await nursery.start(fake_server, path)
                with self.assertRaisesRegex(ControlError, "id does not match"):
                    await send_request(path, 1, "status", {})

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory) / "control.sock")

    def test_bounds_failures_without_leaking_handler_secrets(self) -> None:
        async def broken(params: dict[str, object]) -> object:
            raise RuntimeError("API key is do-not-leak")

        async def secret_result(params: dict[str, object]) -> object:
            return {"access": {"token": "do-not-leak"}}

        async def huge(params: dict[str, object]) -> object:
            return "x" * MAX_MESSAGE_BYTES

        async def scenario(path: Path) -> None:
            async with trio.open_nursery() as nursery:
                await nursery.start(
                    serve_control,
                    path,
                    {"status": broken, "refresh": secret_result, "evict": huge},
                )
                for method, error in (
                    ("status", "request failed"),
                    ("refresh", "handler returned forbidden fields"),
                    ("evict", "response too large"),
                ):
                    with self.assertRaisesRegex(ControlError, f"^{error}$"):
                        await send_request(path, 11, method, {})
                nursery.cancel_scope.cancel()

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory) / "runtime" / "control.sock")

    def test_times_out_a_handler(self) -> None:
        async def blocked(params: dict[str, object]) -> object:
            await trio.sleep_forever()

        async def scenario(path: Path) -> None:
            from functools import partial

            async with trio.open_nursery() as nursery:
                await nursery.start(
                    partial(serve_control, path, {"status": blocked}, timeout=0.01)
                )
                with self.assertRaisesRegex(ControlError, "^request timed out$"):
                    await send_request(path, 1, "status", {}, timeout=1)
                nursery.cancel_scope.cancel()

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory) / "runtime" / "control.sock")

    def test_replaces_only_an_owned_stale_socket(self) -> None:
        async def status(params: dict[str, object]) -> object:
            return True

        async def scenario(path: Path) -> None:
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.close()
            async with trio.open_nursery() as nursery:
                await nursery.start(serve_control, path, {"status": status})
                self.assertTrue(await send_request(path, 1, "status", {}))
                nursery.cancel_scope.cancel()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sock"
            self.assertEqual(os.getuid(), path.parent.stat().st_uid)
            trio.run(scenario, path)

    def test_refuses_to_replace_a_non_socket(self) -> None:
        async def scenario(path: Path) -> None:
            path.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "refusing to replace"):
                await serve_control(path, {})
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory) / "control.sock")


if __name__ == "__main__":
    unittest.main()
