"""``baton next``: implement one plan step with an agent, verify it, and stop for the commit.

Mirrors the ``/next`` skill: gate -> agent -> HEAD check -> verify -> ``[~]`` marking -> report.
By default baton never commits; the user reviews and commits. With ``--commit`` a verified
step without critical review findings is committed with the plan's message and marked ``[x]``.
"""

from __future__ import annotations

import datetime
import shlex
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from baton.adapter import Adapter, AgentResult, CommandAdapter, make_adapter
from baton.config import AgentConfig, ConfigError, find_config_path, load_config
from baton.gate import GateResult, apply_gate, check_gate
from baton.git import (
    PLAN_FILENAME,
    GitError,
    branch_exists,
    commit_all,
    current_branch,
    dirty_paths,
    handoff_context,
    head,
    milestone_branch,
    switch_branch,
)
from baton.limits import LimitStateError, LimitStore
from baton.milestone import sync_milestone
from baton.plan import (
    Plan,
    PlanError,
    Step,
    append_log,
    first_open_step,
    parse_plan,
    save_plan,
    set_step_status,
)
from baton.prompt import fix_prompt, handoff_prompt, implement_prompt, review_prompt, step_text
from baton.review import (
    SEVERITIES,
    ReviewError,
    ReviewResult,
    parse_review,
    review_context,
    run_isolated_review,
)
from baton.roadmap import RoadmapError
from baton.router import Route, resolve_route
from baton.verify import VerifyResult, run_verify

EXIT_OK = 0
EXIT_FAILED = 1  # agent, verify or plan problem; changes stay in the tree
EXIT_STOPPED = 2  # nothing was started (gate, no open step, dirty tree)

DEFAULT_AGENT_TIMEOUT = 1800.0
DEFAULT_VERIFY_TIMEOUT = 900.0


def _say(out: TextIO, text: str = "") -> None:
    print(text, file=out)


def _commit_hint(out: TextIO, message: str) -> None:
    _say(out, "  git add -A")
    _say(out, f"  git commit -m {_quote(message)}")


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def _log(plan_path: Path, line: str, date: str) -> None:
    """Append a Log line to the plan as it is now on disk."""
    text = plan_path.read_text(encoding="utf-8")
    save_plan(plan_path, append_log(text, f"{date}: {line}"))


def _handle_gate(
    plan: Plan, gate: GateResult, plan_path: Path, repo: Path, out: TextIO, date: str
) -> int | None:
    """Return an exit code when the gate stops the run, ``None`` to continue."""
    if gate.state == "blocked_dirty":
        pending = next(s for s in plan.steps if s.number == gate.step)
        _say(out, f"Step {gate.step} is implemented but not committed. Not starting new work.")
        _say(out, "Changed files:")
        for path in gate.dirty:
            _say(out, f"  {path}")
        _say(out, "Review with `git diff`, then commit:")
        _commit_hint(out, pending.message)
        _say(out, "Run `baton next` again after committing.")
        return EXIT_STOPPED
    if gate.state == "discarded":
        _say(out, f"Step {gate.step} is marked [~] but HEAD is unchanged and the tree is clean.")
        _say(out, "The changes were discarded or stashed. Not starting new work.")
        _say(out, "Fix the plan (re-open or skip the step) and run `baton next` again.")
        return EXIT_STOPPED
    if gate.state in ("committed", "overridden"):
        text = plan_path.read_text(encoding="utf-8")
        save_plan(plan_path, apply_gate(text, gate, date))
        _say(out, f"Step {gate.step} marked done (HEAD {gate.head}).")
        return None
    dirty = dirty_paths(repo)
    if dirty:
        _say(out, "The working tree has uncommitted changes but no step is marked [~].")
        _say(out, "Commit or stash them first, so the agent's changes can be reviewed alone:")
        for path in dirty:
            _say(out, f"  {path}")
        return EXIT_STOPPED
    return None


