"""``baton auto``: work through the roadmap, one milestone after the other, without review stops.

``run_milestone`` is the unit of the loop: plan -> every step -> pull request -> merge ->
fast-forward the base checkout. The commands it drives are injected, so tests need no agents.
"""

from __future__ import annotations

import io
import re
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TextIO

from baton.autostate import AutoStateError, AutoStore
from baton.autowait import wait_for_reset
from baton.config import Config, ConfigError, find_config_path, load_config
from baton.git import (
    PLAN_FILENAME,
    GitError,
    current_branch,
    dirty_paths,
    fetch,
    ff_merge,
    is_repo,
    milestone_branch,
    worktree_path_for,
    worktree_paths,
    worktree_remove,
)
from baton.limits import LimitStateError, LimitStore
from baton.next import (
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_VERIFY_TIMEOUT,
    EXIT_EXHAUSTED,
    EXIT_OK,
    EXIT_ROADMAP_PROBLEM,
    EXIT_STOPPED,
    run_next_command,
)
from baton.notify import notify
from baton.plan import Plan, PlanError, Step, parse_plan
from baton.planner import run_plan_command
from baton.pr import run_pr
from baton.roadmap import (
    RoadmapError,
    find_roadmap,
    next_milestone,
    parse_roadmap,
    save_roadmap,
    set_milestone_status,
)
from baton.routed import say
from baton.router import resolve_route

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
    # Blocks while every agent of the role's route is in cooldown; arguments: role, size S/M/L.
    wait: Callable[[str, str | None], str]  # ready, stopped, or budget reset diagnostic
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


def _unfinished(plan: Plan) -> Step | None:
    """The first step that is still open or waiting for its commit to be confirmed."""
    return next((s for s in plan.steps if s.status in ("open", "pending")), None)


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
        if not _adoptable(repo, plan):
            plan = _plan_next(repo, hooks, out, stop_requested, budget)
            if isinstance(plan, MilestoneResult):
                return plan
        assert plan is not None
        store.update(repo, milestone=plan.milestone.id or plan.title, branch=milestone_branch(plan))
        milestone = plan.milestone.id or plan.title
        say(out, f"Milestone {milestone}: {plan.title}")

        while True:
            plan = _read_plan(repo)
            assert plan is not None
            step = _unfinished(plan)
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
                if (waiting := hooks.wait("implement", step.size)) != "ready":
                    if waiting != "stopped":
                        return stop(waiting, milestone)
                    return stop("stop requested while waiting for a cooldown", milestone)
                continue
            if code == EXIT_ROADMAP_PROBLEM:
                return stop(f"roadmap problem reported at step {step.number}", milestone)
            if code not in (EXIT_OK, EXIT_STOPPED):
                return stop(
                    f"step {step.number} failed after escalation (exit code {code}); "
                    "see the plan log",
                    milestone,
                )
            after = _read_plan(repo)
            current = _unfinished(after) if after is not None else step
            # A committed [~] step is only settled by the gate, which then reports "stopped".
            if current is not None and (current.number, current.status) == (
                step.number,
                step.status,
            ):
                detail = "made no progress" if code == EXIT_OK else f"was not started (exit {code})"
                return stop(f"step {step.number} {detail}; see the output above", milestone)
            if code == EXIT_OK:
                budget.steps += 1
                store.update(repo, step=0, steps_done=store.read(repo).steps_done + 1)

        if stop_requested():
            return stop("stop requested", milestone)
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
        _finish_local_roadmap(repo, plan)
        store.update(repo, milestone="", step=0, branch="", pr_url="")
        return MilestoneResult("merged", milestone, pr_url=url)
    except (PlanError, RoadmapError, GitError, LimitStateError, OSError) as exc:
        return stop(f"{exc}")


def _last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no details"


def _adoptable(repo: Path, plan: Plan | None) -> bool:
    """Whether the plan on disk is this run's current milestone rather than a merged leftover."""
    if plan is None:
        return False
    if _is_open(plan):
        return True
    ref = plan.milestone
    if ref.id is None:
        return False
    path = find_roadmap(repo)
    if path is None:
        return False
    found = parse_roadmap(path.read_text(encoding="utf-8")).get(ref.id)
    # Finished steps whose milestone the base still shows as not done: published or merged,
    # but not yet fast-forwarded. `baton pr` recognizes an already merged pull request.
    return found is not None and found.status != "done"


