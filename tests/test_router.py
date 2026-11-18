from pathlib import Path

import pytest

from baton.config import (
    ROUTE_ROLES,
    ROUTE_TIERS,
    AgentConfig,
    Config,
    ConfigError,
    ConfigLocation,
    bundled_config_path,
    load_config,
)
from baton.router import Route, resolve_route

CLAUDE = AgentConfig("sonnet", "claude", "claude", "sonnet")
OPUS = AgentConfig("opus", "claude", "claude", "opus")
CODEX = AgentConfig("codex", "codex", "codex")
ARGON = AgentConfig("argon", "agy", "agy", provider="google")
PI_CLAUDE = AgentConfig("pi-claude", "pi", "pi", provider=" Anthropic ")
PI_UNKNOWN = AgentConfig("pi-unknown", "pi", "pi")


@pytest.fixture
def config() -> Config:
    return Config(
        ConfigLocation(Path("example.toml"), "flag"),
        agents={agent.name: agent for agent in (CODEX, CLAUDE, OPUS, ARGON, PI_CLAUDE, PI_UNKNOWN)},
        routes={
            "plan_roadmap": {"L": ["opus", "argon"]},
            "plan_commits": {"S": ["sonnet", "argon"]},
            "implement": {"S": ["sonnet"], "M": ["codex"], "L": ["argon"]},
            "fix": {"M": ["codex", "sonnet"]},
            "review": {"M": ["pi-claude", "opus", "pi-unknown", "argon", "codex"]},
        },
    )


@pytest.mark.parametrize(
    ("role", "tier", "agents"),
    [
        ("plan_roadmap", "L", (OPUS, ARGON)),
        ("plan_commits", "S", (CLAUDE, ARGON)),
        ("implement", "M", (CODEX,)),
        ("fix", "M", (CODEX, CLAUDE)),
        ("review", "M", (PI_CLAUDE, OPUS, PI_UNKNOWN, ARGON, CODEX)),
    ],
)
def test_resolve_each_role(
    config: Config, role: str, tier: str, agents: tuple[AgentConfig, ...]
) -> None:
    assert resolve_route(config, role, tier=tier) == Route(tier, agents)


def test_tier_precedence(config: Config) -> None:
    assert resolve_route(config, "implement") == Route("M", (CODEX,))
    assert resolve_route(config, "implement", step_size="S") == Route("S", (CLAUDE,))
    assert resolve_route(config, "implement", tier="L", step_size="S") == Route("L", (ARGON,))


def test_explicit_agent_bypasses_missing_route(config: Config) -> None:
    assert resolve_route(config, "fix", tier="L", agent_name="opus") == Route("L", (OPUS,))


@pytest.mark.parametrize(
    ("role", "tier", "agent_name", "error"),
    [
        ("fix", "L", None, r"configure \[routes\.fix\] L"),
        ("plan_roadmap", "M", None, r"configure \[routes\.plan_roadmap\] M"),
        ("implement", "S", "oops", "unknown agent 'oops'"),
        ("implement", "XL", None, "unknown tier 'XL'"),
        ("deploy", "M", None, "unknown role 'deploy'"),
    ],
)
def test_invalid_selection(
    config: Config, role: str, tier: str, agent_name: str | None, error: str
) -> None:
    with pytest.raises(ConfigError, match=rf"example\.toml: .*{error}"):
        resolve_route(config, role, tier=tier, agent_name=agent_name)


def test_legacy_implementation_picks_first_claude(config: Config) -> None:
    legacy = Config(config.location, agents=config.agents)
    assert resolve_route(legacy, "implement", step_size="L") == Route("L", (CLAUDE,))
    assert resolve_route(legacy, "implement", agent_name="codex") == Route("M", (CODEX,))
    with pytest.raises(ConfigError, match=r"configure \[routes\.fix\] M"):
        resolve_route(legacy, "fix")


def test_legacy_implementation_requires_claude(config: Config) -> None:
    legacy = Config(config.location, agents={"codex": CODEX})
    with pytest.raises(ConfigError, match='no agent with adapter = "claude" configured'):
        resolve_route(legacy, "implement")


def test_review_filters_provider_aliases_in_route_order(config: Config) -> None:
    assert resolve_route(config, "review", implementer=CLAUDE) == Route("M", (ARGON, CODEX))
    assert resolve_route(config, "review", implementer=PI_CLAUDE) == Route("M", (ARGON, CODEX))