def _report_success(
    out: TextIO,
    plan: Plan,
    step: Step,
    repo: Path,
    verify: VerifyResult,
    cost: float | None,
    review: ReviewResult | None = None,
    *,
    changed: list[str] | None = None,
    committed: str | None = None,
    notice: str | None = None,
) -> None:
    _say(out)
    _say(out, f"Step {step.number}/{len(plan.steps)} done: {step.message}")
    _say(out, "Changed:")
    for path in dirty_paths(repo) if changed is None else changed:
        _say(out, f"  {path}")
    _say(out, "Checks:")
    if verify.verified:
        _say(out, f"  {verify.command} -> passed")
    else:
        _say(out, "  (the step has no Verify command: not verified)")
    if cost is not None:
        _say(out, f"Agent cost: ${cost:.4f}")
    if review is not None:
        _say(out, f"Independent reviewer: {review.reviewer.name} ({review.reviewer.adapter})")
        if not review.findings:
            _say(out, "  No findings.")
        for severity in SEVERITIES:
            findings = [finding for finding in review.findings if finding.severity == severity]
            if findings:
                _say(out, f"  {severity.upper()}:")
            for finding in findings:
                location = finding.path or ""
                if finding.line is not None:
                    location += f":{finding.line}"
                _say(out, f"    {location + ': ' if location else ''}{finding.message}")
    if committed is not None:
        _say(out, f"Committed: {committed} {step.message}")
    else:
        if notice:
            _say(out, notice)
        _say(out, "Review: `git diff`")
        _say(out, "Commit:")
        _commit_hint(out, step.message)
    following = next((s for s in plan.steps if s.number > step.number and s.status == "open"), None)
    if following:
        when = "" if committed is not None else "after committing, "
        _say(out, f"Next: {when}run `baton next` for step {following.number}:")
        _say(out, f"  {following.message}")
    else:
        if committed is not None:
            _say(out, "Next: that was the last step; run `baton next` once more to finish.")
        else:
            _say(out, "Next: last open step. After committing, run `baton next` once more.")


def _enter_branch(repo: Path, plan: Plan, out: TextIO) -> bool:
    """Make the milestone branch current (creating it from HEAD); False if the tree is dirty."""
    branch = milestone_branch(plan)
    if current_branch(repo) == branch:
        _say(out, f"Branch: {branch}")
        return True
    dirty = dirty_paths(repo)
    if dirty:
        _say(out, f"Cannot switch to {branch}: the working tree has uncommitted changes.")
        for path in dirty:
            _say(out, f"  {path}")
        _say(out, "Commit or stash them, then run `baton next --commit` again.")
        return False
    exists = branch_exists(repo, branch)
    switch_branch(repo, branch, create=not exists)
    _say(out, f"Branch: {branch} ({'switched to' if exists else 'created from ' + head(repo)})")
    return True


def _verify_failure(verify: VerifyResult) -> str:
    if verify.timed_out:
        return "timed out"
    return f"exit {verify.exit_code}"


def run_next(
    repo: Path,
    adapter: Adapter,
    out: TextIO,
    *,
    agent_timeout: float = DEFAULT_AGENT_TIMEOUT,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
    override: bool = False,
    today: str | None = None,
    commit: bool = False,
) -> int:
    """Implement the first open step of ``repo``'s plan; returns the process exit code."""
    return _run_next(
        repo,
        lambda step: adapter,
        out,
        agent_timeout=agent_timeout,
        verify_timeout=verify_timeout,
        override=override,
        today=today,
        commit=commit,
    )


