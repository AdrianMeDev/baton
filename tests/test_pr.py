import pytest
from test_next import PLAN, git

from baton.plan import PlanError
from baton.pr import milestone_report


def test_open_steps_block_publication(tmp_path):
    with pytest.raises(PlanError, match="Complete"):
        milestone_report(tmp_path, PLAN.format(s1="[ ]", verify="true"))


def test_legacy_report_is_ineligible(tmp_path):
    from baton.git import head

    git(tmp_path, "init")
    (tmp_path / "a").write_text("a")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    base = head(tmp_path)
    (tmp_path / "a").write_text("b")
    git(tmp_path, "commit", "-am", "step")
    commit = head(tmp_path)
    text = PLAN.format(s1=f"[x] – {commit}", verify="true").replace("abc1234", base)
    text = text.replace("### 2. [ ]", "### 2. [-]")
    report = milestone_report(tmp_path, text)
    assert "skipped" in report.body
    assert report.blockers and "unknown" in report.body


@pytest.mark.parametrize(
    "critical,stale,extra",
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
def test_review_eligibility(tmp_path, critical, stale, extra):
    from baton.config import AgentConfig
    from baton.git import head
    from baton.review import Finding, ReviewResult, candidate_tree, evidence_entry
    from baton.verify import run_verify

    git(tmp_path, "init")
    (tmp_path / "a").write_text("base")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    base = head(tmp_path, short=False)
    (tmp_path / "a").write_text("candidate")
    review = ReviewResult(
        AgentConfig(name="reviewer", adapter="claude", command="claude"),
        (Finding("critical", "problem"),) if critical else (),
    )
    evidence = evidence_entry(
        1, candidate_tree(tmp_path), base, run_verify("true", tmp_path, 10), review
    )
    if stale:
        (tmp_path / "a").write_text("changed")
    git(tmp_path, "commit", "-am", "step")
    commit = head(tmp_path)
    if extra:
        (tmp_path / "a").write_text("extra")
        git(tmp_path, "commit", "-am", "extra")
    text = PLAN.format(s1=f"[x] – {commit}", verify="true").replace("abc1234", base)
    text = text.replace("### 2. [ ]", "### 2. [-]") + "- " + evidence + "\n"
    report = milestone_report(tmp_path, text)
    assert bool(report.blockers) == (critical or stale or extra)


def _finished(tmp_path, log_lines=()):
    from baton.git import head

    git(tmp_path, "init")
    (tmp_path / "a").write_text("a")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    base = head(tmp_path, short=False)
    (tmp_path / "a").write_text("b")
    git(tmp_path, "commit", "-am", "step")
    commit = head(tmp_path)
    text = PLAN.format(s1=f"[x] – {commit}", verify="true").replace("abc1234", base)
    text = text.replace("### 2. [ ]", "### 2. [-]")
    return text + "".join(f"- {line}\n" for line in log_lines)


def test_report_lists_plan_repairs(tmp_path):
    entry = (
        "plan repaired: step 2: needs a base | old: feat: second | new: chore: base; feat: second"
    )

    report = milestone_report(tmp_path, _finished(tmp_path, [entry]))

    assert "## Plan repairs" in report.body
    assert "- Step 2: needs a base" in report.body
    assert "  - was: feat: second" in report.body
    assert "  - now: chore: base; feat: second" in report.body
    assert report.body.index("## Plan repairs") < report.body.index("## Automatic merge")


def test_report_without_repairs_has_no_repair_section(tmp_path):
    report = milestone_report(tmp_path, _finished(tmp_path, ["2026-11-19: step 1 note"]))

    assert "Plan repairs" not in report.body
