import subprocess
from pathlib import Path

import pytest

from baton.gate import apply_gate, check_gate
from baton.git import GitError, head
from baton.plan import parse_plan

PLAN = """# Plan: Demo
Created: 2026-11-18 · Branch: main · Base: abc1234
Milestone: none

## Steps

### 1. [x] – aaaaaaa feat: first
- **Why:** w

### 2. [~] (on {on}) feat: second
- **Why:** w

### 3. [ ] feat: third
- **Why:** w

## Log
- 2026-11-18: step 1 committed (aaaaaaa)
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def commit_change(repo: Path) -> None:
    (repo / "a.txt").write_text("more\n")
    git(repo, "commit", "-q", "-am", "step")


def test_no_pending_step(repo: Path) -> None:
    plan = parse_plan(PLAN.replace("[~] (on {on})", "[ ]"))
    assert check_gate(plan, repo).state == "no_pending"


def test_blocked_when_dirty(repo: Path) -> None:
    plan = parse_plan(PLAN.format(on=head(repo)))
    (repo / "a.txt").write_text("wip\n")
    result = check_gate(plan, repo)
    assert result.state == "blocked_dirty"
    assert result.step == 2
    assert result.dirty == ("a.txt",)


def test_only_plan_file_changed_counts_as_clean(repo: Path) -> None:
    plan = parse_plan(PLAN.format(on=head(repo)))
    (repo / ".agent-plan.md").write_text("edited\n")
    assert check_gate(plan, repo).state == "discarded"


def test_committed_when_clean_and_head_moved(repo: Path) -> None:
    on = head(repo)
    commit_change(repo)
    result = check_gate(parse_plan(PLAN.format(on=on)), repo)
    assert result.state == "committed"
    assert result.head == head(repo)


def test_discarded_when_clean_and_head_unchanged(repo: Path) -> None:
    result = check_gate(parse_plan(PLAN.format(on=head(repo))), repo)
    assert result.state == "discarded"
    assert result.dirty == ()


def test_override_with_dirty_tree(repo: Path) -> None:
    plan = parse_plan(PLAN.format(on=head(repo)))
    (repo / "a.txt").write_text("wip\n")
    result = check_gate(plan, repo, override=True)
    assert result.state == "overridden"
    assert result.dirty == ("a.txt",)


def test_override_does_not_change_clean_states(repo: Path) -> None:
    plan = parse_plan(PLAN.format(on=head(repo)))
    assert check_gate(plan, repo, override=True).state == "discarded"


def test_non_repo_raises(tmp_path: Path) -> None:
    plan = parse_plan(PLAN.format(on="abc1234"))
    with pytest.raises(GitError, match="not a git repository"):
        check_gate(plan, tmp_path)


def test_apply_committed_marks_done_and_logs(repo: Path) -> None:
    text = PLAN.format(on=head(repo))
    commit_change(repo)
    result = check_gate(parse_plan(text), repo)
    out = apply_gate(text, result, "2026-11-19")
    step = parse_plan(out).steps[1]
    assert (step.status, step.commit) == ("done", head(repo))
    assert out.rstrip().endswith(f"- 2026-11-19: step 2 committed ({head(repo)})")


def test_apply_overridden_logs_override(repo: Path) -> None:
    text = PLAN.format(on=head(repo))
    (repo / "a.txt").write_text("wip\n")
    out = apply_gate(text, check_gate(parse_plan(text), repo, override=True), "2026-11-19")
    assert parse_plan(out).steps[1].status == "done"
    assert "continued without commit" in out


@pytest.mark.parametrize("dirty", [False, True])
def test_apply_leaves_other_states_unchanged(repo: Path, dirty: bool) -> None:
    text = PLAN.format(on=head(repo))
    if dirty:
        (repo / "a.txt").write_text("wip\n")
    result = check_gate(parse_plan(text), repo)
    assert apply_gate(text, result, "2026-11-19") == text
    no_pending = check_gate(parse_plan(text.replace("[~] (on", "[ ] (on")), repo)
    assert apply_gate(text, no_pending, "2026-11-19") == text
