from pathlib import Path

import pytest

from baton.cli import build_parser
from baton.config import (
    ConfigError,
    ConfigLocation,
    bundled_config_path,
    find_config_path,
    load_config,
)

VALID = """
[agents.a]
adapter = "claude"
command = "claude"
model = "opus"

[agents.b]
adapter = "codex"
command = "codex"
"""


def write(path: Path, text: str = VALID) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def load_text(tmp_path: Path, text: str):
    return load_config(ConfigLocation(write(tmp_path / "c.toml", text), "flag"))


def test_flag_beats_env_and_files(tmp_path: Path) -> None:
    write(tmp_path / "home" / ".config" / "baton" / "config.toml")
    loc = find_config_path("x.toml", {"BATON_CONFIG": "env.toml"}, tmp_path / "home")
    assert (loc.origin, loc.path) == ("flag", Path("x.toml"))


def test_env_beats_xdg_and_home(tmp_path: Path) -> None:
    write(tmp_path / "xdg" / "baton" / "config.toml")
    env = {"BATON_CONFIG": "env.toml", "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    loc = find_config_path(None, env, tmp_path)
    assert (loc.origin, loc.path) == ("env", Path("env.toml"))


def test_xdg_beats_home(tmp_path: Path) -> None:
    xdg = write(tmp_path / "xdg" / "baton" / "config.toml")
    write(tmp_path / "home" / ".config" / "baton" / "config.toml")
    loc = find_config_path(None, {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, tmp_path / "home")
    assert (loc.origin, loc.path) == ("xdg", xdg)


def test_home_used_when_present(tmp_path: Path) -> None:
    home_cfg = write(tmp_path / ".config" / "baton" / "config.toml")
    loc = find_config_path(None, {}, tmp_path)
    assert (loc.origin, loc.path) == ("home", home_cfg)


def test_falls_back_to_bundled_example(tmp_path: Path) -> None:
    loc = find_config_path(None, {"XDG_CONFIG_HOME": str(tmp_path / "none")}, tmp_path)
    assert loc.is_bundled
    assert loc.path == bundled_config_path()


def test_valid_file(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, VALID)
    assert cfg.agents["a"].model == "opus"
    assert cfg.agents["b"].model is None
    assert cfg.agents["b"].command == "codex"
    assert cfg.agents["b"].provider is None
    assert cfg.routes == {}


def test_unknown_top_level_keys_are_ignored(tmp_path: Path) -> None:
    cfg = load_text(tmp_path, VALID + "\n[future]\nkey = true\n")
    assert set(cfg.agents) == {"a", "b"}


def test_partial_routes_preserve_chain_order(tmp_path: Path) -> None:
    cfg = load_text(
        tmp_path,
        """
[routes.plan_roadmap]
L = ["b", "a"]
[routes.plan_commits]
S = ["a"]
[routes.implement]
M = ["a", "b"]
L = ["b", "a"]
[routes.fix]
S = ["b"]
[routes.review]
M = ["b", "a"]
"""
        + VALID
        + 'provider = "openai"\n',
    )
    assert cfg.agents["b"].provider == "openai"
    assert cfg.routes == {
        "plan_roadmap": {"L": ["b", "a"]},
        "plan_commits": {"S": ["a"]},
        "implement": {"M": ["a", "b"], "L": ["b", "a"]},
        "fix": {"S": ["b"]},
        "review": {"M": ["b", "a"]},
    }


@pytest.mark.parametrize(
    ("routes", "key", "error"),
    [
        ("routes = 1", "routes", "must be a table"),
        ('[routes]\nplan = ["a"]', "routes.plan", "unknown role"),
        ('[routes]\nimplement = ["a"]', "routes.implement", "must be a table"),
        ('[routes.fix]\nXL = ["a"]', "routes.fix.XL", "unknown tier"),
        ('[routes.fix]\nM = "a"', "routes.fix.M", "non-empty array"),
        ("[routes.fix]\nM = []", "routes.fix.M", "non-empty array"),
        ('[routes.fix]\nM = ["a", 2]', "routes.fix.M", "non-empty strings"),
        ('[routes.fix]\nM = [" "]', "routes.fix.M", "non-empty strings"),
        ('[routes.fix]\nM = ["a", "missing"]', "routes.fix.M", "unknown agent 'missing'"),
    ],
)
def test_invalid_routes(tmp_path: Path, routes: str, key: str, error: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_text(tmp_path, routes + "\n" + VALID)
    message = str(exc.value)
    assert str(tmp_path / "c.toml") in message
    assert key in message
    assert error in message


@pytest.mark.parametrize("provider", ['" "', "3"])
def test_invalid_provider(tmp_path: Path, provider: str) -> None:
    with pytest.raises(ConfigError, match=r"\[agents\.b\].*'provider' must be a non-empty string"):
        load_text(tmp_path, VALID + f"provider = {provider}\n")


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(ConfigLocation(tmp_path / "nope.toml", "flag"))


def test_broken_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_text(tmp_path, "[agents.a\n")


@pytest.mark.parametrize("missing", ["adapter", "command"])
def test_missing_required_key(tmp_path: Path, missing: str) -> None:
    fields = {"adapter": 'adapter = "claude"', "command": 'command = "claude"'}
    fields.pop(missing)
    text = "[agents.a]\n" + "\n".join(fields.values())
    with pytest.raises(ConfigError, match=rf"\[agents\.a\].*missing required key '{missing}'"):
        load_text(tmp_path, text)


def test_unknown_adapter(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown adapter 'gpt'"):
        load_text(tmp_path, '[agents.a]\nadapter = "gpt"\ncommand = "x"\n')


def test_wrong_types(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="'command' must be a non-empty string"):
        load_text(tmp_path, '[agents.a]\nadapter = "pi"\ncommand = 3\n')
    with pytest.raises(ConfigError, match="'model' must be a non-empty string"):
        load_text(tmp_path, '[agents.a]\nadapter = "pi"\ncommand = "pi"\nmodel = ""\n')
    with pytest.raises(ConfigError, match="'agents' must be a table"):
        load_text(tmp_path, "agents = 1\n")
    with pytest.raises(ConfigError, match="must be a table"):
        load_text(tmp_path, "[agents]\na = 1\n")


def test_bundled_example_is_valid() -> None:
    cfg = load_config(ConfigLocation(bundled_config_path(), "bundled"))
    assert {"claude-opus", "codex", "agy", "pi-ollama", "pi-openrouter"} <= set(cfg.agents)


def test_cli_accepts_config_option() -> None:
    assert build_parser().parse_args(["--config", "x.toml"]).config == "x.toml"
