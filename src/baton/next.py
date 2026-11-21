"""``baton next``: implement one plan step with an agent, verify it, and stop for the commit.

Mirrors the ``/next`` skill: gate -> agent -> HEAD check -> verify -> ``[~]`` marking -> report.
By default baton never commits; the user reviews and commits. With ``--commit`` a verified
step without critical review findings is committed with the plan's message and marked ``[x]``.
``--worktree`` runs in a dedicated git worktree of the milestone branch (implies ``--commit``).
"""

from __future__ import annotations

import datetime
import difflib
import shlex
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from baton.adapter import Adapter, AgentResult, CommandAdapter
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
    ignored_paths,
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
    changed_paths,
    first_open_step,
    parse_plan,
    save_plan,
    set_step_status,
)
from baton.plan_check import verify_problems
from baton.prompt import (
    fix_prompt,
    handoff_prompt,
    implement_prompt,
    review_fix_prompt,
    review_prompt,
    roadmap_problem,
    step_text,
)
from baton.repair import repair_from_reply, repair_prompt, repaired_before
from baton.review import (
    SEVERITIES,
    Finding,
    ReviewError,
    ReviewResult,
    candidate_tree,
    evidence_entry,
    parse_review,
    review_context,
)
from baton.roadmap import RoadmapError
from baton.routed import RoutedAdapter, RoutedPlanner, say
from baton.router import resolve_route
from baton.runs import RunStore
from baton.verify import VerifyResult, _clean, run_verify, run_verify_at_head
from baton.worktree import ensure_worktree, find_worktree, mirror_plan

EXIT_OK = 0
EXIT_FAILED = 1  # agent, verify or plan problem; changes stay in the tree
EXIT_STOPPED = 2  # nothing was started (gate, no open step, dirty tree)
EXIT_ROADMAP_PROBLEM = 4  # the agent reported BATON-ROADMAP-PROBLEM; nothing was marked
EXIT_EXHAUSTED = 3  # every agent of the route is in cooldown; the partial diff stays in the tree
EXIT_BASE_FAILING = 5  # the plan's Baseline command already fails on HEAD; no agent was started
EXIT_VERIFY_TIMEOUT = 6  # the step's Verify timed out; no repair was started, the diff stays

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


def _check_baseline(
    repo: Path, plan: Plan, plan_path: Path, timeout: float, out: TextIO, date: str
) -> int | None:
    """Run the plan's Baseline on HEAD; ``EXIT_BASE_FAILING`` if it fails, ``None`` to go on.

    A pass is cached in the Log per HEAD; a failure is re-run, since the user may have fixed it.
    """
    if not plan.baseline:
        return None
    at = head(repo)
    if any(line.endswith(f"baseline passed at {at}") for line in plan.log):
        return None
    say(out, f"Baseline: {plan.baseline}")
    with _protect_plan(plan_path):
        # A dirty tree (resume) holds the partial diff: check the committed base in a worktree.
        run_baseline = run_verify_at_head if dirty_paths(repo) else run_verify
        result = run_baseline(plan.baseline, repo, timeout)
    if result.passed:
        _log(plan_path, f"baseline passed at {at}", date)
        return None
    say(out, f"Base already failing at {at} ({_verify_failure(result)}): {result.command}")
    say(out, result.output_tail)
    say(out, "Not caused by this step. Fix it on the base branch, then fast-forward this branch.")
    _log(plan_path, f"baseline failed at {at} ({_verify_failure(result)})", date)
    return EXIT_BASE_FAILING


RepairPlan = Callable[[Plan, str, Step, str, str], str | None]
"""``(plan, plan_text, step, reason, diff)`` -> the repair agent's reply, ``None`` if it failed."""


def _plan_diff(old: str, new: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        ".agent-plan.md (old)",
        ".agent-plan.md (new)",
        lineterm="",
    )
    return "\n".join(diff)


