"""Run an agent along a route: skip agents in cooldown, hand off on usage limits."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TextIO

from baton.adapter import AgentResult, make_adapter
from baton.config import AgentConfig, ConfigError
from baton.git import handoff_context, head
from baton.limits import LimitStore
from baton.prompt import handoff_prompt
from baton.review import run_isolated_review
from baton.router import Route
from baton.runs import RunStore, budget_reset_message, daily_counts


def say(out: TextIO, text: str = "") -> None:
    print(text, file=out)


class RoutedAdapter:
    """One invocation's route and actual participants, including partial quota attempts.

    ``route.tier`` retains the effective tier even after a handoff. Participants are
    configured identities, so later fix/review selection can inspect their providers.
    Cooldown skips and argv previews are not attempts.
    """

    def __init__(
        self, route: Route, store: LimitStore, out: TextIO, *, review: bool = False
    ) -> None:
        self.route = route
        self.review = review
        self.attempted_agents: list[AgentConfig] = []
        self.exhausted = False  # set by run(): every agent of the route was skipped or limited
        self.store = store
        self.out = out
        self.runs = RunStore()

    def _eligible(self):
        seen = set()
        for agent in self.route.agents:
            if agent.name in seen:
                continue
            seen.add(agent.name)
            deadline = self.store.active().get(agent.name)
            if deadline is not None:
                say(self.out, f"Skipping {agent.name}: cooldown until {deadline.isoformat()}.")
                continue
            yield agent

    def _announce(self, agent) -> None:
        say(self.out, f"Tier {self.route.tier}, agent {agent.name} ({agent.adapter})")

    def argv(self, prompt: str) -> list[str]:
        agent, _ = self.runs.select(
            list(self._eligible()), weighted=any(a.weight is not None for a in self.route.agents)
        )
        if agent is not None:
            self._announce(agent)
            return make_adapter(agent).argv(prompt)
        raise ConfigError(self._exhaustion_message())

    def _exhaustion_message(self):
        counts = daily_counts(self.runs.read(), datetime.datetime.now(datetime.UTC))
        if any(
            a.daily_run_budget is not None and counts.get(a.name, 0) >= a.daily_run_budget
            for a in self.route.agents
        ):
            return budget_reset_message()
        return self._exhausted()

    @staticmethod
    def _exhausted() -> str:
        return (
            "No available agents remain in this route. Changes stay in the working tree; "
            "the step stays open. Wait for a cooldown to expire or sign in manually. "
            "Review and preserve partial edits before retrying; baton next requires a clean "
            "tree for an open step."
        )

    def _handoff(self, prompt: str, previous: str, repo: Path) -> str:
        """The prompt for the successor of a limited agent."""
        return handoff_prompt(prompt, previous, handoff_context(repo))

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        before = None if self.review else head(repo)
        previous = None
        self.exhausted = False
        remaining = list(self._eligible())
        while remaining:
            agent, attempt = self.runs.select(
                remaining, claim=True, weighted=any(a.weight is not None for a in self.route.agents)
            )
            if agent is None:
                break
            remaining = [a for a in remaining if a.name != agent.name]
            self._announce(agent)
            current_prompt = prompt
            if previous is not None and not self.review:
                current_prompt = self._handoff(prompt, previous, repo)
            adapter = make_adapter(agent)
            self.attempted_agents.append(agent)
            result = (
                run_isolated_review(adapter, current_prompt, timeout)
                if self.review
                else adapter.run(current_prompt, repo, timeout)
            )
            self.runs.complete(attempt, result)
            # Check even failed attempts, before persisting a cooldown or trying a successor.
            if not self.review and head(repo) != before:
                return AgentResult(ok=False, error="agent moved HEAD")
            if result.ok or result.limit is None:
                return result
            now = datetime.datetime.now(datetime.UTC)
            deadline = result.limit.reset_at
            fallback = deadline is None or deadline <= now
            if fallback:
                deadline = now + datetime.timedelta(hours=1)
            self.store.set(agent.name, deadline)
            note = " (1h fallback: no usable reset time)" if fallback else ""
            say(
                self.out,
                f"Agent {agent.name} reached its usage limit; "
                f"cooldown until {deadline.isoformat()}{note}.",
            )
            previous = agent.name
        self.exhausted = True
        return AgentResult(ok=False, error=self._exhaustion_message())
