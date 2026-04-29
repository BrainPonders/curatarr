"""Tests for Curatarr configuration loading."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from curatarr.config import ConfigError, expand_env, load_config, parse_duration


VALID_CONFIG = """
users:
  admins: [1]
  standard: [2]
telegram:
  bot_token: ${BOT_TOKEN}
ryot:
  url: http://ryot:8000
  api_key: ${RYOT_KEY}
  scan_interval: 45m
  collections:
    archived: Archived
    owned: Owned
    radarr: Radarr
    sonarr: Sonarr
radarr:
  url: http://radarr:7878
  api_key: ${RADARR_KEY}
  default_profile: Movies
  root_folder: /movies
  exclusion_sync: true
sonarr:
  url: http://sonarr:8989
  api_key: ${SONARR_KEY}
  default_profile: Shows
  root_folder: /series
  exclusion_sync: false
jellyfin:
  url: http://jellyfin:8096
  api_key: ${JELLYFIN_KEY}
  reception_checks:
    enabled: true
    first_check_delay: 5m
    retry_interval: 15m
    timeout: 2h
policy:
  approvals:
    default_mode: confirm_user
identity:
  weak_match_action: disabled
logging:
  level: debug
"""


class ConfigTests(unittest.TestCase):
    def test_expand_env_requires_referenced_variables(self) -> None:
        with self.assertRaisesRegex(ConfigError, "MISSING"):
            expand_env("token: ${MISSING}", environ={})

    def test_parse_duration_returns_seconds(self) -> None:
        self.assertEqual(parse_duration("30m").seconds, 1800)
        self.assertEqual(parse_duration("24h").seconds, 86400)
        self.assertEqual(parse_duration("7d").seconds, 604800)

    def test_parse_duration_rejects_invalid_values(self) -> None:
        with self.assertRaises(ConfigError):
            parse_duration("0m")
        with self.assertRaises(ConfigError):
            parse_duration("30 minutes")

    def test_load_config_expands_env_and_builds_typed_model(self) -> None:
        env = {
            "BOT_TOKEN": "telegram-secret",
            "RYOT_KEY": "ryot-secret",
            "RADARR_KEY": "radarr-secret",
            "SONARR_KEY": "sonarr-secret",
            "JELLYFIN_KEY": "jellyfin-secret",
        }
        old_env = os.environ.copy()
        os.environ.update(env)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "config.yaml"
                path.write_text(VALID_CONFIG, encoding="utf-8")
                config = load_config(path)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertEqual(config.telegram.bot_token, "telegram-secret")
        self.assertEqual(config.users.admins, (1,))
        self.assertEqual(config.ryot.scan_interval.seconds, 2700)
        self.assertTrue(config.radarr.exclusion_sync)
        self.assertFalse(config.sonarr.exclusion_sync)
        self.assertEqual(config.logging.level, "DEBUG")


if __name__ == "__main__":
    unittest.main()
