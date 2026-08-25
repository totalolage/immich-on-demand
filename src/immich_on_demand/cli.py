import argparse
from pathlib import Path
import secrets
import sys
from uuid import UUID

import trio

from . import __version__
from .control import send_request
from .immich import ImmichClient
from .settings import Settings, config_path, load, load_api_key, runtime_path, save


def _asset_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("asset must be a UUID") from error


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="immich-on-demand")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--config", type=Path, default=config_path())
    commands = result.add_subparsers(dest="command", required=True)

    configure = commands.add_parser("configure", help="write non-secret settings")
    configure.add_argument("--server", required=True)
    configure.add_argument("--mount", required=True, type=Path)

    commands.add_parser("auth-check", help="validate the read-only key")
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
                Settings(arguments.server, arguments.mount.expanduser().resolve()), arguments.config
            )
            print(destination)
            return 0
        if arguments.command == "auth-check":
            return trio.run(_auth_check, load(arguments.config))
        if arguments.command in {"refresh", "status", "evict"}:
            params = (
                {"asset": arguments.asset}
                if arguments.command == "evict" and arguments.asset is not None
                else {}
            )
            return trio.run(_control, arguments.command, params)
        raise AssertionError(arguments.command)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"immich-on-demand: {error}", file=sys.stderr)
        return 1


async def _auth_check(settings: Settings) -> int:
    async with ImmichClient(settings.server_url, load_api_key(settings)) as client:
        session = await client.validate()
    print(f"Immich {session.version}; read-only key verified")
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
