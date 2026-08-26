from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import errno
import json
import os
from pathlib import Path
import socket
import stat
from typing import Any

import trio


MAX_MESSAGE_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0
METHODS = frozenset({"status", "refresh", "evict", "describe", "pin"})
_SECRET_FIELDS = frozenset(
    {"apikey", "authorization", "credential", "credentials", "password", "secret", "token"}
)

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


class ControlError(RuntimeError):
    pass


class _RequestError(Exception):
    def __init__(self, message: str, request_id: int | None = None) -> None:
        self.message = message
        self.request_id = request_id


def _prepare_parent(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError(f"control socket directory is not owned by this user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError(f"control socket directory must have mode 0700: {path}")


def _remove_stale_socket(path: Path) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise FileExistsError(f"refusing to replace control socket path: {path}")

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.1)
    try:
        probe.connect(str(path))
    except OSError as error:
        if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
            raise FileExistsError(f"cannot prove control socket is stale: {path}") from error
    else:
        raise FileExistsError(f"control socket is already in use: {path}")
    finally:
        probe.close()

    current = path.stat(follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise FileExistsError(f"control socket changed while checking it: {path}")
    path.unlink()


def _unlink_bound_socket(path: Path, identity: tuple[int, int]) -> None:
    try:
        info = path.stat(follow_symlinks=False)
        if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_json(data: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(
        data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=reject_constant
    )


def _has_secret_field(value: Any) -> bool:
    pending = [value]
    visited = 0
    while pending:
        visited += 1
        if visited > MAX_MESSAGE_BYTES:
            raise ValueError("message structure is too complex")
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = "".join(
                    character for character in str(key).lower() if character.isalnum()
                )
                if any(normalized.endswith(field) for field in _SECRET_FIELDS):
                    return True
                pending.append(child)
                if visited + len(pending) > MAX_MESSAGE_BYTES:
                    raise ValueError("message structure is too complex")
        elif isinstance(item, (list, tuple)):
            for child in item:
                pending.append(child)
                if visited + len(pending) > MAX_MESSAGE_BYTES:
                    raise ValueError("message structure is too complex")
    return False


def _parse_request(data: bytes) -> tuple[int, str, dict[str, Any]]:
    try:
        request = _decode_json(data)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as error:
        raise _RequestError("malformed request") from error
    if not isinstance(request, dict):
        raise _RequestError("request must be an object")

    request_id = request.get("id")
    valid_id = isinstance(request_id, int) and not isinstance(request_id, bool)
    response_id = request_id if valid_id else None
    if set(request) != {"id", "method", "params"} or not valid_id:
        raise _RequestError("invalid request shape", response_id)
    method = request["method"]
    params = request["params"]
    if not isinstance(method, str) or method not in METHODS:
        raise _RequestError("unknown method", request_id)
    if not isinstance(params, dict):
        raise _RequestError("params must be an object", request_id)
    try:
        if _has_secret_field(params):
            raise _RequestError("secret fields are forbidden", request_id)
    except ValueError as error:
        raise _RequestError("request structure is too complex", request_id) from error
    return request_id, method, params


def _encode_response(response: dict[str, Any]) -> bytes:
    try:
        data = json.dumps(
            response, allow_nan=False, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, RecursionError) as error:
        raise _RequestError("result is not JSON serializable", response.get("id")) from error
    if len(data) > MAX_MESSAGE_BYTES:
        raise _RequestError("response too large", response.get("id"))
    return data


async def _read_line(stream: trio.SocketStream) -> bytes:
    data = bytearray()
    while True:
        chunk = await stream.receive_some(min(4096, MAX_MESSAGE_BYTES + 1 - len(data)))
        if not chunk:
            raise _RequestError("request must end with a newline")
        data.extend(chunk)
        newline = data.find(b"\n")
        if newline >= 0:
            if newline + 1 > MAX_MESSAGE_BYTES:
                raise _RequestError("request too large")
            if data[newline + 1 :]:
                raise _RequestError("one request is allowed per connection")
            return bytes(data[:newline])
        if len(data) >= MAX_MESSAGE_BYTES:
            raise _RequestError("request too large")


async def _send(stream: trio.SocketStream, response: dict[str, Any], timeout: float) -> None:
    try:
        data = _encode_response(response)
    except _RequestError as error:
        data = _encode_response({"id": error.request_id, "error": error.message})
    try:
        with trio.fail_after(timeout):
            await stream.send_all(data)
    except (trio.BrokenResourceError, trio.ClosedResourceError, trio.TooSlowError, OSError):
        pass


async def _handle_connection(
    stream: trio.SocketStream, handlers: Mapping[str, Handler], timeout: float
) -> None:
    request_id: int | None = None
    try:
        try:
            with trio.fail_after(timeout):
                data = await _read_line(stream)
            request_id, method, params = _parse_request(data)
        except trio.TooSlowError:
            await _send(stream, {"id": None, "error": "request timed out"}, timeout)
            return
        except _RequestError as error:
            await _send(stream, {"id": error.request_id, "error": error.message}, timeout)
            return

        handler = handlers.get(method)
        if handler is None:
            await _send(stream, {"id": request_id, "error": "method unavailable"}, timeout)
            return
        try:
            with trio.fail_after(timeout):
                result = await handler(params)
        except trio.TooSlowError:
            await _send(stream, {"id": request_id, "error": "request timed out"}, timeout)
            return
        except Exception:
            await _send(stream, {"id": request_id, "error": "request failed"}, timeout)
            return
        try:
            contains_secret = _has_secret_field(result)
        except ValueError:
            await _send(
                stream,
                {"id": request_id, "error": "handler result is too complex"},
                timeout,
            )
            return
        if contains_secret:
            await _send(
                stream,
                {"id": request_id, "error": "handler returned forbidden fields"},
                timeout,
            )
            return
        await _send(stream, {"id": request_id, "result": result}, timeout)
    except (trio.BrokenResourceError, trio.ClosedResourceError, OSError):
        pass


async def serve_control(
    path: Path,
    handlers: Mapping[str, Handler],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    task_status: trio.TaskStatus[list[trio.SocketListener]] = trio.TASK_STATUS_IGNORED,
) -> None:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if not set(handlers) <= METHODS:
        raise ValueError("handler mapping contains an unknown method")
    stable_handlers = dict(handlers)
    _prepare_parent(path.parent)
    _remove_stale_socket(path)

    raw_socket = trio.socket.socket(trio.socket.AF_UNIX, trio.socket.SOCK_STREAM)
    try:
        await raw_socket.bind(str(path))
        info = path.stat(follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        raw_socket.listen()
        os.chmod(path, 0o600)
        listener = trio.SocketListener(raw_socket)
        await trio.serve_listeners(
            lambda stream: _handle_connection(stream, stable_handlers, timeout),
            [listener],
            task_status=task_status,
        )
    finally:
        raw_socket.close()
        if "identity" in locals():
            _unlink_bound_socket(path, identity)


async def send_request(
    path: Path,
    request_id: int,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    request = {"id": request_id, "method": method, "params": params}
    try:
        data = _encode_response(request)
        request_id, _, _ = _parse_request(data[:-1])
    except _RequestError as error:
        raise ControlError(error.message) from error
    try:
        with trio.fail_after(timeout):
            try:
                stream = await trio.open_unix_socket(path)
            except OSError:
                raise ControlError("control service is unavailable") from None
            async with stream:
                await stream.send_all(data)
                response = _decode_json(await _read_line(stream))
    except trio.TooSlowError as error:
        raise ControlError("control request timed out") from error
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
        _RequestError,
    ) as error:
        raise ControlError("invalid control response") from error

    if (
        not isinstance(response, dict)
        or type(response.get("id")) is not int
        or response["id"] != request_id
    ):
        raise ControlError("control response id does not match")
    if set(response) == {"id", "result"}:
        try:
            if _has_secret_field(response["result"]):
                raise ControlError("control response contains forbidden fields")
        except ValueError as error:
            raise ControlError("control response is too complex") from error
        return response["result"]
    if set(response) == {"id", "error"} and isinstance(response["error"], str):
        raise ControlError(response["error"][:256])
    raise ControlError("invalid control response")
