"""``baton next``: implement one plan step with an agent, verify it, and stop for the commit.

Mirrors the ``/next`` skill: gate -> agent -> HEAD check -> verify -> ``[~]`` marking -> report.
By default baton never commits; the user reviews and commits. With ``--commit`` a verified
step without critical review findings is committed with the plan's message and marked ``[x]``.
``--worktree`` runs in a dedicated git worktree of the milestone branch (implies ``--commit``).
"""

from __future__ import annotations

import datetime
import shlex
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from baton.adapter import Adapter, CommandAdapter
from baton.config import ConfigError, find_config_path, load_config
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
    worktree_path_for,
)
from baton.git import push as git_push
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
from baton.prompt import (
    fix_prompt,
    handoff_prompt,
    implement_prompt,
    review_prompt,
    roadmap_problem,
    step_text,
)
from baton.review import (
    SEVERITIES,
    ReviewError,
    ReviewResult,
    candidate_tree,
    evidence_entry,
    parse_review,
    review_context,
)
from baton.roadmap import RoadmapError
from baton.routed import RoutedAdapter, say
from baton.router import resolve_route
from baton.verify import VerifyResult, _clean, run_verify
from baton.worktree import ensure_worktree, mirror_plan

EXIT_OK = 0
EXIT_FAILED = 1  # agent, verify or plan problem; changes stay in the tree
EXIT_STOPPED = 2  # nothing was started (gate, no open step, dirty tree)
EXIT_ROADMAP_PROBLEM = 4  # the agent reported BATON-ROADMAP-PROBLEM; nothing was marked
EXIT_EXHAUSTED = 3  # every agent of the route is in cooldown; the partial diff stays in the tree

DEFAULT_AGENT_TIMEOUT = 1800.0
DEFAULT_VERIFY_TIMEOUT = 900.0


def _commit_hint(out: TextIO, message: str) -> None:
    say(out, "  git add -A")
    say(out, f"  git commit -m {shlex.quote(message)}")


def _log(plan_path: Path, line: str, date: str) -> None:
    """Append a Log line to the plan as it is now on disk."""
    text = plan_path.read_text(encoding="utf-8")
    save_plan(plan_path, append_log(text, f"{date}: {line}"))


@contextmanager
def _protect_plan(path: Path):
    """Restore trusted bytes before reporting any externally induced plan change."""
    trusted = path.read_bytes()
    try:
        yield
    finally:
        try:
            intact = not path.is_symlink() and path.is_file() and path.read_bytes() == trusted
        except OSError:
            intact = False
        if not intact:
            if path.is_dir() and not path.is_symlink():
                displaced = Path(tempfile.mkdtemp(prefix="baton-replaced-plan-", dir=path.parent))
                path.rename(displaced / "replaced-plan")
            else:
                path.unlink(missing_ok=True)
            path.write_bytes(trusted)
            raise PlanError("Active plan changed during execution; trusted plan restored")