def _run_next(
    repo: Path,
    select_adapter: Callable[[Step], Adapter],
    out: TextIO,
    *,
    agent_timeout: float,
    verify_timeout: float,
    override: bool = False,
    today: str | None = None,
    select_fix: Callable[[Step, bool], Adapter | None] | None = None,
    review_step: Callable[[Plan, Step, str], ReviewResult | None] | None = None,
    commit: bool = False,
) -> int:
    date = today or datetime.date.today().isoformat()
    plan_path = repo / PLAN_FILENAME
    try:
        if not plan_path.is_file():
            _say(out, f"No {PLAN_FILENAME} in {repo}. Create one with `/plan-commits <task>`.")
            return EXIT_STOPPED
        plan = parse_plan(plan_path.read_text(encoding="utf-8"))

        gate = check_gate(plan, repo, override)
        stop = _handle_gate(plan, gate, plan_path, repo, out, date)
        if stop is not None:
            return stop
        if gate.state in ("committed", "overridden"):
            plan = parse_plan(plan_path.read_text(encoding="utf-8"))

        step = first_open_step(plan)
        if step is None:
            _say(out, "No open steps left in the plan.")
            return EXIT_STOPPED

        _say(out, f"Step {step.number}/{len(plan.steps)}: {step.message}")
        if commit and not _enter_branch(repo, plan, out):
            return EXIT_STOPPED
        adapter = select_adapter(step)
        raw_step = step_text(plan_path.read_text("utf-8"), step)
        prompt = implement_prompt(plan, step, raw_step)
        before = head(repo)

        result = adapter.run(prompt, repo, agent_timeout)
        after = head(repo)
        if after != before:
            _say(out, f"HEAD moved from {before} to {after}: the agent committed. Step not marked.")
            _log(plan_path, f"step {step.number}: agent moved HEAD {before} -> {after}", date)
            return EXIT_FAILED

        if not result.ok:
            _say(out, f"The agent failed: {result.error}")
            _say(out, "Changes (if any) stay in the working tree; the step stays open.")
            _log(plan_path, f"step {step.number}: agent failed ({result.error})", date)
            return EXIT_FAILED

        verify = run_verify(step.verify, repo, verify_timeout)
        repairs = 0
        for escalated in (False, True):
            if not verify.verified or verify.passed:
                break
            _say(out, f"Verify failed ({_verify_failure(verify)}): {verify.command}")
            _say(out, verify.output_tail)
            fixer = select_fix(step, escalated) if select_fix is not None else None
            if fixer is None:
                break
            repairs += 1
            label = "Escalating repair to tier L." if escalated else "Attempting same-tier repair."
            _say(out, label)
            repair = fixer.run(fix_prompt(prompt, verify), repo, agent_timeout)
            after = head(repo)
            if after != before:
                _say(out, f"HEAD moved from {before} to {after}: fixer committed. Step not marked.")
                _log(plan_path, f"step {step.number}: fixer moved HEAD {before} -> {after}", date)
                return EXIT_FAILED
            if not repair.ok:
                _say(out, f"The fix agent failed: {repair.error}")
                _say(out, "Changes stay in the working tree; the step stays open.")
                _log(plan_path, f"step {step.number}: fix agent failed ({repair.error})", date)
                return EXIT_FAILED
            verify = run_verify(step.verify, repo, verify_timeout)
            cost = (
                result.cost + repair.cost
                if result.cost is not None and repair.cost is not None
                else None
            )
            result = replace(result, cost=cost)
        if verify.verified and not verify.passed:
            _say(out, f"Verify failed ({_verify_failure(verify)}): {verify.command}")
            _say(out, verify.output_tail)
            _say(out, "Changes stay in the working tree; the step stays open.")
            _log(
                plan_path,
                f"step {step.number}: verify failed ({_verify_failure(verify)}) "
                f"after {repairs} repair attempts",
                date,
            )
            return EXIT_FAILED

        review = None
        if review_step is not None:
            try:
                review = review_step(plan, step, raw_step)
            except (ReviewError, ConfigError, GitError, LimitStateError, OSError) as exc:
                _say(out, f"Independent review failed: {exc}")
                _say(out, "Changes stay in the working tree; the step stays open.")
                _log(plan_path, f"step {step.number}: independent review failed ({exc})", date)
                return EXIT_FAILED

        changed = dirty_paths(repo)
        committed = None
        notice = None
        problem = None
        if commit:
            if review is not None and any(f.severity == "critical" for f in review.findings):
                problem = "critical review findings"
                notice = "Auto-commit blocked by critical review findings; decide manually."
            else:
                try:
                    sync_milestone(repo, plan, step, date)
                    committed = commit_all(repo, step.message)
                except (RoadmapError, GitError) as exc:
                    problem = f"commit failed ({exc})"
                    notice = f"Auto-commit failed: {exc}\nChanges stay staged; the step stays [~]."

        # Re-read: the agent may have touched the plan file despite the rules.
        text = plan_path.read_text(encoding="utf-8")
        text = set_step_status(text, step.number, "pending", after)
        note = "implemented" if verify.verified else "implemented (no Verify command)"
        if repairs:
            note += f" after {repairs} repair attempts"
        if review is not None:
            note += f"; {review.summary()}"
        if committed is not None:
            text = set_step_status(text, step.number, "done", committed)
            text = append_log(text, f"{date}: step {step.number} {note}, committed ({committed}).")
        elif problem is not None:
            text = append_log(text, f"{date}: step {step.number} {note}; auto-commit: {problem}.")
        else:
            text = append_log(text, f"{date}: step {step.number} {note}, waiting for commit.")
        save_plan(plan_path, text)
    except (ConfigError, GitError, LimitStateError, PlanError, OSError) as exc:
        _say(out, f"Error: {exc}")
        return EXIT_FAILED

    _report_success(
        out,
        plan,
        step,
        repo,
        verify,
        result.cost,
        review,
        changed=changed,
        committed=committed,
        notice=notice,
    )
    return EXIT_FAILED if problem else EXIT_OK


