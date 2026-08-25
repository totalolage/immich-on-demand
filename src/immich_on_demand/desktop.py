from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from .control import send_request
from .settings import runtime_path


async def run_action(action: str, uri: str | None = None):
    if action == "refresh" and uri is None:
        method, params = "refresh", {}
    elif action == "evict" and isinstance(uri, str):
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("evict requires a local file URI")
        method, params = "evict", {"uri": uri}
    else:
        raise ValueError("invalid desktop action")
    return await send_request(
        runtime_path() / "control.sock",
        secrets.randbits(63) or 1,
        method,
        params,
    )