def _handle_gate(
    plan: Plan,
    gate: GateResult,
    plan_path: Path,
    repo: Path,
    out: TextIO,
    date: str,
    resume: bool = False,
) -> int | None:
    """Return an exit code when the gate stops the run, ``None`` to continue."""
    if gate.state == "blocked_dirty":
        pending = next(s for s in plan.steps if s.number == gate.step)
        say(out, f"Step {gate.step} is implemented but not committed. Not starting new work.")
        say(out, "Changed files:")
        for path in gate.dirty:
            say(out, f"  {path}")
        say(out, "Review with `git diff`, then commit:")
        _commit_hint(out, pending.message)
        say(out, "Run `baton next` again after committing.")
        return EXIT_STOPPED
    if gate.state == "discarded":
        say(out, f"Step {gate.step} is marked [~] but HEAD is unchanged and the tree is clean.")
        say(out, "The changes were discarded or stashed. Not starting new work.")
        say(out, "Fix the plan (re-open or skip the step) and run `baton next` again.")
        return EXIT_STOPPED
    if gate.state in ("committed", "overridden"):
        text = plan_path.read_text(encoding="utf-8")
        save_plan(plan_path, apply_gate(text, gate, date))
        say(out, f"Step {gate.step} marked done (HEAD {gate.head}).")
        return None
    dirty = dirty_paths(repo)
    if dirty and not resume:
        say(out, "The working tree has uncommitted changes but no step is marked [~].")
        say(out, "Commit or stash them first, so the agent's changes can be reviewed alone:")
        for path in dirty:
            say(out, f"  {path}")
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
    pushed: str | None = None,
) -> None:
    say(out)
    say(out, f"Step {step.number}/{len(plan.steps)} done: {step.message}")
    say(out, "Changed:")
    for path in dirty_paths(repo) if changed is None else changed:
        say(out, f"  {path}")
    say(out, "Checks:")
    if verify.verified:
        say(out, f"  {verify.command} -> passed")
    else:
        say(out, "  (the step has no Verify command: not verified)")
    if cost is not None:
        say(out, f"Agent cost: ${cost:.4f}")
    if review is not None:
        say(out, f"Independent reviewer: {review.reviewer.name} ({review.reviewer.adapter})")
        if not review.findings:
            say(out, "  No findings.")
        for severity in SEVERITIES:
            findings = [finding for finding in review.findings if finding.severity == severity]
            if findings:
                say(out, f"  {severity.upper()}:")
            for finding in findings:
                location = finding.path or ""
                if finding.line is not None:
                    location += f":{finding.line}"
                say(out, f"    {location + ': ' if location else ''}{finding.message}")
    if committed is not None:
        say(out, f"Committed: {committed} {step.message}")
        if pushed:
            say(out, f"Pushed: {pushed}")
        if notice:
            say(out, notice)
    else:
        if notice:
            say(out, notice)
        say(out, "Review: `git diff`")
        say(out, "Commit:")
        _commit_hint(out, step.message)
    following = next((s for s in plan.steps if s.number > step.number and s.status == "open"), None)
    if following:
        when = "" if committed is not None else "after committing, "
        say(out, f"Next: {when}run `baton next` for step {following.number}:")
        say(out, f"  {following.message}")
    else:
        if committed is not None:
            say(out, "Next: that was the last step; run `baton next` once more to finish.")
        else:
            say(out, "Next: last open step. After committing, run `baton next` once more.")


def _enter_branch(repo: Path, plan: Plan, out: TextIO) -> bool:
    """Make the milestone branch current (creating it from HEAD); False if the tree is dirty."""
    branch = milestone_branch(plan)
    if current_branch(repo) == branch:
        say(out, f"Branch: {branch}")
        return True
    dirty = dirty_paths(repo)
    if dirty:
        say(out, f"Cannot switch to {branch}: the working tree has uncommitted changes.")
        for path in dirty:
            say(out, f"  {path}")
        say(out, "Commit or stash them, then run `baton next --commit` again.")
        return False
    exists = branch_exists(repo, branch)
    switch_branch(repo, branch, create=not exists)
    say(out, f"Branch: {branch} ({'switched to' if exists else 'created from ' + head(repo)})")
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
    push: bool = False,
    worktree: bool = False,
    resume: bool = False,
) -> int:
    """Implement the first open step of ``repo``'s plan; returns the process exit code."""
    return _with_worktree(
        repo,
        worktree,
        out,
        lambda work: _run_next(
            work,
            lambda step: adapter,
            out,
            agent_timeout=agent_timeout,
            verify_timeout=verify_timeout,
            override=override,
            today=today,
            commit=commit or worktree,
            push=push,
            resume=resume,
        ),
    )


