"""Resolve configured agent chains without starting an agent."""

from __future__ import annotations

from dataclasses import dataclass

from baton.config import ROUTE_ROLES, ROUTE_TIERS, AgentConfig, Config, ConfigError

_DEFAULT_PROVIDERS = {"claude": "anthropic", "codex": "openai"}


@dataclass(frozen=True)
class Route:
    tier: str
    agents: tuple[AgentConfig, ...]


def pick_claude_agent(config: Config) -> AgentConfig:
    """Pick the first Claude agent for legacy implementation configs."""
    for agent in config.agents.values():
        if agent.adapter == "claude":
            return agent
    raise ConfigError(f'{config.location.path}: no agent with adapter = "claude" configured')


def resolve_route(
    config: Config,
    role: str,
    *,
    tier: str | None = None,
    step_size: str | None = None,
    agent_name: str | None = None,
    implementer: AgentConfig | None = None,
    authors: tuple[AgentConfig, ...] = (),
) -> Route:
    """Resolve overrides, tier defaults and provider eligibility for one invocation."""
    path = config.location.path
    if role not in ROUTE_ROLES:
        raise ConfigError(f"{path}: unknown role {role!r} (expected {', '.join(ROUTE_ROLES)})")

    effective_tier = tier if tier is not None else (step_size or "M")
    if effective_tier not in ROUTE_TIERS:
        raise ConfigError(f"{path}: unknown tier {effective_tier!r} (expected S, M, L)")

    if agent_name is not None:
        if agent_name not in config.agents:
            raise ConfigError(f"{path}: unknown agent {agent_name!r}; choose a name from [agents]")
        agents = (config.agents[agent_name],)
    elif not config.routes and role == "implement":
        agents = (pick_claude_agent(config),)
    else:
        names = config.routes.get(role, {}).get(effective_tier)
        if not names:
            raise ConfigError(f"{path}: missing route; configure [routes.{role}] {effective_tier}")
        agents = tuple(config.agents[name] for name in names)

    if role == "review" and (implementer is not None or authors):
        participants = authors + ((implementer,) if implementer is not None else ())
        agents = _eligible_reviewers(config, agents, participants)
    return Route(effective_tier, agents)


def _provider(agent: AgentConfig) -> str | None:
    if agent.provider is not None:
        return agent.provider.strip().casefold() or None
    return _DEFAULT_PROVIDERS.get(agent.adapter)


def _eligible_reviewers(
    config: Config, agents: tuple[AgentConfig, ...], authors: tuple[AgentConfig, ...]
) -> tuple[AgentConfig, ...]:
    path = config.location.path
    providers = set()
    for author in authors:
        provider = _provider(author)
        if provider is None:
            raise ConfigError(f"{path}: set 'provider' in [agents.{author.name}] for review")
        providers.add(provider)

    eligible = tuple(agent for agent in agents if _provider(agent) not in (None, *providers))
    if not eligible:
        names = ", ".join(repr(author.name) for author in authors)
        raise ConfigError(
            f"{path}: no eligible reviewer for {names}; "
            f"select a reviewer with a known provider other than {', '.join(sorted(providers))}"
        )
    return eligible
