"""``baton auto``: work through the roadmap, one milestone after the other, without review stops.

``run_milestone`` is the unit of the loop: plan -> every step -> pull request -> merge ->
fast-forward the base checkout. The commands it drives are injected, so tests need no agents.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, TextIO

from baton.autostate import AutoStore
from baton.git import (
    PLAN_FILENAME,
    GitError,
    current_branch,
    dirty_paths,
    fetch,
    ff_merge,
    milestone_branch,
    worktree_path_for,
    worktree_paths,
    worktree_remove,
)
from baton.next import EXIT_EXHAUSTED, EXIT_OK, EXIT_ROADMAP_PROBLEM
from baton.plan import Plan, PlanError, first_open_step, parse_plan
from baton.roadmap import RoadmapError, find_roadmap, next_milestone, parse_roadmap
from baton.routed import say

Outcome = Literal["merged", "published", "done", "stopped"]


@dataclass(frozen=True)
class MilestoneResult:
    """How one milestone run ended; ``reason`` explains a stop."""

    outcome: Outcome
    milestone: str = ""
    reason: str = ""
    pr_url: str = ""


@dataclass
class Budget:
    """Limits of one auto run, shared by all its milestones."""

    started: datetime
    max_steps: int | None = None
    max_hours: float | None = None
    steps: int = 0

    def exceeded(self, now: datetime) -> str | None:
        if self.max_steps is not None and self.steps >= self.max_steps:
            return f"budget reached: {self.steps} steps (--max-steps {self.max_steps})"
        if self.max_hours is not None and now - self.started >= timedelta(hours=self.max_hours):
            return f"budget reached: {self.max_hours:g} hours (--max-hours)"
        return None


@dataclass(frozen=True)
class MilestoneHooks:
    """What ``run_milestone`` drives. Each command returns its process exit code."""

    plan: Callable[[], int]
    next: Callable[[bool], int]  # argument: resume from a partial diff
    pr: Callable[[bool], tuple[int, str]]  # argument: merge; returns (exit code, output)
    wait: Callable[[], Literal["ready", "stopped"]]  # blocks while every agent is in cooldown
    now: Callable[[], datetime]
    after_merge: Callable[[Path, Plan], str | None] | None = None


def ff_base(repo: Path, plan: Plan) -> str | None:
    """Fast-forward the base checkout to origin and drop the milestone worktree.

    Returns a reason when the base checkout cannot be updated safely, else ``None``.
    """
    base = plan.branch
    try:
        if not base or base == milestone_branch(plan):
            return "the plan does not name a base branch"
        if current_branch(repo) != base:
            return f"the checkout is not on the base branch {base}"
        dirty = dirty_paths(repo)
        if dirty:
            return f"the base checkout has uncommitted changes: {', '.join(dirty)}"
        fetch(repo)
        ff_merge(repo, f"origin/{base}")
    except GitError as exc:
        return f"cannot fast-forward {base}: {' '.join(str(exc).split())}"
    path = worktree_path_for(repo, milestone_branch(plan))
    if path.resolve() in {p.resolve() for p in worktree_paths(repo)}:
        # A leftover worktree is harmless; the next run never reuses a merged branch.
        with suppress(GitError):
            worktree_remove(repo, path)
    return None


def _read_plan(repo: Path) -> Plan | None:
    path = repo / PLAN_FILENAME
    return parse_plan(path.read_text(encoding="utf-8")) if path.is_file() else None


def _is_open(plan: Plan | None) -> bool:
    return plan is not None and any(s.status in ("open", "pending") for s in plan.steps)


def run_milestone(
    repo: Path,
    hooks: MilestoneHooks,
    store: AutoStore,
    budget: Budget,
    *,
    merge: bool = True,
    out: TextIO,
) -> MilestoneResult:
    """Run the next milestone to a merged base, or stop with the reason."""

    def stop(reason: str, milestone: str = "") -> MilestoneResult:
        say(out, f"Stopped: {reason}")
        return MilestoneResult("stopped", milestone, reason)

    def stop_requested() -> bool:
        return store.read(repo).stop_requested

    try:
        plan = _read_plan(repo)
        state = store.read(repo)
        if plan is None or not state.milestone:
            plan = _plan_next(repo, hooks, store, out, stop_requested)
            if isinstance(plan, MilestoneResult):
                return plan
        assert plan is not None
        milestone = plan.milestone.id or plan.title
        say(out, f"Milestone {milestone}: {plan.title}")

        while True:
            plan = _read_plan(repo)
            assert plan is not None
            step = first_open_step(plan)
            if step is None:
                break
            if stop_requested():
                return stop("stop requested", milestone)
            if (reason := budget.exceeded(hooks.now())) is not None:
                return stop(reason, milestone)
            # The same step still recorded as in progress: continue from its partial diff.
            resume = store.read(repo).step == step.number
            store.update(repo, step=step.number)
            say(out, f"Step {step.number}/{len(plan.steps)}: {step.message}")
            code = hooks.next(resume)
            if code == EXIT_EXHAUSTED:
                if hooks.wait() == "stopped":
                    return stop("stop requested while waiting for a cooldown", milestone)
                continue
            if code == EXIT_ROADMAP_PROBLEM:
                return stop(f"roadmap problem reported at step {step.number}", milestone)
            if code != EXIT_OK:
                return stop(
                    f"step {step.number} failed after escalation (exit code {code}); "
                    "see the plan log",
                    milestone,
                )
            after = _read_plan(repo)
            if after is None or (nxt := first_open_step(after)) and nxt.number <= step.number:
                return stop(f"step {step.number} made no progress", milestone)
            budget.steps += 1
            store.update(repo, step=0, steps_done=store.read(repo).steps_done + 1)

        code, output = hooks.pr(merge)
        say(out, output.rstrip())
        url = next((m[0] for m in re.finditer(r"https?://\S+", output)), "")
        if code != EXIT_OK:
            return stop(f"pull request not merged: {_last_line(output)}", milestone)
        store.update(repo, pr_url=url)
        if not merge:
            return MilestoneResult("published", milestone, pr_url=url)
        failure = (hooks.after_merge or ff_base)(repo, plan)
        if failure is not None:
            return stop(failure, milestone)
        store.update(repo, milestone="", step=0, branch="", pr_url="")
        return MilestoneResult("merged", milestone, pr_url=url)
    except (PlanError, RoadmapError, GitError, OSError) as exc:
        return stop(f"{exc}")


def _last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no details"


def _plan_next(
    repo: Path,
    hooks: MilestoneHooks,
    store: AutoStore,
    out: TextIO,
    stop_requested: Callable[[], bool],
) -> Plan | MilestoneResult:
    """Adopt an open plan or plan the next roadmap milestone; a result ends the milestone."""
    existing = _read_plan(repo)
    if not _is_open(existing):
        roadmap_path = find_roadmap(repo)
        if roadmap_path is None:
            return MilestoneResult("stopped", reason=f"no roadmap in {repo}")
        roadmap = parse_roadmap(roadmap_path.read_text(encoding="utf-8"))
        if next_milestone(roadmap) is None:
            if all(m.status in ("done", "skipped") for m in roadmap.milestones):
                return MilestoneResult("done")
            return MilestoneResult(
                "stopped", reason="no open milestone whose dependencies are all done"
            )
        while True:
            if stop_requested():
                return MilestoneResult("stopped", reason="stop requested")
            code = hooks.plan()
            if code == EXIT_OK:
                break
            if code == EXIT_EXHAUSTED:
                if hooks.wait() == "stopped":
                    return MilestoneResult(
                        "stopped", reason="stop requested while waiting for a cooldown"
                    )
                continue
            if code == EXIT_ROADMAP_PROBLEM:
                return MilestoneResult("stopped", reason="roadmap problem reported by the planner")
            return MilestoneResult("stopped", reason=f"planning failed (exit code {code})")
    plan = _read_plan(repo)
    if not _is_open(plan):
        return MilestoneResult("stopped", reason=f"planning left no open steps in {PLAN_FILENAME}")
    assert plan is not None
    store.update(
        repo,
        milestone=plan.milestone.id or plan.title,
        branch=milestone_branch(plan),
        step=0,
    )
    return plan
