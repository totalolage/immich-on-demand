import argparse
import json
import logging
from pathlib import Path
import secrets
import sys
from uuid import UUID

import httpx
import trio

from . import __version__
from .auth import validate_api_key
from .control import send_request
from .service import run_service
from .settings import (
    Settings,
    config_path,
    load,
    load_api_key,
    runtime_path,
    save,
)
from .uploads import UploadErrorCode, UploadState


def _asset_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("asset must be a UUID") from error


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


def _confirmation_name(value: str) -> str:
    if not value:
        raise argparse.ArgumentTypeError("confirmation name must not be empty")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="immich-on-demand")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--config", type=Path, default=config_path())
    commands = result.add_subparsers(dest="command", required=True)

    configure = commands.add_parser("configure", help="write non-secret settings")
    configure.add_argument("--server", required=True)
    configure.add_argument("--mount", required=True, type=Path)
    configure.add_argument("--cache-max-gib", type=_positive_int, default=10)
    configure.add_argument("--cache-max-age-days", type=_positive_int, default=30)
    configure.add_argument("--minimum-free-gib", type=_positive_int, default=5)
    configure.add_argument("--enable-remote-delete", action="store_true")

    auth_check = commands.add_parser("auth-check", help="validate an API key")
    auth_check.add_argument("--mutation", action="store_true")
    commands.add_parser("mount", help="run the foreground filesystem service")
    commands.add_parser("refresh", help="ask the running service to refresh")
    commands.add_parser("status", help="show local catalog counts")
    evict = commands.add_parser("evict", help="evict cached originals")
    evict.add_argument("--asset", type=_asset_id)
    for command in ("pin", "unpin"):
        pin = commands.add_parser(command, help=f"{command} a cached original")
        pin.add_argument("--asset", type=_asset_id, required=True)
    pin_status = commands.add_parser("pin-status", help="show an original's Pin state")
    pin_status.add_argument("--asset", type=_asset_id, required=True)
    restore = commands.add_parser("restore", help="restore a trashed asset")
    restore.add_argument("--asset", type=_asset_id, required=True)
    commands.add_parser("uploads", help="list Pending uploads")
    retry_upload = commands.add_parser("retry-upload", help="retry a Pending upload")
    retry_upload.add_argument("--id", type=_asset_id, required=True)
    cancel_upload = commands.add_parser(
        "cancel-upload", help="cancel a Pending upload"
    )
    cancel_upload.add_argument("--id", type=_asset_id, required=True)
    cancel_upload.add_argument("--revision", type=_nonnegative_int, required=True)
    cancel_upload.add_argument(
        "--confirm-name", type=_confirmation_name, required=True
    )
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "configure":
            destination = save(
                Settings(
                    arguments.server,
                    arguments.mount.expanduser().resolve(),
                    cache_max_bytes=arguments.cache_max_gib * 1024**3,
                    cache_max_age_seconds=arguments.cache_max_age_days * 24 * 60 * 60,
                    minimum_free_bytes=arguments.minimum_free_gib * 1024**3,
                    remote_delete=arguments.enable_remote_delete,
                ),
                arguments.config,
            )
            print(destination)
            return 0
        if arguments.command == "auth-check":
            return trio.run(_auth_check, load(arguments.config), arguments.mutation)
        if arguments.command == "mount":
            logging.basicConfig(level=logging.INFO)
            trio.run(run_service, load(arguments.config))
            return 0
        if arguments.command == "uploads":
            return trio.run(_uploads)
        if arguments.command in {
            "refresh",
            "status",
            "evict",
            "pin",
            "unpin",
            "pin-status",
            "restore",
            "retry-upload",
            "cancel-upload",
        }:
            if arguments.command in {"pin", "unpin"}:
                method = "pin"
                params = {
                    "asset": arguments.asset,
                    "pinned": arguments.command == "pin",
                }
            elif arguments.command == "pin-status":
                method = "pin"
                params = {"asset": arguments.asset}
            elif arguments.command == "restore":
                method = "restore"
                params = {"asset": arguments.asset}
            elif arguments.command == "retry-upload":
                method = "retry-upload"
                params = {"id": arguments.id}
            elif arguments.command == "cancel-upload":
                method = "cancel-upload"
                params = {
                    "id": arguments.id,
                    "revision": arguments.revision,
                    "confirm_name": arguments.confirm_name,
                }
            else:
                method = arguments.command
                params = (
                    {"asset": arguments.asset}
                    if arguments.command == "evict" and arguments.asset is not None
                    else {}
                )
            return trio.run(_control, method, params)
        raise AssertionError(arguments.command)
    except (OSError, RuntimeError, ValueError, httpx.HTTPError) as error:
        print(f"immich-on-demand: {error}", file=sys.stderr)
        return 1


async def _auth_check(settings: Settings, mutation: bool) -> int:
    purpose = "mutation" if mutation else "read-only"
    session = await validate_api_key(
        settings,
        purpose,
        load_api_key(settings, purpose),
    )
    print(f"Immich {session.version}; {purpose} key verified")
    return 0


async def _control(method: str, params: dict[str, object]) -> int:
    result = await send_request(
        runtime_path() / "control.sock", secrets.randbits(63) or 1, method, params
    )
    _print_result(result)
    return 0


async def _uploads() -> int:
    after: str | None = None
    seen: set[str] = set()
    while True:
        result = await send_request(
            runtime_path() / "control.sock",
            secrets.randbits(63) or 1,
            "uploads",
            {"after": after, "limit": 32},
        )
        items, next_id = _upload_page(result)
        for item in items:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
        if next_id is None:
            return 0
        if next_id in seen:
            raise RuntimeError("control returned an invalid uploads page")
        seen.add(next_id)
        after = next_id


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _upload_page(result: object) -> tuple[list[dict[str, object]], str | None]:
    expected_item = {"id", "name", "state", "size", "error", "revision"}
    if (
        not isinstance(result, dict)
        or set(result) != {"items", "next"}
        or not isinstance(result["items"], list)
        or len(result["items"]) > 32
    ):
        raise RuntimeError("control returned an invalid uploads page")
    items = result["items"]
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != expected_item
            or not _is_canonical_uuid(item["id"])
            or not isinstance(item["name"], str)
            or item["state"] not in {state.value for state in UploadState}
            or not (
                item["size"] is None
                or (type(item["size"]) is int and item["size"] >= 0)
            )
            or not (
                item["error"] is None
                or item["error"] in {error.value for error in UploadErrorCode}
            )
            or type(item["revision"]) is not int
            or item["revision"] < 0
        ):
            raise RuntimeError("control returned an invalid uploads page")
    next_id = result.get("next")
    if next_id is not None and not _is_canonical_uuid(next_id):
        raise RuntimeError("control returned an invalid uploads page")
    return items, next_id


def _print_result(result: object) -> None:
    if not isinstance(result, dict) or any(
        not isinstance(key, str)
        or not (value is None or isinstance(value, (bool, int, float, str)))
        for key, value in result.items()
    ):
        raise RuntimeError("control returned a non-flat result")
    print(" ".join(f"{key}={result[key]}" for key in sorted(result)))