def test_explicit_reviewer_must_be_eligible(config: Config) -> None:
    assert resolve_route(config, "review", agent_name="codex", implementer=CLAUDE) == Route(
        "M", (CODEX,)
    )
    with pytest.raises(ConfigError, match="no eligible reviewer.*anthropic"):
        resolve_route(config, "review", agent_name="pi-claude", implementer=CLAUDE)


def test_review_route_with_no_eligible_agents(config: Config) -> None:
    same_provider = Config(
        config.location, config.agents, {"review": {"M": ["opus", "pi-claude", "pi-unknown"]}}
    )
    with pytest.raises(ConfigError, match="no eligible reviewer.*anthropic"):
        resolve_route(same_provider, "review", implementer=CLAUDE)


def test_review_requires_implementer_provider_for_multi_provider_adapter(config: Config) -> None:
    with pytest.raises(ConfigError, match=r"set 'provider' in \[agents\.pi-unknown\]"):
        resolve_route(config, "review", implementer=PI_UNKNOWN)


@pytest.fixture
def bundled() -> Config:
    return load_config(ConfigLocation(bundled_config_path(), "bundled"))


@pytest.mark.parametrize("role", ROUTE_ROLES)
@pytest.mark.parametrize("tier", ROUTE_TIERS)
def test_bundled_routes_cover_every_role_and_tier(bundled: Config, role: str, tier: str) -> None:
    route = resolve_route(bundled, role, tier=tier)
    assert route.tier == tier
    assert [agent.name for agent in route.agents] == bundled.routes[role][tier]


@pytest.mark.parametrize(
    ("role", "tier", "names"),
    [
        ("plan_roadmap", "S", ["claude-opus", "codex-astra", "agy"]),
        ("plan_commits", "S", ["claude-sonnet", "agy"]),
        ("plan_commits", "M", ["claude-sonnet", "agy"]),
        ("plan_commits", "L", ["claude-opus", "codex-astra", "agy"]),
        ("implement", "S", ["pi-ollama", "claude-haiku"]),
        ("implement", "M", ["claude-sonnet", "codex"]),
        ("implement", "L", ["claude-sonnet", "agy", "codex"]),
    ],
)
def test_bundled_route_order(bundled: Config, role: str, tier: str, names: list[str]) -> None:
    assert [agent.name for agent in resolve_route(bundled, role, tier=tier).agents] == names


@pytest.mark.parametrize(
    ("implementer", "reviewers"),
    [
        ("claude-sonnet", ["codex-astra", "agy"]),
        ("codex", ["claude-opus", "agy"]),
        ("agy", ["claude-opus", "codex-astra"]),
    ],
)
def test_bundled_review_is_cross_provider(
    bundled: Config, implementer: str, reviewers: list[str]
) -> None:
    route = resolve_route(bundled, "review", implementer=bundled.agents[implementer])
    assert [agent.name for agent in route.agents] == reviewers


def test_review_excludes_all_contributing_providers(config):
    assert resolve_route(config, "review", authors=(CLAUDE, CODEX, PI_CLAUDE)) == Route(
        "M", (ARGON,)
    )
    # The legacy implementer argument combines with authors rather than replacing them.
    assert resolve_route(config, "review", implementer=CODEX, authors=(PI_CLAUDE,)) == Route(
        "M", (ARGON,)
    )


def test_all_author_providers_exhaust_review_route(config):
    with pytest.raises(ConfigError, match="no eligible reviewer"):
        resolve_route(config, "review", authors=(CLAUDE, CODEX, ARGON))


def test_unknown_author_identity_is_not_silently_ignored(config):
    with pytest.raises(ConfigError, match=r"set 'provider' in \[agents.pi-unknown\]"):
        resolve_route(config, "review", authors=(CLAUDE, PI_UNKNOWN))


@pytest.mark.parametrize("provider", [" OpenAI ", "openai", "OPENAI"])
def test_author_provider_aliases_and_explicit_reviewer(config, provider):
    author = AgentConfig("custom", "pi", "pi", provider=provider)
    with pytest.raises(ConfigError, match="no eligible reviewer"):
        resolve_route(config, "review", agent_name="codex", authors=(author,))
    assert resolve_route(config, "review", agent_name="argon", authors=(author,)) == Route(
        "M", (ARGON,)
    )


@pytest.mark.parametrize("provider", [None, "", "  "])
def test_unknown_reviewer_cannot_be_selected(config, provider):
    unknown = AgentConfig("unknown", "pi", "pi", provider=provider)
    changed = Config(config.location, {**config.agents, "unknown": unknown}, config.routes)
    with pytest.raises(ConfigError, match="no eligible reviewer"):
        resolve_route(changed, "review", agent_name="unknown", authors=(CLAUDE,))
