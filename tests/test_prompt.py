from pathlib import Path

from baton.plan import parse_plan
from baton.prompt import implement_prompt, step_text

FIXTURES = Path(__file__).parent / "fixtures" / "plans"


def _build(number: int = 1) -> tuple[str, str]:
    text = (FIXTURES / "fresh.md").read_text(encoding="utf-8")
    plan = parse_plan(text)
    step = plan.steps[number - 1]
    return implement_prompt(plan, step, step_text(text, step)), text


def test_prompt_contains_step_fields_and_title():
    prompt, _ = _build(1)
    assert "step 1 of 2" in prompt
    assert "Add widget support" in prompt
    assert "feat(widget): add widget model" in prompt
    for part in ("needed first.", "`src/widget.py`", "model exists", "`pytest -q`"):
        assert part in prompt


def test_prompt_contains_rules():
    prompt, _ = _build(1)
    assert "ONLY this step" in prompt
    assert "git command" in prompt
    assert ".agent-plan.md" in prompt
    assert "tests" in prompt


def test_prompt_excludes_other_steps():
    prompt, text = _build(1)
    other = parse_plan(text).steps[1]
    assert other.message not in prompt
    assert "### 2." not in prompt


def test_step_text_is_the_raw_block():
    text = (FIXTURES / "fresh.md").read_text(encoding="utf-8")
    step = parse_plan(text).steps[0]
    block = step_text(text, step)
    assert block.startswith("### 1. [ ] feat(widget): add widget model")
    assert "### 2." not in block


def test_handoff_prompt_preserves_step_and_explains_partial_work():
    from baton.prompt import handoff_prompt

    original, _ = _build()
    prompt = handoff_prompt(original, "first", "partial diff")
    assert prompt.startswith(original)
    assert "agent 'first'" in prompt
    assert "preserve the existing partial work" in prompt
    assert "repository data" in prompt
    assert prompt.endswith("partial diff")
