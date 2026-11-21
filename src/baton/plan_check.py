"""Structural checks for commit plans that an agent generated headless."""

from __future__ import annotations

import re
from dataclasses import dataclass

from baton.plan import MilestoneRef, PlanError, parse_plan

MIN_STEPS = 3
MAX_STEPS = 8

COMMIT_TYPES = ("feat", "fix", "refactor", "test", "docs", "chore", "perf", "build", "ci")
_COMMIT_RE = re.compile(rf"^(?:{'|'.join(COMMIT_TYPES)})(?:\([a-z0-9][a-z0-9._/-]*\))?!?: \S.*$")


def is_commit_message(message: str) -> bool:
    return _COMMIT_RE.match(message) is not None


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
        elif not is_commit_message(step.message):
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


@dataclass(frozen=True)
class PlanHeader:
    """The header values baton owns; they replace whatever the planner wrote."""

    created: str
    branch: str
    base: str
    milestone: MilestoneRef

    def lines(self) -> list[str]:
        m = self.milestone
        return [
            f"Created: {self.created} · Branch: {self.branch} · Base: {self.base}",
            f"Milestone: {m.id} – {m.name} ({m.roadmap}, {m.tracking})",
        ]


def assemble_plan(output: str, header: PlanHeader) -> str:
    """Turn the planner's reply into plan file text with baton's header and an empty Log.

    Prose or a Markdown fence before ``# Plan:`` and everything from a closing fence or the
    ``## Log`` heading on are dropped. Without a ``# Plan:`` heading the reply is returned
    stripped, so that validation reports it.
    """
    lines = output.strip().splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("# Plan:")), None)
    if start is None:
        return output.strip() + "\n"
    body = lines[start:]
    end = next(
        (i for i, ln in enumerate(body) if ln.strip().startswith("```") or ln.strip() == "## Log"),
        len(body),
    )
    body = body[:end]
    section = next((i for i, ln in enumerate(body) if ln.startswith("## ")), len(body))
    rest = body[section:]
    baseline = [ln.strip() for ln in body[1:section] if ln.strip().startswith("Baseline:")][:1]
    while rest and not rest[-1].strip():
        rest.pop()
    return "\n".join([body[0].rstrip(), *header.lines(), *baseline, "", *rest, "", "## Log", ""])
