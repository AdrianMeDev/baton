from pathlib import Path

import pytest

from baton.plan import MilestoneRef, PlanError, load_plan, parse_milestone, parse_plan

FIXTURES = Path(__file__).parent / "fixtures" / "plans"


def test_fresh_plan_header_and_steps():
    plan = load_plan(FIXTURES / "fresh.md")
    assert plan.title == "Add widget support"
    assert (plan.created, plan.branch, plan.base) == ("2026-11-18", "feat/widgets", "abc1234")
    assert plan.milestone == MilestoneRef("M2", "Widgets", "docs/ROADMAP.md", "tracked")
    assert [s.number for s in plan.steps] == [1, 2]
    first = plan.steps[0]
    assert first.status == "open"
    assert first.message == "feat(widget): add widget model"
    assert first.why == "needed first."
    assert first.changes == "`src/widget.py`"
    assert first.done_when == "model exists"
    assert first.verify == "`pytest -q`"
    assert first.size is None
    assert plan.log == ()


def test_all_status_markers():
    plan = load_plan(FIXTURES / "mixed.md")
    done, skipped, pending, open_ = plan.steps
    assert (done.status, done.commit, done.message) == ("done", "abc1234", "feat(a): first")
    assert skipped.status == "skipped"
    assert (pending.status, pending.pending_on, pending.message) == (
        "pending",
        "def5678",
        "feat(c): pending",
    )
    assert open_.status == "open" and open_.commit is None and open_.pending_on is None


def test_size_continuation_and_unknown_bullets():
    plan = load_plan(FIXTURES / "mixed.md")
    assert [s.size for s in plan.steps] == ["S", None, "L", "M"]
    assert plan.steps[2].changes == "`a.py`,\n`b.py`"


def test_line_spans():
    text = (FIXTURES / "mixed.md").read_text().splitlines()
    for step in load_plan(FIXTURES / "mixed.md").steps:
        assert text[step.start_line - 1].startswith(f"### {step.number}.")
        assert step.end_line >= step.start_line
    first, second = load_plan(FIXTURES / "mixed.md").steps[:2]
    assert first.end_line == second.start_line - 1


def test_log_entries_and_unknown_sections_ignored():
    plan = load_plan(FIXTURES / "mixed.md")
    assert plan.log == ("2026-11-18: step 1 committed", "skipped step 2")
    assert len(plan.steps) == 4


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "M2 – Plan (docs/ROADMAP.md, tracked)",
            MilestoneRef("M2", "Plan", "docs/ROADMAP.md", "tracked"),
        ),
        ("M3 - Mixed (ROADMAP.md, local)", MilestoneRef("M3", "Mixed", "ROADMAP.md", "local")),
        ("none", MilestoneRef(None, "", None, "none")),
        ("", MilestoneRef(None, "", None, "none")),
        ("Something else", MilestoneRef(None, "Something else", None, "none")),
    ],
)
def test_parse_milestone(value, expected):
    assert parse_milestone(value) == expected


def test_plan_without_milestone():
    plan = load_plan(FIXTURES / "no_milestone.md")
    assert plan.milestone.tracking == "none"
    assert plan.steps[0].message == "fix(thing): repair it"


def test_missing_steps_section():
    with pytest.raises(PlanError, match="## Steps"):
        load_plan(FIXTURES / "no_steps.md")


def test_unknown_marker_reports_line():
    with pytest.raises(PlanError, match="line 9") as exc:
        load_plan(FIXTURES / "bad_marker.md")
    assert exc.value.line == 9


def test_duplicate_step_number_reports_line():
    with pytest.raises(PlanError, match="duplicate step number 1") as exc:
        load_plan(FIXTURES / "duplicate.md")
    assert exc.value.line == 9


def test_empty_steps_section():
    with pytest.raises(PlanError, match="no steps"):
        parse_plan("# Plan: x\n\n## Steps\n\n## Log\n")
