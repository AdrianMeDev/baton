import json

import pytest

from baton.plan import append_log, parse_plan, set_step_status
from baton.repair import (
    apply_repair,
    parse_repair,
    repair_from_reply,
    repair_prompt,
    validate_repair,
)

EVIDENCE = "baton-evidence: " + json.dumps({"step": 1, "verdict": "pass"})

PLAN = f"""\
# Plan: Widget support
Created: 2026-11-18 · Branch: feat/widgets · Base: abc1234
Milestone: none

## Goal
Widgets work.

## Steps

### 1. [x] – abc1234 feat(widget): add widget model
- **Why:** needed first.
- **Changes:** `src/widget.py`
- **Done when:** model exists
- **Verify:** `pytest -q`

### 2. [ ] feat(widget): render widgets
- **Why:** users see them.
- **Changes:** `src/render.py`
- **Done when:** rendering works
- **Verify:** `pytest -q`

### 3. [ ] docs(widget): document widgets
- **Why:** users find them.
- **Changes:** `README.md`
- **Done when:** README explains widgets
- **Verify:** `pytest -q`

## Log
- started
- {EVIDENCE}
"""


def _step(n: int, message: str, **overrides: str) -> str:
    fields = {
        "Why": f"why {n}",
        "Changes": f"changes {n}",
        "Done when": f"done {n}",
        "Verify": "`pytest -q`",
    }
    fields.update(overrides)
    body = "\n".join(f"- **{k}:** {v}" for k, v in fields.items())
    return f"### {n}. [ ] {message}\n{body}\n"


def _repair(*steps: str) -> str:
    return "\n".join(steps)


def test_extend_adds_steps_and_logs_the_repair():
    replacement = _repair(
        _step(2, "feat(widget): add render base"),
        _step(3, "feat(widget): render widgets"),
        _step(4, "docs(widget): document widgets"),
    )

    result = apply_repair(PLAN, 2, replacement, "render needs a base class")

    assert result.ok
    plan = parse_plan(result.text)
    assert [s.message for s in plan.steps] == [
        "feat(widget): add widget model",
        "feat(widget): add render base",
        "feat(widget): render widgets",
        "docs(widget): document widgets",
    ]
    assert plan.log[:2] == ("started", EVIDENCE)
    assert plan.log[2] == (
        "plan repaired: step 2: render needs a base class"
        " | old: feat(widget): render widgets; docs(widget): document widgets"
        " | new: feat(widget): add render base; feat(widget): render widgets;"
        " docs(widget): document widgets"
    )
    assert result.text.startswith(PLAN.split("### 2.")[0])


def test_insert_in_the_middle_renumbers_later_steps():
    replacement = _repair(
        _step(2, "chore(widget): prepare render"),
        _step(3, "feat(widget): render widgets"),
        _step(4, "docs(widget): document widgets"),
    )

    result = apply_repair(PLAN, 2, replacement, "x")

    assert result.ok
    assert [s.number for s in parse_plan(result.text).steps] == [1, 2, 3, 4]


def test_split_replaces_one_step_by_two():
    replacement = _repair(
        _step(2, "feat(widget): render text widgets"),
        _step(3, "feat(widget): render image widgets"),
        _step(4, "docs(widget): document widgets"),
    )

    assert apply_repair(PLAN, 2, replacement, "too big").ok


def test_shrinking_the_tail_is_allowed():
    replacement = _step(3, "docs(widget): document widgets")

    result = apply_repair(PLAN, 3, replacement, "reworded")

    assert result.ok


def test_reply_with_prose_and_fence_is_cleaned():
    reply = (
        "Here is the repair:\n\n```markdown\n"
        + _repair(_step(2, "feat(widget): render widgets"), _step(3, "docs(widget): docs"))
        + "```\nDone.\n"
    )

    result = repair_from_reply(PLAN, 2, reply, "r")

    assert result.ok
    assert parse_plan(result.text).steps[2].message == "docs(widget): docs"


def test_reply_stops_at_a_next_level_two_heading():
    reply = _step(2, "feat(widget): a") + _step(3, "feat(widget): b") + "\n## Log\n- evil\n"

    result = repair_from_reply(PLAN, 2, reply, "r")

    assert result.ok
    assert "evil" not in result.text


def test_needs_decision_is_reported_and_text_is_untouched():
    result = repair_from_reply(PLAN, 2, "BATON-NEEDS-DECISION: pick a database", "r")

    assert not result.ok
    assert result.needs_decision == "pick a database"
    assert result.text == PLAN
    assert result.errors == ()


def test_reply_without_steps_is_rejected():
    result = repair_from_reply(PLAN, 2, "I cannot help with that.", "r")

    assert result.errors == ("reply contains no '### <n>. [ ] <message>' step",)
    assert result.text == PLAN


