import argparse

from . import __version__


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="immich-on-demand")
    result.add_argument("--version", action="version", version=__version__)
    return result


def main(argv: list[str] | None = None) -> int:
    parser().parse_args(argv)
    return 0
