"""``baton next``: implement one plan step with an agent, verify it, and stop for the commit.

Mirrors the ``/next`` skill: gate -> agent -> HEAD check -> verify -> ``[~]`` marking -> report.
baton never commits; the user reviews and commits.
"""

from __future__ import annotations

import datetime
import shlex
import sys
from pathlib import Path
from typing import TextIO

from baton.adapter import Adapter, ClaudeAdapter
from baton.config import ConfigError, find_config_path, load_config
from baton.gate import GateResult, apply_gate, check_gate
from baton.git import PLAN_FILENAME, GitError, dirty_paths, head
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
from baton.prompt import implement_prompt, step_text
from baton.router import pick_claude_agent
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
    out: TextIO, plan: Plan, step: Step, repo: Path, verify: VerifyResult, cost: float | None
) -> None:
    _say(out)
    _say(out, f"Step {step.number}/{len(plan.steps)} done: {step.message}")
    _say(out, "Changed:")
    for path in dirty_paths(repo):
        _say(out, f"  {path}")
    _say(out, "Checks:")
    if verify.verified:
        _say(out, f"  {verify.command} -> passed")
    else:
        _say(out, "  (the step has no Verify command: not verified)")
    if cost is not None:
        _say(out, f"Agent cost: ${cost:.4f}")
    _say(out, "Review: `git diff`")
    _say(out, "Commit:")
    _commit_hint(out, step.message)
    following = next((s for s in plan.steps if s.number > step.number and s.status == "open"), None)
    if following:
        _say(out, f"Next: after committing, run `baton next` for step {following.number}:")
        _say(out, f"  {following.message}")
    else:
        _say(out, "Next: last open step. After committing, run `baton next` once more.")


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
) -> int:
    """Implement the first open step of ``repo``'s plan; returns the process exit code."""
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
        prompt = implement_prompt(plan, step, step_text(plan_path.read_text("utf-8"), step))
        before = head(repo)

        result = adapter.run(prompt, repo, agent_timeout)
        if not result.ok:
            _say(out, f"The agent failed: {result.error}")
            _say(out, "Changes (if any) stay in the working tree; the step stays open.")
            _log(plan_path, f"step {step.number}: agent failed ({result.error})", date)
            return EXIT_FAILED

        after = head(repo)
        if after != before:
            _say(out, f"HEAD moved from {before} to {after}: the agent committed. Step not marked.")
            _log(plan_path, f"step {step.number}: agent moved HEAD {before} -> {after}", date)
            return EXIT_FAILED

        verify = run_verify(step.verify, repo, verify_timeout)
        if verify.verified and not verify.passed:
            _say(out, f"Verify failed ({_verify_failure(verify)}): {verify.command}")
            _say(out, verify.output_tail)
            _say(out, "Changes stay in the working tree; the step stays open.")
            _log(
                plan_path,
                f"step {step.number}: verify failed ({_verify_failure(verify)})",
                date,
            )
            return EXIT_FAILED

        # Re-read: the agent may have touched the plan file despite the rules.
        text = plan_path.read_text(encoding="utf-8")
        text = set_step_status(text, step.number, "pending", after)
        note = "implemented" if verify.verified else "implemented (no Verify command)"
        text = append_log(text, f"{date}: step {step.number} {note}, waiting for commit.")
        save_plan(plan_path, text)
    except (GitError, PlanError, OSError) as exc:
        _say(out, f"Error: {exc}")
        return EXIT_FAILED

    _report_success(out, plan, step, repo, verify, result.cost)
    return EXIT_OK


def _dry_run(repo: Path, adapter: ClaudeAdapter, out: TextIO) -> int:
    """Print the prompt and argv for the first open step; change nothing."""
    plan_path = repo / PLAN_FILENAME
    if not plan_path.is_file():
        _say(out, f"No {PLAN_FILENAME} in {repo}. Create one with `/plan-commits <task>`.")
        return EXIT_STOPPED
    text = plan_path.read_text(encoding="utf-8")
    plan = parse_plan(text)
    step = first_open_step(plan)
    if step is None:
        _say(out, "No open steps left in the plan.")
        return EXIT_STOPPED
    prompt = implement_prompt(plan, step, step_text(text, step))
    _say(out, f"Step {step.number}/{len(plan.steps)}: {step.message}")
    _say(out, "Command (cwd = repo root):")
    _say(out, "  " + " ".join(shlex.quote(a) for a in adapter.argv("<prompt>")))
    _say(out, "Prompt:")
    _say(out, prompt)
    return EXIT_OK


def run_next_command(
    repo: Path,
    *,
    config_path: str | None = None,
    agent_timeout: float = DEFAULT_AGENT_TIMEOUT,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
    dry_run: bool = False,
    out: TextIO | None = None,
) -> int:
    """CLI entry: resolve the agent from the config, then run (or dry-run) one step."""
    out = out or sys.stdout
    try:
        config = load_config(find_config_path(config_path))
        agent = pick_claude_agent(config)
        adapter = ClaudeAdapter(agent.command, agent.model)
        if dry_run:
            return _dry_run(repo, adapter, out)
    except (ConfigError, PlanError, OSError) as exc:
        _say(out, f"Error: {exc}")
        return EXIT_FAILED
    return run_next(repo, adapter, out, agent_timeout=agent_timeout, verify_timeout=verify_timeout)
