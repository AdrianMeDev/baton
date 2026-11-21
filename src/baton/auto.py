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
    head,
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
    EXIT_BASE_FAILING,
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
    next_milestone,
    parse_roadmap,
    resolve_roadmap,
    save_roadmap,
    set_milestone_status,
)
from baton.routed import say
from baton.router import resolve_route
from baton.runs import RunStore

Outcome = Literal["merged", "published", "done", "stopped"]
# recoverable: running `baton auto` again may simply continue; needs-human: a decision or fix first.
StopKind = Literal["recoverable", "needs-human"]


@dataclass(frozen=True)
class MilestoneResult:
    """How one milestone run ended; ``reason``, ``kind`` and ``next`` explain a stop."""

    outcome: Outcome
    milestone: str = ""
    reason: str = ""
    pr_url: str = ""
    kind: StopKind | None = None
    next: str = ""


AGAIN = "run `baton auto` again"


def _report_stop(out: TextIO, result: MilestoneResult) -> MilestoneResult:
    """Print the reason and exactly one ``Next:`` line, and journal the stop."""
    say(out, f"Stopped: {result.reason}")
    say(out, f"Next: {result.next} ({result.kind})")
    with suppress(LimitStateError, OSError):
        RunStore().record_stop(result.reason, result.kind or "needs-human")
    return result


def _stopped(reason: str, milestone: str, kind: StopKind, next_action: str) -> MilestoneResult:
    return MilestoneResult("stopped", milestone, reason, kind=kind, next=next_action)


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
    roadmap: str | None = None,
    out: TextIO,
) -> MilestoneResult:
    """Run the next milestone of ``roadmap`` (default: the project's) to a merged base, or stop."""

    def stop(
        reason: str, milestone: str = "", kind: StopKind = "needs-human", next_action: str = ""
    ) -> MilestoneResult:
        return _report_stop(out, _stopped(reason, milestone, kind, next_action or AGAIN))

    def stop_requested() -> bool:
        return store.read(repo).stop_requested

    try:
        plan = _read_plan(repo)
        rel = _relative_roadmap(repo, roadmap)
        if roadmap is not None and _is_open(plan) and plan is not None:
            other = plan.milestone.roadmap
            if other is not None and other != rel:
                return stop(
                    f"{PLAN_FILENAME} belongs to {other}, not {rel}; finish it with `baton auto` "
                    "or remove it first",
                    next_action=f"remove {PLAN_FILENAME} or run `baton auto` without --roadmap",
                )
        if not _adoptable(repo, plan, roadmap):
            plan = _plan_next(repo, hooks, out, stop_requested, budget, roadmap)
            if isinstance(plan, MilestoneResult):
                return _report_stop(out, plan) if plan.outcome == "stopped" else plan
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
                return stop("stop requested", milestone, "recoverable")
            if (reason := budget.exceeded(hooks.now())) is not None:
                return stop(reason, milestone, "recoverable", f"{AGAIN} (raise the budget flag)")
            # The same step still recorded as in progress: continue from its partial diff.
            resume = store.read(repo).step == step.number
            store.update(repo, step=step.number)
            say(out, f"Step {step.number}/{len(plan.steps)}: {step.message}")
            code = hooks.next(resume)
            if code == EXIT_EXHAUSTED:
                if (waiting := hooks.wait("implement", step.size)) != "ready":
                    if waiting != "stopped":
                        return stop(waiting, milestone, "recoverable", f"{AGAIN} after the reset")
                    return stop(
                        "stop requested while waiting for a cooldown", milestone, "recoverable"
                    )
                continue
            if code == EXIT_ROADMAP_PROBLEM:
                return stop(
                    f"roadmap problem reported at step {step.number}",
                    milestone,
                    next_action=f"fix docs/ROADMAP.md or {PLAN_FILENAME}, then {AGAIN}",
                )
            if code == EXIT_BASE_FAILING:
                at = ""
                with suppress(GitError):
                    at = f" at {head(repo)}"
                return stop(
                    f"base already failing{at}, not caused by step {step.number}; fix it on the "
                    "base branch, then fast-forward the milestone branch",
                    milestone,
                    next_action=f"fix the base branch, fast-forward the milestone branch, "
                    f"then {AGAIN}",
                )
            if code not in (EXIT_OK, EXIT_STOPPED):
                return stop(
                    f"step {step.number} failed after escalation (exit code {code}); "
                    "see the plan log",
                    milestone,
                    "recoverable",
                    f"{AGAIN} (resumes the step); see the plan log first",
                )
            after = _read_plan(repo)
            current = _unfinished(after) if after is not None else step
            # A committed [~] step is only settled by the gate, which then reports "stopped".
            if current is not None and (current.number, current.status) == (
                step.number,
                step.status,
            ):
                detail = "made no progress" if code == EXIT_OK else f"was not started (exit {code})"
                return stop(
                    f"step {step.number} {detail}; see the output above",
                    milestone,
                    next_action="run `baton status` and inspect the working tree",
                )
            if code == EXIT_OK:
                budget.steps += 1
                store.update(repo, step=0, steps_done=store.read(repo).steps_done + 1)

        if stop_requested():
            return stop("stop requested", milestone, "recoverable")
        code, output = hooks.pr(merge)
        say(out, output.rstrip())
        url = next((m[0] for m in re.finditer(r"https?://\S+", output)), "")
        if code != EXIT_OK:
            return stop(
                f"pull request not merged: {_last_line(output)}",
                milestone,
                next_action=f"review and merge the pull request, then {AGAIN}",
            )
        store.update(repo, pr_url=url)
        if not merge:
            return MilestoneResult("published", milestone, pr_url=url)
        failure = (hooks.after_merge or ff_base)(repo, plan)
        if failure is not None:
            return stop(failure, milestone, next_action=f"fix the base branch, then {AGAIN}")
        _finish_local_roadmap(repo, plan, roadmap)
        store.update(repo, milestone="", step=0, branch="", pr_url="")
        return MilestoneResult("merged", milestone, pr_url=url)
    except (PlanError, RoadmapError, GitError, LimitStateError, OSError) as exc:
        return stop(f"{exc}", next_action=f"fix the error above, then {AGAIN}")