def _try_repair(
    repo: Path,
    plan_path: Path,
    plan: Plan,
    step: Step,
    reason: str,
    repair_plan: RepairPlan,
    commit: bool,
    out: TextIO,
    date: str,
) -> int | None:
    """Repair the plan after a reported problem; ``None`` means: repaired, continue the step."""
    plan_text = plan_path.read_text(encoding="utf-8")
    say(out, f"Asking the plan repair agent to fix steps {step.number}..{len(plan.steps)}.")
    try:
        with _protect_plan(plan_path):
            reply = repair_plan(plan, plan_text, step, reason, handoff_context(repo))
    except (ConfigError, GitError, LimitStateError, OSError) as exc:
        reply = None
        say(out, f"The plan repair agent could not run: {exc}")
    if reply is None:
        _log(plan_path, f"step {step.number}: plan repair failed", date)
        return EXIT_ROADMAP_PROBLEM
    result = repair_from_reply(plan_text, step.number, reply, reason)
    if result.needs_decision is not None:
        say(out, f"The plan repair needs your decision: {result.needs_decision}")
        _log(
            plan_path,
            f"step {step.number}: plan repair needs a decision ({result.needs_decision})",
            date,
        )
        return EXIT_ROADMAP_PROBLEM
    if result.errors:
        say(out, "The proposed plan repair was rejected:")
        for error in result.errors:
            say(out, f"  {error}")
        _log(
            plan_path,
            f"step {step.number}: plan repair rejected ({'; '.join(result.errors)})",
            date,
        )
        return EXIT_ROADMAP_PROBLEM
    save_plan(plan_path, result.text)
    say(out, f"Plan repaired at step {step.number}. Changes to {PLAN_FILENAME}:")
    say(out, _plan_diff(plan_text, result.text))
    if not commit:
        say(out, "Review the plan change, then run `baton next --resume` to continue the step.")
        return EXIT_STOPPED
    return None


def _critical(review: ReviewResult | None) -> list[Finding]:
    return [] if review is None else [f for f in review.findings if f.severity == "critical"]


def _run_verify(plan: Plan, step: Step, repo: Path, timeout: float) -> VerifyResult:
    """Run the step's Verify, but first fail when git ignores a file the step names in Changes.

    Such a file would be missing from the commit, so a green local Verify would be misleading.
    """
    existing = [p for p in changed_paths(step) if p != PLAN_FILENAME and (repo / p).exists()]
    ignored = ignored_paths(repo, existing)
    if ignored:
        listing = "\n".join(ignored)
        return VerifyResult(
            verified=True,
            passed=False,
            exit_code=1,
            timed_out=False,
            output_tail=(
                "These files named in Changes are ignored by git and would not be committed "
                "(source:line:pattern, path). Remove or adjust the ignoring rule's effect, for "
                "example with a negation in .gitignore, or place the file elsewhere:\n" + listing
            ),
            command="git check-ignore -v (paths named in Changes)",
        )
    started = time.monotonic()
    result = run_verify(step.verify, repo, timeout)
    if result.verified:
        with suppress(LimitStateError, OSError):  # measuring must never fail a step
            RunStore().record_verify(
                plan.milestone.id or "",
                step.number,
                time.monotonic() - started,
                result.passed,
                result.timed_out,
            )
    return result


def _verify_timeout(plan: Plan, step: Step, flag: float) -> float:
    """The Verify timeout in seconds: the step's field, else the plan header's, else the flag."""
    for value in (step.verify_timeout, plan.verify_timeout):
        if value is not None:
            return value
    return flag


