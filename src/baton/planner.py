"""``baton plan``: turn a roadmap milestone into ``.agent-plan.md`` with a headless planner.

Mirrors the ``/plan-commits`` skill: select the milestone -> prompt the planner -> validate ->
one retry with the validation errors -> write the plan, or stop without touching it.
"""

from __future__ import annotations

import datetime
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from baton.adapter import Adapter, AgentResult
from baton.config import ConfigError, find_config_path, load_config
from baton.git import (
    PLAN_FILENAME,
    GitError,
    current_branch,
    dirty_paths,
    ensure_excluded,
    head,
    is_tracked,
)
from baton.limits import LimitStateError, LimitStore
from baton.next import DEFAULT_AGENT_TIMEOUT, EXIT_FAILED, EXIT_OK, EXIT_STOPPED
from baton.plan import MilestoneRef, PlanError, Tracking, parse_plan
from baton.plan_check import PlanHeader, assemble_plan, validate_plan
from baton.prompt import plan_prompt, plan_retry_prompt
from baton.roadmap import (
    Milestone,
    Roadmap,
    RoadmapError,
    find_roadmap,
    next_milestone,
    parse_roadmap,
    save_roadmap,
    set_milestone_status,
)
from baton.routed import RoutedAdapter, say
from baton.router import resolve_route

ATTEMPTS = 2  # the first plan plus one retry with the validation errors


class RoutedPlanner(RoutedAdapter):
    """A route for planning: a successor after a usage limit simply starts the plan afresh."""

    def _handoff(self, prompt: str, previous: str, repo: Path) -> str:
        return prompt

    @staticmethod
    def _exhausted() -> str:
        return (
            "No available agents remain in this route. No plan was written. "
            "Wait for a cooldown to expire or sign in manually, then run `baton plan` again."
        )


