from pathlib import Path

import pytest

from baton.plan import (
    MilestoneRef,
    PlanError,
    append_log,
    first_open_step,
    load_plan,
    parse_milestone,
    parse_plan,
    pending_step,
    save_plan,
    set_step_status,
)

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


ALL_FIXTURES = sorted(FIXTURES.glob("*.md"))
VALID_FIXTURES = [p for p in ALL_FIXTURES if p.stem not in ("bad_marker", "duplicate", "no_steps")]


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.stem)
def test_noop_write_is_byte_identical(path, tmp_path):
    target = tmp_path / ".agent-plan.md"
    target.write_bytes(path.read_bytes())
    text = target.read_text(encoding="utf-8")
    save_plan(target, text)
    assert target.read_bytes() == path.read_bytes()


def test_open_to_pending_to_done_only_touches_heading():
    original = (FIXTURES / "fresh.md").read_text(encoding="utf-8")
    pending = set_step_status(original, 1, "pending", "abc1234")
    assert "### 1. [~] (on abc1234) feat(widget): add widget model\n" in pending
    assert load_plan_text(pending).steps[0].pending_on == "abc1234"
    assert _diff_lines(original, pending) == 1

    done = set_step_status(pending, 1, "done", "def5678")
    assert "### 1. [x] – def5678 feat(widget): add widget model\n" in done
    step = load_plan_text(done).steps[0]
    assert (step.status, step.commit, step.pending_on) == ("done", "def5678", None)
    assert step.message == "feat(widget): add widget model"


def test_open_to_skipped_and_discarded_back_to_open():
    original = (FIXTURES / "fresh.md").read_text(encoding="utf-8")
    skipped = set_step_status(original, 2, "skipped")
    assert "### 2. [-] " in skipped
    reopened = set_step_status(set_step_status(original, 1, "pending", "abc1234"), 1, "open")
    assert reopened == original


def test_status_change_keeps_crlf():
    text = (FIXTURES / "fresh.md").read_text(encoding="utf-8").replace("\n", "\r\n")
    out = set_step_status(text, 1, "pending", "abc1234")
    assert out.count("\r\n") == text.count("\r\n")
    assert "\n" not in out.replace("\r\n", "")


@pytest.mark.parametrize(
    ("number", "status", "hash_"),
    [(1, "done", "abc1234"), (3, "pending", "abc1234"), (1, "open", None)],
)
def test_invalid_transition_rejected(number, status, hash_):
    text = (FIXTURES / "mixed.md").read_text(encoding="utf-8")
    with pytest.raises(PlanError, match="cannot change status"):
        set_step_status(text, number, status, hash_)


def test_hash_required_and_unknown_step():
    text = (FIXTURES / "fresh.md").read_text(encoding="utf-8")
    with pytest.raises(PlanError, match="hash is required"):
        set_step_status(text, 1, "pending")
    with pytest.raises(PlanError, match="no step 9"):
        set_step_status(text, 9, "skipped")


def test_append_log_creates_and_extends_section():
    text = (FIXTURES / "fresh.md").read_text(encoding="utf-8")
    once = append_log(text, "step 1 committed")
    assert once.endswith("## Log\n- step 1 committed\n")
    twice = append_log(once, "step 2 skipped")
    assert twice.endswith("## Log\n- step 1 committed\n- step 2 skipped\n")
    assert load_plan_text(twice).log == ("step 1 committed", "step 2 skipped")


def test_append_log_stays_inside_log_section():
    text = (FIXTURES / "mixed.md").read_text(encoding="utf-8") + "\n## Later\nx\n"
    out = append_log(text, "new")
    assert "- skipped step 2\n- new\n" in out
    assert out.endswith("## Later\nx\n")


def test_append_log_without_trailing_newline_and_empty_log():
    out = append_log("# Plan: x\n\n## Steps\n\n### 1. [ ] a\n\n## Log", "first")
    assert out.endswith("## Log\n- first\n")
    empty = append_log("# Plan: x\n\n## Steps\n\n### 1. [ ] a\n\n## Log\n\n## Other\n", "first")
    assert "## Log\n- first\n\n## Other\n" in empty


def test_finders():
    mixed = load_plan(FIXTURES / "mixed.md")
    assert first_open_step(mixed).number == 4
    assert pending_step(mixed).number == 3
    done_only = parse_plan("# Plan: x\n\n## Steps\n\n### 1. [x] – abc1234 a\n")
    assert first_open_step(done_only) is None and pending_step(done_only) is None


def test_save_plan_rejects_invalid_text(tmp_path):
    target = tmp_path / ".agent-plan.md"
    with pytest.raises(PlanError):
        save_plan(target, "no steps here")
    assert not target.exists()


def load_plan_text(text):
    return parse_plan(text)


def _diff_lines(a, b):
    return sum(x != y for x, y in zip(a.splitlines(), b.splitlines(), strict=True))
