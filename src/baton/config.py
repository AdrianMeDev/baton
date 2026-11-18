"""Loading and validating baton's ``config.toml``."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

ADAPTERS = ("claude", "codex", "agy", "pi")
ROUTE_ROLES = ("plan_roadmap", "plan_commits", "implement", "fix", "review")
ROUTE_TIERS = ("S", "M", "L")
NOTIFIERS = ("none", "notify-send", "ntfy")
ENV_VAR = "BATON_CONFIG"


class ConfigError(Exception):
    """Raised when the configuration cannot be found, parsed or validated."""


@dataclass(frozen=True)
class ConfigLocation:
    path: Path
    origin: str  # "flag" | "env" | "xdg" | "home" | "bundled"

    @property
    def is_bundled(self) -> bool:
        return self.origin == "bundled"


@dataclass(frozen=True)
class AgentConfig:
    name: str
    adapter: str
    command: str
    model: str | None = None
    provider: str | None = None
    limit_patterns: tuple[str, ...] | None = None
    daily_run_budget: int | None = None
    weight: int | None = None


@dataclass(frozen=True)
class AutoConfig:
    """``[auto]`` defaults for `baton auto`; every key is optional."""

    notify: str = "none"
    ntfy_url: str | None = None
    merge: bool = True
    max_steps: int | None = None
    max_hours: float | None = None


@dataclass(frozen=True)
class Config:
    location: ConfigLocation
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    routes: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    auto: AutoConfig = field(default_factory=AutoConfig)


def bundled_config_path() -> Path:
    return Path(str(resources.files("baton").joinpath("config.example.toml")))


def find_config_path(
    explicit: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ConfigLocation:
    """Resolve the config file: flag > $BATON_CONFIG > XDG > ~/.config > bundled example."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home

    if explicit:
        return ConfigLocation(Path(explicit), "flag")
    if env.get(ENV_VAR):
        return ConfigLocation(Path(env[ENV_VAR]), "env")
    if env.get("XDG_CONFIG_HOME"):
        xdg = Path(env["XDG_CONFIG_HOME"]) / "baton" / "config.toml"
        if xdg.is_file():
            return ConfigLocation(xdg, "xdg")
    default = home / ".config" / "baton" / "config.toml"
    if default.is_file():
        return ConfigLocation(default, "home")
    return ConfigLocation(bundled_config_path(), "bundled")


def load_config(location: ConfigLocation) -> Config:
    path = location.path
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"{path}: config file not found") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config file: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    raw_agents = data.get("agents", {})
    if not isinstance(raw_agents, dict):
        raise ConfigError(f"{path}: 'agents' must be a table")

    agents = {name: _parse_agent(path, name, raw) for name, raw in raw_agents.items()}
    routes = _parse_routes(path, data.get("routes", {}), agents)
    auto = _parse_auto(path, data.get("auto", {}))
    return Config(location=location, agents=agents, routes=routes, auto=auto)


def _parse_auto(path: Path, raw: object) -> AutoConfig:
    where = f"{path}: [auto]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(raw) - {"notify", "ntfy_url", "merge", "max_steps", "max_hours"}
    if unknown:
        raise ConfigError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
    notify = _require_str(raw, "notify", where) if "notify" in raw else "none"
    if notify not in NOTIFIERS:
        raise ConfigError(f"{where}: 'notify' must be one of: {', '.join(NOTIFIERS)}")
    ntfy_url = _require_str(raw, "ntfy_url", where) if "ntfy_url" in raw else None
    if ntfy_url is not None and not re.match(r"https?://\S+$", ntfy_url):
        raise ConfigError(f"{where}: 'ntfy_url' must be an http(s) URL")
    if notify == "ntfy" and ntfy_url is None:
        raise ConfigError(f"{where}: notify = \"ntfy\" requires 'ntfy_url'")
    merge = raw.get("merge", True)
    if not isinstance(merge, bool):
        raise ConfigError(f"{where}: 'merge' must be true or false")
    max_steps = raw.get("max_steps")
    if "max_steps" in raw and (
        isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1
    ):
        raise ConfigError(f"{where}: 'max_steps' must be a positive integer")
    max_hours = raw.get("max_hours")
    if "max_hours" in raw:
        if isinstance(max_hours, bool) or not isinstance(max_hours, int | float) or max_hours <= 0:
            raise ConfigError(f"{where}: 'max_hours' must be a positive number")
        max_hours = float(max_hours)
    return AutoConfig(notify, ntfy_url, merge, max_steps, max_hours)


def _parse_agent(path: Path, name: str, raw: object) -> AgentConfig:
    where = f"{path}: [agents.{name}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table")

    adapter = _require_str(raw, "adapter", where)
    if adapter not in ADAPTERS:
        raise ConfigError(
            f"{where}: unknown adapter {adapter!r} (expected one of: {', '.join(ADAPTERS)})"
        )
    command = _require_str(raw, "command", where)

    model = _require_str(raw, "model", where) if "model" in raw else None
    provider = _require_str(raw, "provider", where) if "provider" in raw else None
    weight = raw.get("weight")
    if "weight" in raw and (type(weight) is not int or weight < 1):
        raise ConfigError(f"{where}: weight must be a positive integer")
    daily_run_budget = raw.get("daily_run_budget")
    if "daily_run_budget" in raw and (type(daily_run_budget) is not int or daily_run_budget < 1):
        raise ConfigError(f"{where}: daily_run_budget must be a positive integer")
    patterns = raw.get("limit_patterns")
    if "limit_patterns" in raw:
        if not isinstance(patterns, list) or not patterns:
            raise ConfigError(
                f"{where}: 'limit_patterns' must be a non-empty array of regex strings"
            )
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                raise ConfigError(f"{where}: limit patterns must be non-empty regex strings")
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ConfigError(f"{where}: invalid limit pattern: {exc}") from exc
            if compiled.search(""):
                raise ConfigError(f"{where}: limit pattern must not match empty text")
    return AgentConfig(
        name=name,
        adapter=adapter,
        command=command,
        model=model,
        provider=provider,
        daily_run_budget=daily_run_budget,
        weight=weight,
        limit_patterns=tuple(patterns) if patterns is not None else None,
    )


def _parse_routes(
    path: Path, raw: object, agents: Mapping[str, AgentConfig]
) -> dict[str, dict[str, list[str]]]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'routes' must be a table")

    routes: dict[str, dict[str, list[str]]] = {}
    for role, raw_tiers in raw.items():
        where = f"{path}: [routes.{role}]"
        if role not in ROUTE_ROLES:
            raise ConfigError(f"{where}: unknown role (expected one of: {', '.join(ROUTE_ROLES)})")
        if not isinstance(raw_tiers, dict):
            raise ConfigError(f"{where} must be a table")

        tiers: dict[str, list[str]] = {}
        for tier, chain in raw_tiers.items():
            where = f"{path}: routes.{role}.{tier}"
            if tier not in ROUTE_TIERS:
                raise ConfigError(f"{where}: unknown tier (expected one of: S, M, L)")
            if not isinstance(chain, list) or not chain:
                raise ConfigError(f"{where} must be a non-empty array of agent names")
            for name in chain:
                if not isinstance(name, str) or not name.strip():
                    raise ConfigError(f"{where}: agent names must be non-empty strings")
                if name not in agents:
                    raise ConfigError(f"{where}: unknown agent {name!r}")
            tiers[tier] = chain
        routes[role] = tiers

    return routes


def _require_str(raw: dict[str, object], key: str, where: str) -> str:
    if key not in raw:
        raise ConfigError(f"{where}: missing required key '{key}'")
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: '{key}' must be a non-empty string")
    return value
