"""The commit gate: never start a new step while the previous one is uncommitted.

Mirrors section 2 of the ``/next`` skill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from baton.git import GitError, dirty_paths, head
from baton.plan import Plan, PlanError, append_log, pending_step, set_step_status

GateState = Literal["no_pending", "blocked_dirty", "committed", "discarded", "overridden"]


@dataclass(frozen=True)
class GateResult:
    state: GateState
    step: int | None = None  # number of the pending step, if any
    dirty: tuple[str, ...] = field(default=())
    head: str | None = None  # current HEAD (short)


def _same_commit(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def check_gate(plan: Plan, repo: Path, override: bool = False) -> GateResult:
    """Evaluate the gate for the ``[~]`` step of ``plan``; raises ``GitError`` outside a repo."""
    step = pending_step(plan)
    if step is None:
        return GateResult("no_pending")

    current = head(repo)
    dirty = tuple(dirty_paths(repo))
    if dirty:
        state: GateState = "overridden" if override else "blocked_dirty"
        return GateResult(state, step.number, dirty, current)
    if step.pending_on and _same_commit(step.pending_on, current):
        return GateResult("discarded", step.number, (), current)
    return GateResult("committed", step.number, (), current)


def apply_gate(text: str, result: GateResult, date: str) -> str:
    """Mark the pending step ``[x] – <hash>`` and log it for ``committed``/``overridden``.

    Other states leave ``text`` unchanged. ``date`` is the ISO date used in the Log line.
    """
    if result.state not in ("committed", "overridden"):
        return text
    if result.step is None or result.head is None:
        raise PlanError(f"gate result {result.state} lacks step or HEAD")
    text = set_step_status(text, result.step, "done", result.head)
    if result.state == "overridden":
        line = f"{date}: step {result.step} continued without commit (HEAD {result.head})"
    else:
        line = f"{date}: step {result.step} committed ({result.head})"
    return append_log(text, line)


__all__ = ["GateResult", "GateState", "GitError", "apply_gate", "check_gate"]
