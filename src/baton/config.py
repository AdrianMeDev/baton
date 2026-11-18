"""Loading and validating baton's ``config.toml``."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

ADAPTERS = ("claude", "codex", "agy", "pi")
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


@dataclass(frozen=True)
class Config:
    location: ConfigLocation
    agents: dict[str, AgentConfig] = field(default_factory=dict)


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
    return Config(location=location, agents=agents)


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

    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigError(f"{where}: 'model' must be a non-empty string")
    return AgentConfig(name=name, adapter=adapter, command=command, model=model)


def _require_str(raw: dict[str, object], key: str, where: str) -> str:
    if key not in raw:
        raise ConfigError(f"{where}: missing required key '{key}'")
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: '{key}' must be a non-empty string")
    return value
