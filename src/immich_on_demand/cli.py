import argparse
from dataclasses import asdict
from pathlib import Path
import sys

import trio

from . import __version__
from .app import refresh_catalog
from .catalog import Catalog, CatalogStats
from .immich import ImmichClient
from .settings import Settings, config_path, load, load_api_key, save, state_path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="immich-on-demand")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--config", type=Path, default=config_path())
    commands = result.add_subparsers(dest="command", required=True)

    configure = commands.add_parser("configure", help="write non-secret settings")
    configure.add_argument("--server", required=True)
    configure.add_argument("--mount", required=True, type=Path)

    commands.add_parser("auth-check", help="validate the read-only key")
    commands.add_parser("refresh", help="replace the local catalog from Immich")
    commands.add_parser("status", help="show local catalog counts")
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
        settings = load(arguments.config)
        if arguments.command == "auth-check":
            return trio.run(_auth_check, settings)
        if arguments.command == "refresh":
            return trio.run(_refresh, settings)
        if arguments.command == "status":
            with Catalog(state_path() / "catalog.db") as catalog:
                _print_stats(catalog.stats())
            return 0
        raise AssertionError(arguments.command)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"immich-on-demand: {error}", file=sys.stderr)
        return 1


async def _auth_check(settings: Settings) -> int:
    async with ImmichClient(settings.server_url, load_api_key(settings)) as client:
        session = await client.validate()
    print(f"Immich {session.version}; read-only key verified")
    return 0


async def _refresh(settings: Settings) -> int:
    with Catalog(state_path() / "catalog.db") as catalog:
        async with ImmichClient(settings.server_url, load_api_key(settings)) as client:
            session = await client.validate()
            stats = await refresh_catalog(catalog, client, session)
    _print_stats(stats)
    return 0


def _print_stats(stats: CatalogStats) -> None:
    values = asdict(stats)
    print(" ".join(f"{key}={value}" for key, value in values.items()))
