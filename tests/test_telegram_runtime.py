"""CLI wiring and isolated startup checks without contacting external services."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from curatarr.cli import main
from curatarr.config import ConfigError, TmdbConfig, UsersConfig, parse_config
from curatarr.runtime import build_telegram_bot
from curatarr.storage import Storage
from test_config import VALID_CONFIG


class TelegramRuntimeTests(unittest.TestCase):
    def config(self):
        return parse_config(yaml.safe_load(VALID_CONFIG))

    def test_bot_requires_tmdb_and_configured_users(self):
        storage = Storage(":memory:")
        self.addCleanup(storage.close)
        storage.initialize()
        config = self.config()
        with self.assertRaisesRegex(ConfigError, "tmdb.api_key"):
            build_telegram_bot(config, storage)
        config = replace(config, tmdb=TmdbConfig("test-key"), users=UsersConfig((), ()))
        with self.assertRaisesRegex(ConfigError, "configured Telegram user"):
            build_telegram_bot(config, storage)

    def test_factory_wires_shared_storage_and_read_adapters(self):
        storage = Storage(":memory:")
        self.addCleanup(storage.close)
        config = replace(self.config(), tmdb=TmdbConfig("test-key"))
        bot = build_telegram_bot(config, storage)
        self.assertIs(bot.callbacks.storage, storage)
        self.assertIs(bot.search.search_decisions.pending_decision_service.storage, storage)
        self.assertEqual(bot.allowed_users, frozenset({1, 2}))
        self.assertIs(bot.callbacks.search_decisions, bot.search.search_decisions)

    def test_persistent_runtime_has_a_dedicated_read_only_maintenance_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            storage=Storage(Path(temp)/"state.sqlite")
            self.addCleanup(storage.close)
            config=replace(self.config(),tmdb=TmdbConfig("test-key"))
            bot=build_telegram_bot(config,storage)
            self.assertIsNotNone(bot.maintenance_worker)
            self.assertIsNot(bot.maintenance_worker,bot.automation_worker)

    def test_source_collection_is_separate_from_execution_and_default_off(self):
        with tempfile.TemporaryDirectory() as temp:
            storage=Storage(Path(temp)/'state.sqlite');storage.initialize();self.addCleanup(storage.close)
            config=replace(self.config(),tmdb=TmdbConfig('fixture'))
            self.assertIsNone(build_telegram_bot(config,storage).source_worker)
            config=replace(config,coordinator=replace(config.coordinator,enabled=True))
            bot=build_telegram_bot(config,storage)
            self.assertIsNotNone(bot.source_worker)
            self.assertIsNot(bot.source_worker,bot.automation_worker)

    def test_cli_starts_polling_and_closes_storage_on_interrupt(self):
        config = replace(self.config(), tmdb=TmdbConfig("test-key"))
        bot = Mock()
        bot.run.side_effect = KeyboardInterrupt
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "state.sqlite"
            with patch("curatarr.cli.load_config", return_value=config), \
                    patch("curatarr.runtime.build_telegram_bot", return_value=bot) as build:
                code = main(["--config", str(Path(temp) / "config.yaml"),
                             "--run-telegram", "--database", str(database)])
            self.assertEqual(code, 0)
            self.assertTrue(database.exists())
            bot.run.assert_called_once()
            with self.assertRaises(Exception) as raised:
                build.call_args.args[1].connection.execute("SELECT 1")
            self.assertIn("closed", str(raised.exception))

    def test_check_config_does_not_start_bot(self):
        with patch("curatarr.cli.load_config", return_value=self.config()), \
                patch("curatarr.runtime.build_telegram_bot") as build, patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["--config", "config.yaml", "--check-config"]), 0)
        build.assert_not_called()

    def test_direct_imports_work_in_fresh_process(self):
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        result = subprocess.run([
            sys.executable, "-c",
            "from curatarr.workflows.search_add import SearchAddPlanner; "
            "from curatarr.runtime import build_telegram_bot; "
            "from curatarr.services import TelegramSearchCommand, PendingDecisionService",
        ], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_configuration_rejects_invalid_auth_ids_and_logging(self):
        for value in (True, 0, -1):
            data = yaml.safe_load(VALID_CONFIG)
            data["users"]["admins"] = [value]
            with self.assertRaises(ConfigError):
                parse_config(data)
        data = yaml.safe_load(VALID_CONFIG)
        data["logging"]["level"] = "unsupported"
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_tmdb_config_is_optional_for_existing_configs_and_validated_if_present(self):
        self.assertIsNone(self.config().tmdb)
        data = yaml.safe_load(VALID_CONFIG)
        data["tmdb"] = {"api_key": "test-key"}
        self.assertEqual(parse_config(data).tmdb.api_key, "test-key")
        data["tmdb"] = {}
        with self.assertRaises(ConfigError):
            parse_config(data)
