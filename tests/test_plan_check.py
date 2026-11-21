from pathlib import Path

import pytest

from baton.plan import MilestoneRef, parse_plan
from baton.plan_check import PlanHeader, assemble_plan, validate_plan

FIXTURES = Path(__file__).parent / "fixtures" / "plans"


def _errors(name: str, milestone_id: str = "M2") -> list[str]:
    return validate_plan((FIXTURES / name).read_text(encoding="utf-8"), milestone_id)


def test_valid_generated_plan_has_no_errors():
    assert _errors("generated_valid.md") == []


def test_distributed_planning_skill_example_passes_validation():
    skill = Path(__file__).resolve().parents[1] / "integrations" / "plan-commits" / "SKILL.md"
    example = skill.read_text(encoding="utf-8").split("```markdown\n", 1)[1].split("```", 1)[0]

    assert validate_plan(example, "M2") == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("generated_two_steps.md", "plan must have 3-8 steps, found 2"),
        ("generated_no_verify.md", "step 2: missing '**Verify:**'"),
        ("generated_bad_size.md", "step 2: '**Size:**' must be S, M or L"),
        (
            "generated_bad_message.md",
            "step 2: commit message 'Render the widgets.' is not '<type>(<scope>): <description>'",
        ),
        ("generated_done_step.md", "step 1: must be open '[ ]', found done"),
        ("generated_gap.md", "steps must be numbered 1..3 in order, found [1, 2, 4]"),
        ("generated_wrong_milestone.md", "Milestone header must name M2, found M3"),
        ("generated_no_goal.md", "missing '## Goal' section"),
    ],
)
def test_invalid_generated_plans_report_specific_errors(name: str, expected: str):
    assert _errors(name) == [expected]


def test_unparsable_plan_reports_parse_error():
    errors = validate_plan("# Plan: x\n\nno steps here\n", "M2")
    assert errors == ["plan does not parse: missing '## Steps' section"]


def test_too_many_steps():
    step = (
        "### {n}. [ ] feat(x): step {n}\n- **Why:** w\n- **Changes:** c\n"
        "- **Done when:** d\n- **Verify:** `v`\n- **Size:** S\n\n"
    )
    text = (
        "# Plan: Big\nMilestone: M2 – Widgets (docs/ROADMAP.md, local)\n\n## Goal\nG\n\n"
        "## Steps\n\n" + "".join(step.format(n=n) for n in range(1, 10))
    )
    assert validate_plan(text, "M2") == ["plan must have 3-8 steps, found 9"]


@pytest.mark.parametrize(
    "message",
    ["feat: add x", "fix(api)!: drop field", "refactor(next): move routed adapter"],
)
def test_conventional_commit_variants_are_accepted(message: str):
    text = (FIXTURES / "generated_valid.md").read_text(encoding="utf-8")
    text = text.replace("feat(widget): render widgets", message)
    assert validate_plan(text, "M2") == []


def test_missing_milestone_header():
    text = (FIXTURES / "generated_valid.md").read_text(encoding="utf-8")
    text = text.replace("Milestone: M2 – Widgets (docs/ROADMAP.md, tracked)\n", "")
    assert validate_plan(text, "M2") == ["Milestone header must name M2, found none"]


HEADER = PlanHeader(
    created="2026-11-19",
    branch="feat/m2",
    base="def5678",
    milestone=MilestoneRef("M2", "Widgets", "docs/ROADMAP.md", "tracked"),
)


def test_assemble_plan_replaces_header_and_passes_validation():
    reply = (FIXTURES / "generated_valid.md").read_text(encoding="utf-8")
    text = assemble_plan(reply, HEADER)
    plan = parse_plan(text)
    assert (plan.created, plan.branch, plan.base) == ("2026-11-19", "feat/m2", "def5678")
    assert plan.milestone == HEADER.milestone
    assert plan.title == "Widget support"
    assert validate_plan(text, "M2") == []
    assert text.endswith("\n## Log\n")


def test_assemble_plan_strips_fences_prose_and_log_entries():
    plan = (FIXTURES / "generated_valid.md").read_text(encoding="utf-8")
    plan = plan.replace("## Log\n", "## Log\n- leftover\n")
    reply = f"Here is the plan:\n\n```markdown\n{plan}```\n\nLet me know!\n"
    text = assemble_plan(reply, HEADER)
    assert text.startswith("# Plan: Widget support\nCreated: 2026-11-19")
    assert "```" not in text
    assert "Here is" not in text
    assert "Let me know" not in text
    assert "leftover" not in text
    assert validate_plan(text, "M2") == []


def test_assemble_plan_adds_header_when_planner_omitted_it():
    reply = (FIXTURES / "generated_valid.md").read_text(encoding="utf-8")
    reply = "\n".join(
        ln for ln in reply.splitlines() if not ln.startswith(("Created", "Milestone"))
    )
    assert validate_plan(assemble_plan(reply, HEADER), "M2") == []


def test_assemble_plan_without_title_is_left_for_validation():
    text = assemble_plan("I could not plan this.\n", HEADER)
    assert text == "I could not plan this.\n"
    assert validate_plan(text, "M2") == ["plan does not parse: missing '## Steps' section"]


def test_plan_with_and_without_baseline_is_valid():
    assert _errors("generated_with_baseline.md") == []
    assert _errors("generated_valid.md") == []


def test_assemble_plan_keeps_the_planners_baseline():
    reply = (FIXTURES / "generated_with_baseline.md").read_text(encoding="utf-8")
    text = assemble_plan(reply, HEADER)
    assert parse_plan(text).baseline == "uv run pytest -q && uv run ruff check ."
    assert validate_plan(text, "M2") == []
    assert parse_plan(assemble_plan(_valid_reply(), HEADER)).baseline is None


def _valid_reply() -> str:
    return (FIXTURES / "generated_valid.md").read_text(encoding="utf-8")