def _dry_run(
    repo: Path,
    select_adapter: Callable[[Step], CommandAdapter | _RoutedAdapter],
    out: TextIO,
    *,
    commit: bool = False,
) -> int:
    """Print the prompt and argv for the first open step; change nothing."""
    plan_path = repo / PLAN_FILENAME
    if not plan_path.is_file():
        _say(out, f"No {PLAN_FILENAME} in {repo}. Create one with `/plan-commits <task>`.")
        return EXIT_STOPPED
    text = plan_path.read_text(encoding="utf-8")
    plan = parse_plan(text)
    gate = check_gate(plan, repo)
    if gate.state in ("blocked_dirty", "discarded"):
        _say(out, f"Gate: {gate.state}; no agent selected.")
        return EXIT_STOPPED
    if gate.state == "committed":
        text = apply_gate(text, gate, datetime.date.today().isoformat())
        plan = parse_plan(text)
    step = first_open_step(plan)
    if step is None:
        _say(out, "No open steps left in the plan.")
        return EXIT_STOPPED
    prompt = implement_prompt(plan, step, step_text(text, step))
    _say(out, f"Step {step.number}/{len(plan.steps)}: {step.message}")
    if commit:
        branch = milestone_branch(plan)
        if current_branch(repo) == branch:
            state = "current"
        else:
            state = "exists, would switch" if branch_exists(repo, branch) else "would be created"
        _say(out, f"Branch: {branch} ({state})")
    adapter = select_adapter(step)
    _say(out, "Command (cwd = repo root):")
    _say(out, "  " + " ".join(shlex.quote(a) for a in adapter.argv("<prompt>")))
    _say(out, "Prompt:")
    _say(out, prompt)
    return EXIT_OK


