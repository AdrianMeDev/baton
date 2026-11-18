"""Prompt templates for the agents that baton drives."""

from __future__ import annotations

from baton.plan import Plan, Step

RULES = """\
Rules:
- Implement ONLY this step. Do not start, prepare or anticipate any other step.
- Do not run any git command that changes state (no commit, add, stash, reset, checkout, push).
  The user reviews and commits.
- Do not edit `.agent-plan.md`; baton maintains it.
- Include the tests this step asks for and follow the project's existing conventions.
- Re-read the files you touch before editing; do not rely on assumptions.
- If the step cannot be done as written, stop and explain why instead of widening the scope.
- When finished, reply with a short summary of what you changed."""


def step_text(plan_text: str, step: Step) -> str:
    """The raw lines of ``step`` (heading to last body line) from the plan file text."""
    lines = plan_text.splitlines()
    return "\n".join(lines[step.start_line - 1 : step.end_line]).strip()


def implement_prompt(plan: Plan, step: Step, text: str) -> str:
    """Prompt for implementing exactly one plan step; ``text`` is the step's raw plan lines."""
    total = len(plan.steps)
    return (
        f"You are implementing step {step.number} of {total} of the plan "
        f'"{plan.title}" in the current repository.\n\n'
        f"{RULES}\n\n"
        f"Step:\n\n{text}\n"
    )
