"""YAML configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DURATION_PATTERN = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>s|m|h|d)$")


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or validated."""


@dataclass(frozen=True)
class Duration:
    """Human-readable duration stored as seconds."""

    seconds: int
    source: str


@dataclass(frozen=True)
class UsersConfig:
    admins: tuple[int, ...]
    standard: tuple[int, ...]


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str


@dataclass(frozen=True)
class RyotCollectionsConfig:
    archived: str
    owned: str
    radarr: str
    sonarr: str


@dataclass(frozen=True)
class RyotConfig:
    url: str
    api_key: str
    scan_interval: Duration
    collections: RyotCollectionsConfig


@dataclass(frozen=True)
class ArrConfig:
    url: str
    api_key: str
    default_profile: str
    root_folder: str
    exclusion_sync: bool


@dataclass(frozen=True)
class JellyfinReceptionConfig:
    enabled: bool
    first_check_delay: Duration
    retry_interval: Duration
    timeout: Duration


@dataclass(frozen=True)
class JellyfinConfig:
    url: str
    api_key: str
    reception_checks: JellyfinReceptionConfig


@dataclass(frozen=True)
class PolicyConfig:
    default_approval_mode: str


@dataclass(frozen=True)
class IdentityConfig:
    weak_match_action: str


@dataclass(frozen=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True)
class CuratarrConfig:
    users: UsersConfig
    telegram: TelegramConfig
    ryot: RyotConfig
    radarr: ArrConfig
    sonarr: ArrConfig
    jellyfin: JellyfinConfig
    policy: PolicyConfig
    identity: IdentityConfig
    logging: LoggingConfig


def load_config(path: str | Path) -> CuratarrConfig:
    """Load, expand, and validate a Curatarr YAML configuration file."""

    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read config file {config_path}: {exc}") from exc

    try:
        raw_data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise ConfigError("Configuration root must be a YAML mapping.")

    raw_data = _expand_env_in_value(raw_data)
    return parse_config(raw_data)


def expand_env(text: str, environ: dict[str, str] | None = None) -> str:
    """Expand `${ENV_NAME}` placeholders and fail if a referenced value is missing."""

    source = os.environ if environ is None else environ

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in source:
            raise ConfigError(f"Missing required environment variable: {name}")
        return source[name]

    return _ENV_PATTERN.sub(replace, text)


def _expand_env_in_value(value: Any) -> Any:
    if isinstance(value, str):
        return expand_env(value)
    if isinstance(value, list):
        return [_expand_env_in_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env_in_value(item) for key, item in value.items()}
    return value


def parse_duration(value: Any) -> Duration:
    """Parse duration strings such as `30m`, `24h`, or `7d`."""

    if not isinstance(value, str):
        raise ConfigError(f"Duration must be a string, got {type(value).__name__}.")
    match = _DURATION_PATTERN.match(value.strip())
    if not match:
        raise ConfigError(f"Invalid duration {value!r}; expected formats like 30m, 24h, or 7d.")
    amount = int(match.group("value"))
    unit = match.group("unit")
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return Duration(seconds=amount * multiplier, source=value)


def parse_config(data: dict[str, Any]) -> CuratarrConfig:
    """Parse a raw mapping into the typed Curatarr config model."""

    return CuratarrConfig(
        users=_parse_users(_require_mapping(data, "users")),
        telegram=_parse_telegram(_require_mapping(data, "telegram")),
        ryot=_parse_ryot(_require_mapping(data, "ryot")),
        radarr=_parse_arr(_require_mapping(data, "radarr"), "radarr"),
        sonarr=_parse_arr(_require_mapping(data, "sonarr"), "sonarr"),
        jellyfin=_parse_jellyfin(_require_mapping(data, "jellyfin")),
        policy=_parse_policy(_require_mapping(data, "policy")),
        identity=_parse_identity(_require_mapping(data, "identity")),
        logging=_parse_logging(_require_mapping(data, "logging")),
    )


def _parse_users(data: dict[str, Any]) -> UsersConfig:
    return UsersConfig(
        admins=_tuple_of_ints(data.get("admins", ()), "users.admins"),
        standard=_tuple_of_ints(data.get("standard", ()), "users.standard"),
    )


def _parse_telegram(data: dict[str, Any]) -> TelegramConfig:
    return TelegramConfig(bot_token=_require_str(data, "bot_token", "telegram.bot_token"))


def _parse_ryot(data: dict[str, Any]) -> RyotConfig:
    collections = _require_mapping(data, "collections", "ryot.collections")
    return RyotConfig(
        url=_require_str(data, "url", "ryot.url"),
        api_key=_require_str(data, "api_key", "ryot.api_key"),
        scan_interval=parse_duration(data.get("scan_interval", "30m")),
        collections=RyotCollectionsConfig(
            archived=_require_str(collections, "archived", "ryot.collections.archived"),
            owned=_require_str(collections, "owned", "ryot.collections.owned"),
            radarr=_require_str(collections, "radarr", "ryot.collections.radarr"),
            sonarr=_require_str(collections, "sonarr", "ryot.collections.sonarr"),
        ),
    )


def _parse_arr(data: dict[str, Any], section: str) -> ArrConfig:
    return ArrConfig(
        url=_require_str(data, "url", f"{section}.url"),
        api_key=_require_str(data, "api_key", f"{section}.api_key"),
        default_profile=_require_str(data, "default_profile", f"{section}.default_profile"),
        root_folder=_require_str(data, "root_folder", f"{section}.root_folder"),
        exclusion_sync=bool(data.get("exclusion_sync", False)),
    )


def _parse_jellyfin(data: dict[str, Any]) -> JellyfinConfig:
    checks = _require_mapping(data, "reception_checks", "jellyfin.reception_checks")
    return JellyfinConfig(
        url=_require_str(data, "url", "jellyfin.url"),
        api_key=_require_str(data, "api_key", "jellyfin.api_key"),
        reception_checks=JellyfinReceptionConfig(
            enabled=bool(checks.get("enabled", True)),
            first_check_delay=parse_duration(checks.get("first_check_delay", "5m")),
            retry_interval=parse_duration(checks.get("retry_interval", "15m")),
            timeout=parse_duration(checks.get("timeout", "2h")),
        ),
    )


def _parse_policy(data: dict[str, Any]) -> PolicyConfig:
    approvals = _require_mapping(data, "approvals", "policy.approvals")
    return PolicyConfig(
        default_approval_mode=_require_str(
            approvals,
            "default_mode",
            "policy.approvals.default_mode",
        )
    )


def _parse_identity(data: dict[str, Any]) -> IdentityConfig:
    return IdentityConfig(
        weak_match_action=_require_str(data, "weak_match_action", "identity.weak_match_action")
    )


def _parse_logging(data: dict[str, Any]) -> LoggingConfig:
    return LoggingConfig(level=str(data.get("level", "INFO")).upper())


def _require_mapping(
    data: dict[str, Any],
    key: str,
    path: str | None = None,
) -> dict[str, Any]:
    value = data.get(key)
    label = key if path is None else path
    if not isinstance(value, dict):
        raise ConfigError(f"Missing or invalid mapping: {label}")
    return value


def _require_str(data: dict[str, Any], key: str, path: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Missing or invalid string: {path}")
    return value


def _tuple_of_ints(value: Any, path: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be a list of integer Telegram user IDs.")
    result = []
    for item in value:
        if not isinstance(item, int):
            raise ConfigError(f"{path} must contain only integer Telegram user IDs.")
        result.append(item)
    return tuple(result)