class _RoutedAdapter:
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
        self.store = store
        self.out = out

    def _eligible(self):
        seen = set()
        for agent in self.route.agents:
            if agent.name in seen:
                continue
            seen.add(agent.name)
            deadline = self.store.active().get(agent.name)
            if deadline is not None:
                _say(self.out, f"Skipping {agent.name}: cooldown until {deadline.isoformat()}.")
                continue
            yield agent

    def _announce(self, agent) -> None:
        _say(self.out, f"Tier {self.route.tier}, agent {agent.name} ({agent.adapter})")

    def argv(self, prompt: str) -> list[str]:
        for agent in self._eligible():
            self._announce(agent)
            return make_adapter(agent).argv(prompt)
        raise ConfigError(self._exhausted())

    @staticmethod
    def _exhausted() -> str:
        return (
            "No available agents remain in this route. Changes stay in the working tree; "
            "the step stays open. Wait for a cooldown to expire or sign in manually. "
            "Review and preserve partial edits before retrying; baton next requires a clean "
            "tree for an open step."
        )

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        before = None if self.review else head(repo)
        previous = None
        for agent in self._eligible():
            self._announce(agent)
            current_prompt = prompt
            if previous is not None and not self.review:
                current_prompt = handoff_prompt(prompt, previous, handoff_context(repo))
            adapter = make_adapter(agent)
            self.attempted_agents.append(agent)
            result = (
                run_isolated_review(adapter, current_prompt, timeout)
                if self.review
                else adapter.run(current_prompt, repo, timeout)
            )
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
            _say(
                self.out,
                f"Agent {agent.name} reached its usage limit; "
                f"cooldown until {deadline.isoformat()}{note}.",
            )
            previous = agent.name
        return AgentResult(ok=False, error=self._exhausted())


def run_next_command(
    repo: Path,
    *,
    config_path: str | None = None,
    agent_name: str | None = None,
    tier: str | None = None,
    agent_timeout: float = DEFAULT_AGENT_TIMEOUT,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
    dry_run: bool = False,
    commit: bool = False,
    out: TextIO | None = None,
) -> int:
    """CLI entry: select an agent for the step being run or previewed."""
    out = out or sys.stdout

    config = None
    implementation = None

    def select_adapter(step: Step) -> _RoutedAdapter:
        nonlocal config, implementation
        config = load_config(find_config_path(config_path))
        route = resolve_route(
            config, "implement", tier=tier, step_size=step.size, agent_name=agent_name
        )
        implementation = _RoutedAdapter(route, LimitStore(), out)
        return implementation

    def select_fix(step: Step, escalated: bool) -> Adapter | None:
        # Legacy configs have no role routes; injected callers likewise keep their behavior.
        assert config is not None and implementation is not None
        if not config.routes:
            return None
        route = resolve_route(config, "fix", tier="L" if escalated else implementation.route.tier)
        fixer = _RoutedAdapter(route, implementation.store, out)
        fixer.attempted_agents = implementation.attempted_agents
        return fixer

    def review_step(plan: Plan, step: Step, raw_step: str) -> ReviewResult | None:
        assert config is not None and implementation is not None
        if not config.routes:
            return None
        route = resolve_route(
            config,
            "review",
            tier=implementation.route.tier,
            authors=tuple(implementation.attempted_agents),
        )
        reviewer = _RoutedAdapter(route, implementation.store, out, review=True)
        before = head(repo)
        changes = review_context(repo)
        result = reviewer.run(review_prompt(plan, step, raw_step, changes), repo, agent_timeout)
        if head(repo) != before or review_context(repo) != changes:
            raise ReviewError("Candidate changed during review; review is stale")
        if not result.ok:
            raise ReviewError(result.error)
        return ReviewResult(reviewer.attempted_agents[-1], parse_review(result.text))

    if dry_run:
        try:
            return _dry_run(repo, select_adapter, out, commit=commit)
        except (ConfigError, GitError, LimitStateError, PlanError, OSError) as exc:
            _say(out, f"Error: {exc}")
            return EXIT_FAILED
    return _run_next(
        repo,
        select_adapter,
        out,
        agent_timeout=agent_timeout,
        verify_timeout=verify_timeout,
        select_fix=select_fix,
        review_step=review_step,
        commit=commit,
    )