def _with_worktree(repo: Path, enabled: bool, out: TextIO, run: Callable[[Path], int]) -> int:
    """Run ``run`` in the milestone worktree of ``repo`` (if ``enabled``), then mirror the plan."""
    if not enabled:
        return run(repo)
    plan_path = repo / PLAN_FILENAME
    try:
        if not plan_path.is_file():
            say(out, f"No {PLAN_FILENAME} in {repo}. Create one with `/plan-commits <task>`.")
            return EXIT_STOPPED
        work, created = ensure_worktree(repo, parse_plan(plan_path.read_text(encoding="utf-8")))
    except (GitError, PlanError, OSError) as exc:
        say(out, f"Error: {exc}")
        return EXIT_FAILED
    say(out, f"Worktree: {work} ({'created' if created else 'reused'})")
    try:
        return run(work)
    finally:
        try:
            mirror_plan(work, repo)
        except OSError as exc:
            say(out, f"Could not mirror {PLAN_FILENAME} back to {repo}: {exc}")


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
    push: bool = False,
    resume: bool = False,
) -> int:
    """``resume`` continues the first open step from the partial diff a limited agent left."""
    commit = commit or push
    date = today or datetime.date.today().isoformat()
    plan_path = repo / PLAN_FILENAME
    try:
        if not plan_path.is_file():
            say(out, f"No {PLAN_FILENAME} in {repo}. Create one with `/plan-commits <task>`.")
            return EXIT_STOPPED
        plan = parse_plan(plan_path.read_text(encoding="utf-8"))

        gate = check_gate(plan, repo, override)
        stop = _handle_gate(plan, gate, plan_path, repo, out, date, resume)
        if stop is not None:
            return stop
        if gate.state in ("committed", "overridden"):
            plan = parse_plan(plan_path.read_text(encoding="utf-8"))

        step = first_open_step(plan)
        if step is None:
            say(out, "No open steps left in the plan.")
            return EXIT_STOPPED

        say(out, f"Step {step.number}/{len(plan.steps)}: {step.message}")
        if commit and not _enter_branch(repo, plan, out):
            return EXIT_STOPPED
        if commit and not _clean(step.verify):
            say(out, "Automatic commits require a Verify command; the step stays open.")
            return EXIT_FAILED
        adapter = select_adapter(step)
        raw_step = step_text(plan_path.read_text("utf-8"), step)
        prompt = implement_prompt(plan, step, raw_step)
        if resume and dirty_paths(repo):
            say(out, "Resuming from the partial diff in the working tree.")
            prompt = handoff_prompt(prompt, "previous run", handoff_context(repo))
        before = head(repo)

        with _protect_plan(plan_path):
            result = adapter.run(prompt, repo, agent_timeout)
        after = head(repo)
        if after != before:
            say(out, f"HEAD moved from {before} to {after}: the agent committed. Step not marked.")
            _log(plan_path, f"step {step.number}: agent moved HEAD {before} -> {after}", date)
            return EXIT_FAILED

        problem_reason = roadmap_problem(result.text)
        if problem_reason is not None:
            say(out, f"The agent reported a roadmap problem: {problem_reason}")
            say(out, "Changes (if any) stay in the working tree; the step stays open.")
            _log(plan_path, f"step {step.number}: roadmap problem ({problem_reason})", date)
            return EXIT_ROADMAP_PROBLEM
        if not result.ok:
            say(out, f"The agent failed: {result.error}")
            say(out, "Changes (if any) stay in the working tree; the step stays open.")
            _log(plan_path, f"step {step.number}: agent failed ({result.error})", date)
            return EXIT_EXHAUSTED if getattr(adapter, "exhausted", False) else EXIT_FAILED

        if commit:
            sync_milestone(repo, plan, step, date)
        with _protect_plan(plan_path):
            verify = run_verify(step.verify, repo, verify_timeout)
        repairs = 0
        for escalated in (False, True):
            if not verify.verified or verify.passed:
                break
            say(out, f"Verify failed ({_verify_failure(verify)}): {verify.command}")
            say(out, verify.output_tail)
            fixer = select_fix(step, escalated) if select_fix is not None else None
            if fixer is None:
                break
            repairs += 1
            label = "Escalating repair to tier L." if escalated else "Attempting same-tier repair."
            say(out, label)
            with _protect_plan(plan_path):
                repair = fixer.run(fix_prompt(prompt, verify), repo, agent_timeout)
            after = head(repo)
            if after != before:
                say(out, f"HEAD moved from {before} to {after}: fixer committed. Step not marked.")
                _log(plan_path, f"step {step.number}: fixer moved HEAD {before} -> {after}", date)
                return EXIT_FAILED
            if not repair.ok:
                say(out, f"The fix agent failed: {repair.error}")
                say(out, "Changes stay in the working tree; the step stays open.")
                _log(plan_path, f"step {step.number}: fix agent failed ({repair.error})", date)
                return EXIT_FAILED
            with _protect_plan(plan_path):
                verify = run_verify(step.verify, repo, verify_timeout)
            cost = (
                result.cost + repair.cost
                if result.cost is not None and repair.cost is not None
                else None
            )
            result = replace(result, cost=cost)
        if verify.verified and not verify.passed:
            say(out, f"Verify failed ({_verify_failure(verify)}): {verify.command}")
            say(out, verify.output_tail)
            say(out, "Changes stay in the working tree; the step stays open.")
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
                with _protect_plan(plan_path):
                    review = review_step(plan, step, raw_step)
            except (ReviewError, ConfigError, GitError, LimitStateError, OSError) as exc:
                say(out, f"Independent review failed: {exc}")
                say(out, "Changes stay in the working tree; the step stays open.")
                _log(plan_path, f"step {step.number}: independent review failed ({exc})", date)
                return EXIT_FAILED

        evidence = evidence_entry(
            step.number, candidate_tree(repo), head(repo, short=False), verify, review
        )
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

        push_problem = None
        pushed = None
        if push and committed is not None:
            try:
                branch = current_branch(repo)
                git_push(repo, branch)
                pushed = f"origin/{branch}"
            except GitError as exc:
                push_problem = " ".join(str(exc).split())
                notice = f"Push failed: {push_problem}\nThe commit stays; push manually when ready."

        # Re-read: the agent may have touched the plan file despite the rules.
        text = plan_path.read_text(encoding="utf-8")
        text = append_log(text, evidence)
        text = set_step_status(text, step.number, "pending", after)
        note = "implemented" if verify.verified else "implemented (no Verify command)"
        if repairs:
            note += f" after {repairs} repair attempts"
        if review is not None:
            note += f"; {review.summary()}"
        if committed is not None:
            text = set_step_status(text, step.number, "done", committed)
            text = append_log(text, f"{date}: step {step.number} {note}, committed ({committed}).")
            if push_problem is not None:
                text = append_log(text, f"{date}: step {step.number} push failed: {push_problem}.")
        elif problem is not None:
            text = append_log(text, f"{date}: step {step.number} {note}; auto-commit: {problem}.")
        else:
            text = append_log(text, f"{date}: step {step.number} {note}, waiting for commit.")
        save_plan(plan_path, text)
    except (ConfigError, GitError, LimitStateError, PlanError, RoadmapError, OSError) as exc:
        say(out, f"Error: {exc}")
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
        pushed=pushed,
    )
    return EXIT_FAILED if problem or push_problem else EXIT_OK