class _Stop(Exception):
    """End the run with ``code`` after ``message`` was reported."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


def _select(roadmap: Roadmap, milestone_id: str | None) -> Milestone:
    if milestone_id is None:
        milestone = next_milestone(roadmap)
        if milestone is None:
            raise _Stop(EXIT_STOPPED, "No open milestone whose dependencies are all done.")
        return milestone
    milestone = roadmap.get(milestone_id)
    if milestone is None:
        raise _Stop(EXIT_FAILED, f"The roadmap has no milestone {milestone_id}.")
    if milestone.status == "done":
        raise _Stop(EXIT_STOPPED, f"{milestone.id} is already done.")
    if milestone.status == "skipped":
        raise _Stop(EXIT_STOPPED, f"{milestone.id} is skipped in the roadmap.")
    done = {m.id for m in roadmap.milestones if m.status == "done"}
    missing = [dep for dep in milestone.depends_on if dep not in done]
    if missing:
        raise _Stop(EXIT_STOPPED, f"{milestone.id} depends on {', '.join(missing)} (not done).")
    return milestone


def _check_existing_plan(path: Path) -> None:
    if not path.is_file():
        return
    try:
        plan = parse_plan(path.read_text(encoding="utf-8"))
    except PlanError as exc:
        raise _Stop(EXIT_STOPPED, f"{PLAN_FILENAME} exists but does not parse ({exc}).") from exc
    if any(s.status in ("open", "pending") for s in plan.steps):
        raise _Stop(
            EXIT_STOPPED,
            f"{PLAN_FILENAME} still has open or pending steps. Finish or delete it first.",
        )


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _restore(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(content)


def _run_planner(
    adapter: Adapter, prompt: str, repo: Path, timeout: float, plan_path: Path
) -> AgentResult:
    """Run the planner and reject any change it made to HEAD, the tree or the plan file."""
    before = head(repo)
    plan_before = _read(plan_path)
    result = adapter.run(prompt, repo, timeout)
    if _read(plan_path) != plan_before:
        _restore(plan_path, plan_before)
        raise _Stop(EXIT_FAILED, f"The planner changed {PLAN_FILENAME}; restored it. No plan.")
    after = head(repo)
    if after != before:
        raise _Stop(EXIT_FAILED, f"HEAD moved from {before} to {after}: the planner committed.")
    changed = dirty_paths(repo)
    if changed:
        listing = "".join(f"\n  {p}" for p in changed)
        raise _Stop(
            EXIT_FAILED, f"The planner changed the working tree; review it yourself:{listing}"
        )
    if not result.ok:
        raise _Stop(EXIT_FAILED, f"The planner failed: {result.error}")
    return result


@dataclass(frozen=True)
class _Target:
    roadmap_path: Path
    roadmap_text: str
    rel: str  # the roadmap path relative to the repository root
    tracking: Tracking
    milestone: Milestone

    def prompt(self) -> str:
        return plan_prompt(self.roadmap_text, self.milestone, self.rel, self.tracking)


def _target(repo: Path, milestone_id: str | None) -> _Target:
    roadmap_path = find_roadmap(repo)
    if roadmap_path is None:
        raise _Stop(EXIT_STOPPED, f"No docs/ROADMAP.md or ROADMAP.md in {repo}.")
    text = roadmap_path.read_text(encoding="utf-8")
    milestone = _select(parse_roadmap(text), milestone_id)
    rel = roadmap_path.relative_to(repo).as_posix()
    tracking: Tracking = "tracked" if is_tracked(repo, rel) else "local"
    return _Target(roadmap_path, text, rel, tracking, milestone)


_ERRORS = (ConfigError, GitError, LimitStateError, PlanError, RoadmapError, OSError)


def run_plan(
    repo: Path,
    adapter: Adapter,
    out: TextIO,
    *,
    milestone_id: str | None = None,
    today: str | None = None,
    timeout: float = DEFAULT_AGENT_TIMEOUT,
) -> int:
    """Plan the next open (or the given) milestone of ``repo``; returns the process exit code."""
    return _run_plan(
        repo,
        lambda milestone: adapter,
        out,
        milestone_id=milestone_id,
        today=today,
        timeout=timeout,
    )


def _run_plan(
    repo: Path,
    select_adapter: Callable[[Milestone], Adapter],
    out: TextIO,
    *,
    milestone_id: str | None,
    today: str | None,
    timeout: float,
) -> int:
    date = today or datetime.date.today().isoformat()
    plan_path = repo / PLAN_FILENAME
    try:
        target = _target(repo, milestone_id)
        milestone = target.milestone
        _check_existing_plan(plan_path)
        dirty = dirty_paths(repo)
        if dirty:
            listing = "".join(f"\n  {p}" for p in dirty)
            raise _Stop(EXIT_STOPPED, f"The working tree has uncommitted changes:{listing}")

        say(out, f"Planning {milestone.id} – {milestone.name} ({target.rel}, {target.tracking})")
        header = PlanHeader(
            created=date,
            branch=current_branch(repo) or "(detached)",
            base=head(repo),
            milestone=MilestoneRef(milestone.id, milestone.name, target.rel, target.tracking),
        )
        adapter = select_adapter(milestone)

        prompt = target.prompt()
        current = prompt
        errors: list[str] = []
        for attempt in range(1, ATTEMPTS + 1):
            result = _run_planner(adapter, current, repo, timeout, plan_path)
            text = assemble_plan(result.text, header)
            errors = validate_plan(text, milestone.id)
            if not errors:
                break
            say(out, f"Attempt {attempt}: the plan was rejected:")
            for error in errors:
                say(out, f"  {error}")
            current = plan_retry_prompt(prompt, result.text, errors)
        if errors:
            raise _Stop(EXIT_FAILED, f"No valid plan after {ATTEMPTS} attempts. Nothing written.")

        with plan_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        ensure_excluded(repo)
        if target.tracking == "local" and milestone.status == "open":
            updated = set_milestone_status(target.roadmap_text, milestone.id, "pending")
            save_roadmap(target.roadmap_path, updated)
        plan = parse_plan(text)
        say(out, f"Wrote {PLAN_FILENAME}: {plan.title} ({len(plan.steps)} steps)")
        for step in plan.steps:
            say(out, f"  {step.number}. {step.message} [{step.size}]")
        say(out, "Review the plan, then run `baton next`.")
        return EXIT_OK
    except _Stop as stop:
        say(out, str(stop))
        return stop.code
    except _ERRORS as exc:
        say(out, f"Error: {exc}")
        return EXIT_FAILED


def run_plan_command(
    repo: Path,
    *,
    config_path: str | None = None,
    milestone_id: str | None = None,
    agent_name: str | None = None,
    tier: str | None = None,
    timeout: float = DEFAULT_AGENT_TIMEOUT,
    dry_run: bool = False,
    out: TextIO | None = None,
) -> int:
    """CLI entry: route ``plan_commits`` by the milestone's size and plan it (or preview it)."""
    out = out or sys.stdout

    def select_adapter(milestone: Milestone) -> RoutedPlanner:
        config = load_config(find_config_path(config_path))
        route = resolve_route(
            config, "plan_commits", tier=tier, step_size=milestone.size, agent_name=agent_name
        )
        return RoutedPlanner(route, LimitStore(), out)

    if not dry_run:
        return _run_plan(
            repo, select_adapter, out, milestone_id=milestone_id, today=None, timeout=timeout
        )
    try:
        target = _target(repo, milestone_id)
        milestone = target.milestone
        say(out, f"Milestone: {milestone.id} – {milestone.name} ({target.rel}, {target.tracking})")
        adapter = select_adapter(milestone)
        say(out, "Command (cwd = repo root):")
        say(out, "  " + " ".join(shlex.quote(a) for a in adapter.argv("<prompt>")))
        say(out, "Prompt:")
        say(out, target.prompt())
        return EXIT_OK
    except _Stop as stop:
        say(out, str(stop))
        return stop.code
    except _ERRORS as exc:
        say(out, f"Error: {exc}")
        return EXIT_FAILED
