from __future__ import annotations

import json
import secrets
from urllib.parse import urlsplit

from .control import send_request
from .settings import runtime_path


def _is_local_uri(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "file"
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
    )


async def run_action(action: str, target: str | list[str] | None = None):
    if action in {"status", "refresh"} and target is None:
        method, params = action, {}
    elif action == "evict" and _is_local_uri(target):
        assert isinstance(target, str)
        method, params = "evict", {"uri": target}
    elif action in {"pin", "unpin"} and _is_local_uri(target):
        assert isinstance(target, str)
        method, params = "pin", {"uri": target, "pinned": action == "pin"}
    elif (
        action == "describe"
        and isinstance(target, list)
        and 0 < len(target) <= 64
        and all(_is_local_uri(uri) for uri in target)
    ):
        params = {"uris": target}
        frame = {
            "id": (1 << 63) - 1,
            "method": "describe",
            "params": params,
        }
        if (
            len(
                json.dumps(
                    frame,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            + 1
            >= 48 * 1024
        ):
            raise ValueError("describe request is too large")
        method = "describe"
    else:
        raise ValueError("invalid desktop action")
    return await send_request(
        runtime_path() / "control.sock",
        secrets.randbits(63) or 1,
        method,
        params,
    )