def _dry_run(
    repo: Path,
    select_adapter: Callable[[Step], CommandAdapter | RoutedAdapter],
    out: TextIO,
    *,
    commit: bool = False,
    push: bool = False,
    worktree: bool = False,
) -> int:
    """Print the prompt and argv for the first open step; change nothing."""
    plan_path = repo / PLAN_FILENAME
    if not plan_path.is_file():
        say(out, f"No {PLAN_FILENAME} in {repo}. Create one with `/plan-commits <task>`.")
        return EXIT_STOPPED
    text = plan_path.read_text(encoding="utf-8")
    plan = parse_plan(text)
    gate = check_gate(plan, repo)
    if gate.state in ("blocked_dirty", "discarded"):
        say(out, f"Gate: {gate.state}; no agent selected.")
        return EXIT_STOPPED
    if gate.state == "committed":
        text = apply_gate(text, gate, datetime.date.today().isoformat())
        plan = parse_plan(text)
    step = first_open_step(plan)
    if step is None:
        say(out, "No open steps left in the plan.")
        return EXIT_STOPPED
    prompt = implement_prompt(plan, step, step_text(text, step))
    say(out, f"Step {step.number}/{len(plan.steps)}: {step.message}")
    if commit:
        branch = milestone_branch(plan)
        if current_branch(repo) == branch:
            state = "current"
        else:
            state = "exists, would switch" if branch_exists(repo, branch) else "would be created"
        say(out, f"Branch: {branch} ({state})")
        if worktree:
            path = worktree_path_for(repo, branch)
            say(out, f"Worktree: {path} ({'exists' if path.is_dir() else 'would be created'})")
        if push:
            say(out, f"Would push: origin {branch}")
    adapter = select_adapter(step)
    say(out, "Command (cwd = repo root):")
    say(out, "  " + " ".join(shlex.quote(a) for a in adapter.argv("<prompt>")))
    if getattr(adapter, "transport", "argv") == "stdin":
        say(out, "  (prompt via stdin)")
    say(out, "Prompt:")
    say(out, prompt)
    return EXIT_OK


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
    push: bool = False,
    worktree: bool = False,
    resume: bool = False,
    out: TextIO | None = None,
) -> int:
    """CLI entry: select an agent for the step being run or previewed."""
    out = out or sys.stdout

    config = None
    implementation = None
    active = repo  # the checkout the agent works in: ``repo`` or its milestone worktree

    def select_adapter(step: Step) -> RoutedAdapter:
        nonlocal config, implementation
        config = load_config(find_config_path(config_path))
        route = resolve_route(
            config, "implement", tier=tier, step_size=step.size, agent_name=agent_name
        )
        implementation = RoutedAdapter(route, LimitStore(), out)
        return implementation

    def select_fix(step: Step, escalated: bool) -> Adapter | None:
        # Legacy configs have no role routes; injected callers likewise keep their behavior.
        assert config is not None and implementation is not None
        if not config.routes:
            return None
        route = resolve_route(config, "fix", tier="L" if escalated else implementation.route.tier)
        fixer = RoutedAdapter(route, implementation.store, out)
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
        reviewer = RoutedAdapter(route, implementation.store, out, review=True)
        before = head(active)
        changes = review_context(active)
        result = reviewer.run(review_prompt(plan, step, raw_step, changes), active, agent_timeout)
        if head(active) != before or review_context(active) != changes:
            raise ReviewError("Candidate changed during review; review is stale")
        if not result.ok:
            raise ReviewError(result.error)
        return ReviewResult(reviewer.attempted_agents[-1], parse_review(result.text))

    if dry_run:
        try:
            return _dry_run(
                repo,
                select_adapter,
                out,
                commit=commit or push or worktree,
                push=push,
                worktree=worktree,
            )
        except (ConfigError, GitError, LimitStateError, PlanError, RoadmapError, OSError) as exc:
            say(out, f"Error: {exc}")
            return EXIT_FAILED

    def run_in(work: Path) -> int:
        nonlocal active
        active = work
        return _run_next(
            work,
            select_adapter,
            out,
            agent_timeout=agent_timeout,
            verify_timeout=verify_timeout,
            select_fix=select_fix,
            review_step=review_step,
            commit=commit or worktree,
            push=push,
            resume=resume,
        )

    return _with_worktree(repo, worktree, out, run_in)
