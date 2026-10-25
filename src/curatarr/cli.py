"""Command-line entrypoint for Curatarr."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from curatarr import __version__
from curatarr.config import ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curatarr",
        description="Curatarr runtime bootstrap.",
    )
    runtime = parser.add_mutually_exclusive_group()
    runtime.add_argument(
        "--run-telegram", action="store_true", help="Run Telegram movie search and decision review.",
    )
    runtime.add_argument("--run-web", action="store_true", help="Run the Telegram Mini App and admin maintenance API.")
    runtime.add_argument("--reconcile-movies", action="store_true",
                         help="Run a fresh read-only movie consistency scan and print persisted admin findings as JSON.")
    runtime.add_argument("--check-maintenance", action="store_true",
                         help="Run the comprehensive read-only ecosystem check and print repository diagnostics as JSON.")
    parser.add_argument("--host", default="127.0.0.1", help="Web bind address (default: loopback only).")
    parser.add_argument("--port", type=int, default=8765, help="Web port (default: 8765).")
    parser.add_argument(
        "--database", type=Path, default=None,
        help="SQLite workflow file for decisions and reconciliation; defaults to data/curatarr.sqlite beside the config.",
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

    if args.check_maintenance:
        from curatarr.runtime import build_system_maintenance
        from curatarr.storage import Storage, StorageError
        from curatarr.adapters import AdapterError
        storage = None
        try:
            database = args.database or args.config.resolve().parent / "data" / "curatarr.sqlite"
            database.parent.mkdir(parents=True, exist_ok=True)
            storage = Storage(database)
            storage.initialize()
            monitor = build_system_maintenance(config, storage)
            claim = monitor.claim(force=True)
            if claim:
                monitor.run(claim)
            print(json.dumps({"summary": monitor.summary(), "report": monitor.report()}, sort_keys=True, indent=2))
            return 0 if monitor.summary()["complete"] else 1
        except (AdapterError, StorageError, OSError, sqlite3.Error, ValueError):
            print("Maintenance check could not complete. Previous findings were retained.", file=sys.stderr)
            return 1
        finally:
            if storage is not None:
                storage.close()

    if args.reconcile_movies:
        from curatarr.adapters import AdapterError
        from curatarr.runtime import build_movie_reconciliation
        from curatarr.storage import Storage, StorageError

        database = args.database or args.config.resolve().parent / "data" / "curatarr.sqlite"
        storage = None
        try:
            database.parent.mkdir(parents=True, exist_ok=True)
            storage = Storage(database)
            storage.initialize()
            report = build_movie_reconciliation(config, storage).scan()
            print(json.dumps(report, sort_keys=True, indent=2))
            return 0 if report["revalidated"] else 1
        except KeyboardInterrupt:
            return 130
        except (AdapterError, ConfigError, OSError, sqlite3.Error, StorageError):
            print("Movie reconciliation could not complete. Check configuration and local storage.", file=sys.stderr)
            return 1
        finally:
            if storage is not None:
                storage.close()

    if args.run_web:
        import uvicorn
        from curatarr.web.app import create_app

        database = args.database or args.config.resolve().parent / "data" / "curatarr.sqlite"
        try:
            app = create_app(config, database=database)
            uvicorn.run(app, host=args.host, port=args.port, access_log=False,
                        log_level=config.logging.level.lower(), proxy_headers=False)
        except KeyboardInterrupt:
            return 0
        except (ConfigError, OSError, sqlite3.Error):
            print("Mini App could not start. Check configuration and local port availability.", file=sys.stderr)
            return 1
        return 0

    if args.run_telegram:
        from curatarr.integrations.telegram.client import TelegramError
        from curatarr.runtime import build_telegram_bot
        from curatarr.storage import Storage

        database = args.database or args.config.resolve().parent / "data" / "curatarr.sqlite"
        storage = None
        try:
            database.parent.mkdir(parents=True, exist_ok=True)
            storage = Storage(database)
            storage.initialize()
            bot = build_telegram_bot(config, storage)
            logging.basicConfig(level=config.logging.level)
            bot.run()
        except KeyboardInterrupt:
            return 0
        except (ConfigError, TelegramError, OSError, sqlite3.Error):
            print("Telegram runtime could not start or continue. Check configuration and service access.",
                  file=sys.stderr)
            return 1
        finally:
            if storage is not None:
                storage.close()
        return 0

    print("Use --run-telegram or --run-web for interaction, --reconcile-movies for a read-only scan, "
          "or --check-config to validate configuration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