def _finish_local_roadmap(repo: Path, plan: Plan) -> None:
    """A local (untracked) roadmap is not part of the merge; mark its milestone done here."""
    ref = plan.milestone
    path = find_roadmap(repo)
    if ref.tracking != "local" or ref.id is None or path is None:
        return
    text = path.read_text(encoding="utf-8")
    save_roadmap(path, set_milestone_status(text, ref.id, "done"))


def _plan_next(
    repo: Path,
    hooks: MilestoneHooks,
    out: TextIO,
    stop_requested: Callable[[], bool],
    budget: Budget,
) -> Plan | MilestoneResult:
    """Plan the next roadmap milestone; a result ends the milestone."""
    roadmap_path = find_roadmap(repo)
    if roadmap_path is None:
        return MilestoneResult("stopped", reason=f"no roadmap in {repo}")
    roadmap = parse_roadmap(roadmap_path.read_text(encoding="utf-8"))
    milestone = next_milestone(roadmap)
    if milestone is None:
        if all(m.status in ("done", "skipped") for m in roadmap.milestones):
            return MilestoneResult("done")
        return MilestoneResult(
            "stopped", reason="no open milestone whose dependencies are all done"
        )
    while True:
        if stop_requested():
            return MilestoneResult("stopped", milestone.id, "stop requested")
        if (reason := budget.exceeded(hooks.now())) is not None:
            return MilestoneResult("stopped", milestone.id, reason)
        say(out, f"Planning {milestone.id} – {milestone.name}")
        code = hooks.plan()
        if code == EXIT_OK:
            break
        if code == EXIT_EXHAUSTED:
            if (waiting := hooks.wait("plan_commits", milestone.size)) != "ready":
                if waiting != "stopped":
                    return MilestoneResult("stopped", milestone.id, waiting)
                return MilestoneResult(
                    "stopped", milestone.id, "stop requested while waiting for a cooldown"
                )
            continue
        if code == EXIT_ROADMAP_PROBLEM:
            return MilestoneResult(
                "stopped", milestone.id, "roadmap problem reported by the planner"
            )
        return MilestoneResult("stopped", milestone.id, f"planning failed (exit code {code})")
    plan = _read_plan(repo)
    if not _is_open(plan):
        return MilestoneResult(
            "stopped", milestone.id, f"planning left no open steps in {PLAN_FILENAME}"
        )
    assert plan is not None
    return plan


EXIT_DONE = 0  # the roadmap is complete, or (--no-merge) the milestone pull request is published
EXIT_STOP = 1  # a stop rule fired; the reason was printed and notified
EXIT_NOT_STARTED = 2  # nothing ran: not a git repository, invalid config or state


def default_hooks(
    repo: Path,
    config: Config,
    config_path: str | None,
    *,
    agent_timeout: float,
    verify_timeout: float,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    store: AutoStore,
    out: TextIO,
    budget: Budget | None = None,
) -> MilestoneHooks:
    """The real `baton plan`, `baton next --worktree --push` and `baton pr` as hooks."""

    def pr(merge: bool) -> tuple[int, str]:
        buffer = io.StringIO()
        code = run_pr(
            repo,
            merge=merge,
            out=buffer,
            stop_requested=lambda: store.read(repo).stop_requested,
        )
        return code, buffer.getvalue()

    def wait(role: str, size: str | None) -> str:
        from baton.runs import RunStore, budget_reset_message, daily_counts

        agents = resolve_route(config, role, step_size=size).agents
        counts = daily_counts(RunStore().read(), now())
        capped = [
            a
            for a in agents
            if a.daily_run_budget is not None and counts.get(a.name, 0) >= a.daily_run_budget
        ]
        cooling = LimitStore().active(now=now())
        if capped and all(a in capped or a.name in cooling for a in agents):
            return budget_reset_message()
        names = [a.name for a in agents if a not in capped]

        def recheck(name: str, timeout: float) -> str | None:
            from baton.probe import recheck_agent

            counts = daily_counts(RunStore().read(), now())
            agent = config.agents[name]
            if agent.daily_run_budget is not None and counts.get(name, 0) >= agent.daily_run_budget:
                return budget_reset_message()
            recheck_agent(agent, LimitStore(), timeout=timeout, out=out, now=now)
            return None

        return wait_for_reset(
            names,
            LimitStore(),
            recheck_interval_seconds=config.auto.recheck_interval_seconds,
            recheck=recheck if config.auto.recheck_interval_seconds is not None else None,
            now=now,
            sleep=sleep,
            stop_requested=lambda: store.read(repo).stop_requested,
            deadline=(
                budget.started + timedelta(hours=budget.max_hours)
                if budget is not None and budget.max_hours is not None
                else None
            ),
            out=out,
        )

    return MilestoneHooks(
        plan=lambda: run_plan_command(
            repo, config_path=config_path, timeout=agent_timeout, out=out
        ),
        next=lambda resume: run_next_command(
            repo,
            config_path=config_path,
            agent_timeout=agent_timeout,
            verify_timeout=verify_timeout,
            push=True,
            worktree=True,
            resume=resume,
            out=out,
        ),
        pr=pr,
        wait=wait,
        now=now,
    )


