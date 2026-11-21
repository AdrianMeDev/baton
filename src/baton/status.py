"""``baton status``: show plan, milestone and commit-gate state (read-only)."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import TextIO

from baton.config import ConfigError, find_config_path, load_config
from baton.gate import GateResult, check_gate
from baton.git import PLAN_FILENAME, GitError
from baton.limits import LimitStateError, LimitStore
from baton.plan import Plan, PlanError, Step, first_open_step, load_plan, pending_step
from baton.roadmap import RoadmapError, find_roadmap, load_roadmap
from baton.worktree import find_worktree

SYMBOLS = {"open": "[ ]", "pending": "[~]", "done": "[x]", "skipped": "[-]"}


def _milestone_line(plan: Plan, root: Path) -> str:
    ref = plan.milestone
    if ref.tracking == "none" or ref.id is None:
        return "Milestone: none"
    line = f"Milestone: {ref.id} {ref.name}".rstrip()
    path = find_roadmap(root)
    if path is None:
        return line
    try:
        found = load_roadmap(path).get(ref.id)
    except (RoadmapError, OSError, UnicodeDecodeError) as exc:
        return f"{line} (roadmap unreadable: {exc})"
    return f"{line} [{SYMBOLS[found.status][1]}]" if found else line


def _step_line(step: Step, total: int) -> str:
    size = f" ({step.size})" if step.size else ""
    return f"  {SYMBOLS[step.status]} {step.number}/{total} {step.message}{size}"


def _gate_lines(plan: Plan, root: Path) -> list[str]:
    step = pending_step(plan)
    if step is None:
        return []
    try:
        gate: GateResult = check_gate(plan, root)
    except GitError as exc:
        return [f"Gate: cannot be evaluated: {exc}"]
    if gate.state == "blocked_dirty":
        return [
            f"Gate: BLOCKED - step {step.number} is not committed. Changed files:",
            *(f"  {path}" for path in gate.dirty),
            "Commit it:",
            "  git add -A",
            f"  git commit -m {shlex.quote(step.message)}",
        ]
    if gate.state == "committed":
        return [
            f"Gate: step {step.number} is committed (HEAD {gate.head}) but not yet marked [x];"
            " the next step will do that."
        ]
    if gate.state == "discarded":
        return [
            f"Gate: step {step.number} has no new commit and a clean tree"
            " - the changes were discarded or stashed."
        ]
    return [f"Gate: {gate.state}"]


def _plan_status(root: Path, out: TextIO | None = None) -> int:
    """Print the status of the plan in ``root``. Exit 0, or 1 for an unreadable plan."""
    out = sys.stdout if out is None else out
    path = root / PLAN_FILENAME
    if not path.is_file():
        print(f"No {PLAN_FILENAME} in {root}. Create one with /plan-commits.", file=out)
        return 0
    try:
        plan = load_plan(path)
    except (PlanError, OSError, UnicodeDecodeError) as exc:
        print(f"Cannot read {path}: {exc}", file=out)
        return 1

    worktree = find_worktree(root, plan)
    if worktree is not None and (worktree / PLAN_FILENAME).is_file():
        try:
            lead = load_plan(worktree / PLAN_FILENAME)
        except (PlanError, OSError, UnicodeDecodeError) as exc:
            print(f"Cannot read {worktree / PLAN_FILENAME}: {exc}", file=out)
            return 1
        print(f"Plan: milestone worktree {worktree} (leads)", file=out)
        if (worktree / PLAN_FILENAME).read_bytes() != path.read_bytes():
            print(
                f"Warning: {path} differs from the worktree copy; the worktree copy is shown.",
                file=out,
            )
        plan, root = lead, worktree

    total = len(plan.steps)
    print(plan.title, file=out)
    print(_milestone_line(plan, root), file=out)
    if plan.branch:
        print(f"Branch: {plan.branch}", file=out)
    print("\nSteps:", file=out)
    for step in plan.steps:
        print(_step_line(step, total), file=out)

    print(file=out)
    pending = pending_step(plan)
    nxt = first_open_step(plan)
    if pending is not None:
        print(f"Current: step {pending.number}/{total} {pending.message}", file=out)
    elif nxt is not None:
        print(f"Next: step {nxt.number}/{total} {nxt.message}", file=out)
    else:
        print("All steps are finished.", file=out)
    for line in _gate_lines(plan, root):
        print(line, file=out)
    return 0


def run_status(root: Path, out: TextIO | None = None, *, config_path: str | None = None) -> int:
    """Report plan and configured availability without changing plan or cooldown state."""
    out = sys.stdout if out is None else out
    code = _plan_status(root, out)
    print("\nAgents:", file=out)
    try:
        config = load_config(find_config_path(config_path))
        if not config.agents:
            print("  (none configured)", file=out)
            return code
        active = LimitStore().active()
        for name in config.agents:
            deadline = active.get(name)
            if deadline is None:
                print(f"  {name}: available (no active cooldown)", file=out)
            else:
                local = deadline.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (%z)")
                print(f"  {name}: cooldown until {local}", file=out)
    except (ConfigError, LimitStateError) as exc:
        print(f"Cannot report agent availability: {exc}", file=out)
        return 1
    return code