def _last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no details"


def _relative_roadmap(repo: Path, roadmap: str | None) -> str | None:
    """The roadmap in use as a path relative to ``repo``, or None if there is none."""
    path = resolve_roadmap(repo, roadmap)
    return path.relative_to(repo).as_posix() if path is not None else None


def _adoptable(repo: Path, plan: Plan | None, roadmap: str | None = None) -> bool:
    """Whether the plan on disk is this run's current milestone rather than a merged leftover."""
    if plan is None:
        return False
    if _is_open(plan):
        return True
    ref = plan.milestone
    if ref.id is None:
        return False
    path = resolve_roadmap(repo, roadmap)
    if path is None:
        return False
    if ref.roadmap is not None and ref.roadmap != path.relative_to(repo).as_posix():
        return False  # a finished plan of another roadmap
    found = parse_roadmap(path.read_text(encoding="utf-8")).get(ref.id)
    # Finished steps whose milestone the base still shows as not done: published or merged,
    # but not yet fast-forwarded. `baton pr` recognizes an already merged pull request.
    return found is not None and found.status != "done"


def _finish_local_roadmap(repo: Path, plan: Plan, roadmap: str | None = None) -> None:
    """A local (untracked) roadmap is not part of the merge; mark its milestone done here."""
    ref = plan.milestone
    path = resolve_roadmap(repo, roadmap)
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
    roadmap: str | None = None,
) -> Plan | MilestoneResult:
    """Plan the next roadmap milestone; a result ends the milestone."""
    roadmap_path = resolve_roadmap(repo, roadmap)
    if roadmap_path is None:
        return _stopped(
            f"no roadmap in {repo}", "", "needs-human", "create one with `baton roadmap`"
        )
    roadmap = parse_roadmap(roadmap_path.read_text(encoding="utf-8"))
    milestone = next_milestone(roadmap)
    if milestone is None:
        if all(m.status in ("done", "skipped") for m in roadmap.milestones):
            return MilestoneResult("done")
        return _stopped(
            "no open milestone whose dependencies are all done",
            "",
            "needs-human",
            "check the milestone states and dependencies in docs/ROADMAP.md",
        )
    while True:
        if stop_requested():
            return _stopped("stop requested", milestone.id, "recoverable", AGAIN)
        if (reason := budget.exceeded(hooks.now())) is not None:
            return _stopped(reason, milestone.id, "recoverable", f"{AGAIN} (raise the budget flag)")
        say(out, f"Planning {milestone.id} – {milestone.name}")
        code = hooks.plan()
        if code == EXIT_OK:
            break
        if code == EXIT_EXHAUSTED:
            if (waiting := hooks.wait("plan_commits", milestone.size)) != "ready":
                if waiting != "stopped":
                    return _stopped(
                        waiting, milestone.id, "recoverable", f"{AGAIN} after the reset"
                    )
                return _stopped(
                    "stop requested while waiting for a cooldown",
                    milestone.id,
                    "recoverable",
                    AGAIN,
                )
            continue
        if code == EXIT_ROADMAP_PROBLEM:
            return _stopped(
                "roadmap problem reported by the planner",
                milestone.id,
                "needs-human",
                f"fix docs/ROADMAP.md, then {AGAIN}",
            )
        return _stopped(f"planning failed (exit code {code})", milestone.id, "recoverable", AGAIN)
    plan = _read_plan(repo)
    if not _is_open(plan):
        return _stopped(
            f"planning left no open steps in {PLAN_FILENAME}",
            milestone.id,
            "needs-human",
            f"check the milestone in docs/ROADMAP.md, then {AGAIN}",
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
    roadmap: str | None = None,
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
            repo, config_path=config_path, roadmap=roadmap, timeout=agent_timeout, out=out
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
    roadmap: str | None = None,
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
        if roadmap is not None and resolve_roadmap(repo, roadmap) is None:
            raise ConfigError(f"roadmap {roadmap} not found in {repo}")
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
    except (GitError, ConfigError, AutoStateError, RoadmapError, OSError) as exc:
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
            roadmap,
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
    roadmap=None,
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
        roadmap=roadmap,
    )
    while True:
        result = run_milestone(repo, hooks, store, budget, merge=merge, roadmap=roadmap, out=out)
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
        send("baton: stopped", f"{where}{result.reason}{link}. Next: {result.next}")
        return EXIT_STOP