def _verify_timed_out(
    out: TextIO, plan_path: Path, plan: Plan, step: Step, timeout: float, date: str
) -> int:
    """Stop on a Verify timeout: repairing would only wait out the same timeout again."""
    say(out, f"Verify timed out after {timeout:g}s")
    if step.verify_timeout is not None:
        hint = f"the step's Verify-Timeout field (step {step.number})"
    elif plan.verify_timeout is not None:
        hint = "the plan header's Verify-Timeout field"
    else:
        hint = "--verify-timeout"
    say(out, f"Raise the Verify timeout ({hint}) and run again.")
    say(out, "Changes stay in the working tree; the step stays open.")
    _log(plan_path, f"step {step.number}: verify timed out", date)
    return EXIT_VERIFY_TIMEOUT


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
    repair_plan: RepairPlan | None = None,
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
            repair_plan=repair_plan,
        ),
    )


def _with_worktree(repo: Path, enabled: bool, out: TextIO, run: Callable[[Path], int]) -> int:
    """Run ``run`` in the milestone worktree of ``repo`` (if ``enabled``), then mirror the plan."""
    plan_path = repo / PLAN_FILENAME
    try:
        if not plan_path.is_file():
            if not enabled:
                return run(repo)
            say(out, f"No {PLAN_FILENAME} in {repo}. Create one with `/plan-commits <task>`.")
            return EXIT_STOPPED
        plan = parse_plan(plan_path.read_text(encoding="utf-8"))
        if enabled:
            work, created = ensure_worktree(repo, plan)
        else:
            # An existing milestone worktree holds the earlier steps, so use it automatically.
            work, created = find_worktree(repo, plan), False
            if work is None:
                return run(repo)
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
    repair_plan: RepairPlan | None = None,
) -> int:
    """``resume`` continues the first open step from the partial diff a limited agent left.

    ``repair_plan`` is asked once per step to replace the plan's remaining steps when the agent
    reports a roadmap problem; without it (or when it fails) the run stops as before.
    """
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
        unusable, container = verify_problems(step.verify)
        if unusable:
            say(out, f"Step {step.number}: the Verify cannot work: {unusable}.")
            say(
                out, "Fix the step's Verify (one shell command in backticks); no agent was started."
            )
            _log(plan_path, f"step {step.number}: verify unusable ({unusable})", date)
            return EXIT_STOPPED
        if container:
            say(out, f"Warning: step {step.number}'s Verify is slow: {container}.")
        stop = _check_baseline(repo, plan, plan_path, verify_timeout, out, date)
        if stop is not None:
            return stop
        step_timeout = _verify_timeout(plan, step, verify_timeout)
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
            if repair_plan is not None and not repaired_before(plan, step.number):
                _log(plan_path, f"step {step.number}: roadmap problem ({problem_reason})", date)
                stop = _try_repair(
                    repo, plan_path, plan, step, problem_reason, repair_plan, commit, out, date
                )
                if stop is not None:
                    return stop
                return _run_next(
                    repo,
                    select_adapter,
                    out,
                    agent_timeout=agent_timeout,
                    verify_timeout=verify_timeout,
                    override=override,
                    today=today,
                    select_fix=select_fix,
                    review_step=review_step,
                    commit=commit,
                    push=push,
                    resume=True,
                    repair_plan=repair_plan,
                )
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
            verify = _run_verify(plan, step, repo, step_timeout)
        repairs = 0
        for escalated in (False, True):
            if not verify.verified or verify.passed or verify.timed_out:
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
                verify = _run_verify(plan, step, repo, step_timeout)
            cost = (
                result.cost + repair.cost
                if result.cost is not None and repair.cost is not None
                else None
            )
            result = replace(result, cost=cost)
        if verify.timed_out:
            return _verify_timed_out(out, plan_path, plan, step, step_timeout, date)
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
        review_fixes = 0
        if review_step is not None:

            def review_once() -> bool:
                nonlocal review
                try:
                    with _protect_plan(plan_path):
                        review = review_step(plan, step, raw_step)
                except (ReviewError, ConfigError, GitError, LimitStateError, OSError) as exc:
                    say(out, f"Independent review failed: {exc}")
                    say(out, "Changes stay in the working tree; the step stays open.")
                    _log(plan_path, f"step {step.number}: independent review failed ({exc})", date)
                    return False
                return True

            if not review_once():
                return EXIT_FAILED
            for escalated in (False, True):
                critical = _critical(review)
                # A finding without a location cannot be handed to a fixer: stop as before.
                if not critical or any(f.path is None for f in critical) or select_fix is None:
                    break
                try:
                    fixer = select_fix(step, escalated)
                except ConfigError:
                    fixer = None  # no fix route configured: the review only reports, as before
                if fixer is None:
                    break
                review_fixes += 1
                label = "tier L" if escalated else "the step's tier"
                say(out, f"Critical review findings ({len(critical)}): fixing with {label}.")
                _log(
                    plan_path,
                    f"step {step.number}: review fix round {review_fixes} "
                    f"({len(critical)} critical findings)",
                    date,
                )
                with _protect_plan(plan_path):
                    repair = fixer.run(review_fix_prompt(prompt, critical), repo, agent_timeout)
                after = head(repo)
                if after != before:
                    say(
                        out,
                        f"HEAD moved from {before} to {after}: fixer committed. Step not marked.",
                    )
                    _log(
                        plan_path, f"step {step.number}: fixer moved HEAD {before} -> {after}", date
                    )
                    return EXIT_FAILED
                if not repair.ok:
                    say(out, f"The fix agent failed: {repair.error}")
                    say(out, "Changes stay in the working tree; the step stays open.")
                    _log(plan_path, f"step {step.number}: fix agent failed ({repair.error})", date)
                    return EXIT_FAILED
                with _protect_plan(plan_path):
                    verify = _run_verify(plan, step, repo, step_timeout)
                if verify.timed_out:
                    return _verify_timed_out(out, plan_path, plan, step, step_timeout, date)
                if verify.verified and not verify.passed:
                    say(out, f"Verify failed after the review fix ({_verify_failure(verify)}).")
                    say(out, verify.output_tail)
                    say(out, "Changes stay in the working tree; the step stays open.")
                    _log(
                        plan_path,
                        f"step {step.number}: verify failed ({_verify_failure(verify)}) "
                        f"after review fix round {review_fixes}",
                        date,
                    )
                    return EXIT_FAILED
                if result.cost is not None and repair.cost is not None:
                    result = replace(result, cost=result.cost + repair.cost)
                else:
                    result = replace(result, cost=None)
                if not review_once():
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
        if review_fixes:
            note += f" after {review_fixes} review fix rounds"
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
    if plan.baseline:
        say(out, f"Baseline: {plan.baseline} (not run in a dry run)")
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