def _rejects(replacement: str, fragment: str, step: int = 2) -> None:
    result = apply_repair(PLAN, step, replacement, "r")
    assert result.text == PLAN
    assert any(fragment in e for e in result.errors), result.errors


def test_rejects_wrong_first_number():
    _rejects(_step(3, "feat(widget): a"), "numbered 1..")


def test_rejects_gap_in_numbering():
    _rejects(_repair(_step(2, "feat(widget): a"), _step(4, "feat(widget): b")), "numbered 1..")


def test_rejects_bad_commit_message():
    _rejects(_step(2, "Render the widgets."), "is not '<type>(<scope>): <description>'")


@pytest.mark.parametrize("label", ["Why", "Changes", "Done when", "Verify"])
def test_rejects_empty_required_field(label: str):
    _rejects(_step(2, "feat(widget): a", **{label: ""}), f"missing '**{label}:**'")


def test_rejects_step_that_is_not_open():
    replacement = _step(2, "feat(widget): a").replace("[ ]", "[x] – abc1234")

    _rejects(replacement, "must be open")


def test_rejects_more_than_twelve_steps():
    replacement = _repair(*(_step(n, f"feat(widget): s{n}") for n in range(2, 14)))

    _rejects(replacement, "at most 12 steps")


def test_rejects_second_repair_of_the_same_step():
    once = apply_repair(PLAN, 2, _step(2, "feat(widget): a"), "first")
    assert once.ok

    again = apply_repair(once.text, 2, _step(2, "feat(widget): b"), "second")

    assert again.text == once.text
    assert any("already repaired" in e for e in again.errors)


def test_unknown_step_is_rejected():
    result = apply_repair(PLAN, 9, _step(9, "feat(widget): a"), "r")

    assert result.errors == ("step 9 does not exist in the plan",)


def test_validate_rejects_changed_earlier_step():
    new = PLAN.replace("add widget model", "add the widget model")

    assert any("must stay unchanged" in e for e in validate_repair(PLAN, new, 2))


def test_validate_rejects_changed_earlier_status():
    new = set_step_status(PLAN, 2, "pending", "abc1234")

    assert any("must stay unchanged" in e for e in validate_repair(PLAN, new, 3))


def test_validate_rejects_dropped_evidence_line():
    new = PLAN.replace(f"- {EVIDENCE}\n", "")

    assert "baton-evidence log lines must stay unchanged" in validate_repair(PLAN, new, 2)


def test_validate_rejects_altered_evidence_line():
    new = PLAN.replace('"pass"', '"fail"')

    assert "baton-evidence log lines must stay unchanged" in validate_repair(PLAN, new, 2)


def test_validate_rejects_closed_step_after_the_problem_step():
    old = set_step_status(PLAN, 3, "skipped")

    assert any("must still be open" in e for e in validate_repair(old, old, 2))


def test_validate_accepts_log_growth():
    assert validate_repair(PLAN, append_log(PLAN, "note"), 2) == []


def test_crlf_plan_keeps_its_line_endings():
    crlf = PLAN.replace("\n", "\r\n")

    result = apply_repair(crlf, 2, _step(2, "feat(widget): a"), "r")

    assert result.ok
    assert "\r\n" in result.text
    assert result.text.replace("\r\n", "").count("\n") == 0


def test_parse_repair_keeps_only_the_step_blocks():
    proposal = parse_repair("intro\n" + _step(2, "feat(widget): a") + "\n")

    assert proposal.replacement.startswith("### 2. [ ] feat(widget): a")
    assert proposal.replacement.endswith("`pytest -q`")


def test_repair_prompt_carries_reason_step_and_diff():
    plan = parse_plan(PLAN)

    prompt = repair_prompt(plan, PLAN, plan.steps[1], "needs a base", "diff --git a/x b/x")

    assert "needs a base" in prompt
    assert "### 2. [ ] feat(widget): render widgets" in prompt
    assert "diff --git a/x b/x" in prompt
    assert "BATON-NEEDS-DECISION:" in prompt
    assert "at most 12 steps" in prompt


def test_parse_repair_log_round_trips_what_apply_wrote():
    from baton.repair import parse_repair_log

    result = apply_repair(PLAN, 2, _step(2, "feat(widget): a"), "x | y")
    entry = parse_plan(result.text).log[-1]

    record = parse_repair_log(entry)

    assert record is not None
    assert (record.step, record.reason) == (2, "x | y")
    assert record.old == "feat(widget): render widgets; docs(widget): document widgets"
    assert record.new == "feat(widget): a"
    assert parse_repair_log("2026-11-19: step 1 done") is None
