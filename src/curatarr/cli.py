"""Command-line entrypoint for Curatarr."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from curatarr import __version__
from curatarr.config import ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curatarr",
        description="Curatarr runtime bootstrap.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml. Required unless --version is used.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Load and validate configuration, then exit.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"curatarr {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.config is None:
        parser.error("--config is required")

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.check_config:
        print(f"Configuration OK: {config.logging.level}")
        return 0

    print("Curatarr runtime workflows are not implemented yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
