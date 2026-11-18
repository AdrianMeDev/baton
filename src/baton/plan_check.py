"""Structural checks for commit plans that an agent generated headless."""

from __future__ import annotations

import re

from baton.plan import PlanError, parse_plan

MIN_STEPS = 3
MAX_STEPS = 8

COMMIT_TYPES = ("feat", "fix", "refactor", "test", "docs", "chore", "perf", "build", "ci")
_COMMIT_RE = re.compile(rf"^(?:{'|'.join(COMMIT_TYPES)})(?:\([a-z0-9][a-z0-9._/-]*\))?!?: \S.*$")


def validate_plan(text: str, milestone_id: str) -> list[str]:
    """Readable reasons why ``text`` is not an acceptable fresh plan for ``milestone_id``.

    An empty list means the plan is valid.
    """
    try:
        plan = parse_plan(text)
    except PlanError as exc:
        return [f"plan does not parse: {exc}"]

    errors: list[str] = []
    if not plan.title:
        errors.append("missing '# Plan: <title>' heading")
    if not any(line.strip() == "## Goal" for line in text.splitlines()):
        errors.append("missing '## Goal' section")
    if plan.milestone.id != milestone_id:
        found = plan.milestone.id or "none"
        errors.append(f"Milestone header must name {milestone_id}, found {found}")

    count = len(plan.steps)
    if not MIN_STEPS <= count <= MAX_STEPS:
        errors.append(f"plan must have {MIN_STEPS}-{MAX_STEPS} steps, found {count}")
    numbers = [s.number for s in plan.steps]
    if numbers != list(range(1, count + 1)):
        errors.append(f"steps must be numbered 1..{count} in order, found {numbers}")

    for step in plan.steps:
        where = f"step {step.number}"
        if step.status != "open":
            errors.append(f"{where}: must be open '[ ]', found {step.status}")
        if not step.message:
            errors.append(f"{where}: missing commit message")
        elif not _COMMIT_RE.match(step.message):
            errors.append(
                f"{where}: commit message {step.message!r} is not '<type>(<scope>): <description>'"
            )
        for label, value in (
            ("Why", step.why),
            ("Changes", step.changes),
            ("Done when", step.done_when),
            ("Verify", step.verify),
        ):
            if not value.strip():
                errors.append(f"{where}: missing '**{label}:**'")
        if step.size is None:
            errors.append(f"{where}: '**Size:**' must be S, M or L")
    return errors