def run_auto_command(
    repo: Path,
    *,
    config_path: str | None = None,
    max_steps: int | None = None,
    max_hours: float | None = None,
    merge: bool | None = None,
    agent_timeout: float = DEFAULT_AGENT_TIMEOUT,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
    out: TextIO | None = None,
    hooks: MilestoneHooks | None = None,
    notifier: Callable[[str, str], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Loop over the roadmap's milestones until it is done or a stop rule fires.

    Resumes after a crash from the plan file, the roadmap, git and ``auto.json``; running it
    again is always safe. Exit codes: 0 done, 1 stopped, 2 not started.
    """
    out = out or sys.stdout
    try:
        if not is_repo(repo):
            raise GitError(f"not a git repository: {repo}")
        if max_steps is not None and max_steps < 1:
            raise ConfigError("--max-steps must be at least 1")
        if max_hours is not None and max_hours <= 0:
            raise ConfigError("--max-hours must be greater than 0")
        config = load_config(find_config_path(config_path))
        store = AutoStore()
        # Refuse a second concurrent run before touching the shared state.
        stack = ExitStack()
        stack.enter_context(store.run_lock(repo))
        try:
            # A stop requested before this run started belongs to an earlier run.
            store.update(repo, stop_requested=False, steps_done=0, started_at=now().isoformat())
        except BaseException:
            stack.close()
            raise
    except (GitError, ConfigError, AutoStateError, OSError) as exc:
        say(out, f"Cannot start: {exc}")
        return EXIT_NOT_STARTED
    with stack:
        return _auto_loop(
            repo,
            config,
            config_path,
            store,
            out,
            hooks,
            notifier,
            now,
            sleep,
            merge,
            max_steps,
            max_hours,
            agent_timeout,
            verify_timeout,
        )


def _auto_loop(
    repo,
    config,
    config_path,
    store,
    out,
    hooks,
    notifier,
    now,
    sleep,
    merge,
    max_steps,
    max_hours,
    agent_timeout,
    verify_timeout,
) -> int:

    settings = config.auto
    merge = settings.merge if merge is None else merge
    budget = Budget(
        now(),
        max_steps=max_steps if max_steps is not None else settings.max_steps,
        max_hours=max_hours if max_hours is not None else settings.max_hours,
    )
    send = notifier or (lambda title, message: notify(settings, title, message))
    hooks = hooks or default_hooks(
        repo,
        config,
        config_path,
        agent_timeout=agent_timeout,
        verify_timeout=verify_timeout,
        now=now,
        sleep=sleep,
        store=store,
        out=out,
        budget=budget,
    )
    while True:
        result = run_milestone(repo, hooks, store, budget, merge=merge, out=out)
        link = f" {result.pr_url}" if result.pr_url else ""
        if result.outcome == "merged":
            say(out, f"Merged {result.milestone}.{link}")
            send(f"baton: {result.milestone} merged", f"Milestone {result.milestone} merged.{link}")
            continue
        if result.outcome == "published":
            say(out, f"PR ready for review: {result.milestone}.{link}")
            send(f"baton: {result.milestone} PR ready", f"PR ready for review.{link}")
            return EXIT_DONE
        if result.outcome == "done":
            say(out, "Roadmap complete.")
            send("baton: roadmap complete", "All milestones are done.")
            return EXIT_DONE
        where = f"{result.milestone}: " if result.milestone else ""
        send("baton: stopped", f"{where}{result.reason}{link}")
        return EXIT_STOP
