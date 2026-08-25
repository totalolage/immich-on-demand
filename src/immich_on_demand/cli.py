import argparse
import logging
from pathlib import Path
import secrets
import sys
from uuid import UUID

import httpx
import trio

from . import __version__
from .control import send_request
from .immich import (
    ImmichClient,
    MUTATION_PERMISSIONS,
    READ_PERMISSIONS,
    UPLOAD_PERMISSIONS,
)
from .service import run_service
from .settings import Settings, config_path, load, load_api_key, runtime_path, save


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
        if arguments.command in {"refresh", "status", "evict"}:
            params = (
                {"asset": arguments.asset}
                if arguments.command == "evict" and arguments.asset is not None
                else {}
            )
            return trio.run(_control, arguments.command, params)
        raise AssertionError(arguments.command)
    except (OSError, RuntimeError, ValueError, httpx.HTTPError) as error:
        print(f"immich-on-demand: {error}", file=sys.stderr)
        return 1


async def _auth_check(settings: Settings, mutation: bool) -> int:
    purpose = "mutation" if mutation else "read-only"
    permissions = READ_PERMISSIONS
    if mutation:
        permissions = MUTATION_PERMISSIONS if settings.remote_delete else UPLOAD_PERMISSIONS
    async with ImmichClient(settings.server_url, load_api_key(settings, purpose)) as client:
        session = await client.validate(permissions)
    print(f"Immich {session.version}; {purpose} key verified")
    return 0


async def _control(method: str, params: dict[str, object]) -> int:
    result = await send_request(
        runtime_path() / "control.sock", secrets.randbits(63) or 1, method, params
    )
    _print_result(result)
    return 0


def _print_result(result: object) -> None:
    if not isinstance(result, dict) or any(
        not isinstance(key, str)
        or not (value is None or isinstance(value, (bool, int, float, str)))
        for key, value in result.items()
    ):
        raise RuntimeError("control returned a non-flat result")
    print(" ".join(f"{key}={result[key]}" for key in sorted(result)))