def _invalid_review(result: AgentResult) -> str | None:
    try:
        parse_review(result.text)
    except ReviewError as exc:
        return str(exc)
    return None


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
        prompt = review_prompt(plan, step, raw_step, changes)
        result = reviewer.run(prompt, active, agent_timeout, accept=_invalid_review)
        if not result.ok and reviewer.rejected and not reviewer.exhausted:
            say(out, "No reviewer gave a valid review; trying the route once more.")
            result = reviewer.run(prompt, active, agent_timeout, accept=_invalid_review)
        if head(active) != before or review_context(active) != changes:
            raise ReviewError("Candidate changed during review; review is stale")
        if not result.ok:
            raise ReviewError(result.error)
        return ReviewResult(reviewer.attempted_agents[-1], parse_review(result.text))

    def repair_plan(plan: Plan, plan_text: str, step: Step, reason: str, diff: str) -> str | None:
        assert config is not None and implementation is not None
        if not config.routes:
            return None
        route = resolve_route(config, "plan_commits", tier="L")
        planner = RoutedPlanner(route, implementation.store, out)
        reply = planner.run(
            repair_prompt(plan, plan_text, step, reason, diff), active, agent_timeout
        )
        if not reply.ok:
            say(out, f"The plan repair agent failed: {reply.error}")
            return None
        return reply.text

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
            repair_plan=repair_plan,
        )

    return _with_worktree(repo, worktree, out, run_in)
