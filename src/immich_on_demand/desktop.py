from __future__ import annotations

import json
import secrets
from urllib.parse import urlsplit
from uuid import UUID

from .control import send_request
from .profiles import Profile


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


def _canonical_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid desktop action")
    try:
        return str(UUID(value))
    except ValueError as error:
        raise ValueError("invalid desktop action") from error


async def run_action(
    profile: Profile,
    action: str,
    target: str | list[str] | None = None,
    revision: int | None = None,
    confirm_name: str | None = None,
):
    if (
        action in {"status", "refresh"}
        and target is None
        and revision is None
        and confirm_name is None
    ):
        method, params = action, {}
    elif (
        action == "evict"
        and _is_local_uri(target)
        and revision is None
        and confirm_name is None
    ):
        assert isinstance(target, str)
        method, params = "evict", {"uri": target}
    elif (
        action in {"pin", "unpin"}
        and _is_local_uri(target)
        and revision is None
        and confirm_name is None
    ):
        assert isinstance(target, str)
        method, params = "pin", {"uri": target, "pinned": action == "pin"}
    elif (
        action == "restore"
        and isinstance(target, str)
        and revision is None
        and confirm_name is None
    ):
        method, params = "restore", {"asset": _canonical_id(target)}
    elif (
        action == "uploads"
        and (target is None or isinstance(target, str))
        and revision is None
        and confirm_name is None
    ):
        method = "uploads"
        params = {
            "after": None if target is None else _canonical_id(target),
            "limit": 32,
        }
    elif (
        action == "retry-upload"
        and isinstance(target, str)
        and revision is None
        and confirm_name is None
    ):
        method, params = "retry-upload", {"id": _canonical_id(target)}
    elif (
        action == "cancel-upload"
        and isinstance(target, str)
        and type(revision) is int
        and revision >= 0
        and isinstance(confirm_name, str)
        and bool(confirm_name)
    ):
        method, params = "cancel-upload", {
            "id": _canonical_id(target),
            "revision": revision,
            "confirm_name": confirm_name,
        }
    elif (
        action == "describe"
        and isinstance(target, list)
        and 0 < len(target) <= 64
        and all(_is_local_uri(uri) for uri in target)
        and revision is None
        and confirm_name is None
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
        profile.runtime / "control.sock",
        secrets.randbits(63) or 1,
        method,
        params,
    )
