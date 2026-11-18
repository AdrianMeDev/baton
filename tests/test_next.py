import io
import subprocess
from pathlib import Path

import pytest

from baton.adapter import AgentResult
from baton.git import head
from baton.next import EXIT_FAILED, EXIT_OK, EXIT_STOPPED, run_next
from baton.plan import parse_plan

PLAN = """# Plan: Demo
Created: 2026-11-18 · Branch: main · Base: abc1234
Milestone: none

## Steps

### 1. {s1} feat: first
- **Why:** w
- **Verify:** `{verify}`

### 2. [ ] feat: second
- **Why:** w
- **Verify:** `true`

## Log
- 2026-11-18: created
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


class FakeAdapter:
    def __init__(self, result: AgentResult | None = None, action=None) -> None:
        self.result = result or AgentResult(ok=True, text="done", cost=0.01)
        self.action = action
        self.prompts: list[str] = []

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        self.prompts.append(prompt)
        if self.action:
            self.action(repo)
        return self.result


def edit(repo: Path) -> None:
    (repo / "new.txt").write_text("hello\n")


def write_plan(repo: Path, s1: str = "[ ]", verify: str = "true") -> None:
    (repo / ".agent-plan.md").write_text(PLAN.format(s1=s1, verify=verify), encoding="utf-8")


def plan_of(repo: Path):
    return parse_plan((repo / ".agent-plan.md").read_text(encoding="utf-8"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    write_plan(tmp_path)
    return tmp_path


def run(repo: Path, adapter: FakeAdapter, **kw) -> tuple[int, str]:
    out = io.StringIO()
    code = run_next(repo, adapter, out, today="2026-11-19", **kw)
    return code, out.getvalue()


def test_success_marks_pending_and_keeps_head(repo: Path) -> None:
    before = head(repo)
    adapter = FakeAdapter(action=edit)
    code, out = run(repo, adapter)
    assert code == EXIT_OK
    step = plan_of(repo).steps[0]
    assert step.status == "pending" and step.pending_on == before
    assert head(repo) == before
    assert "Step 1/2: feat: first" in out
    assert "new.txt" in out
    assert 'git commit -m "feat: first"' in out
    assert "feat: second" in out
    assert "step 1 implemented" in plan_of(repo).log[-1]
    assert "feat: first" in adapter.prompts[0] and "feat: second" not in adapter.prompts[0]


def test_verify_fails_keeps_step_open(repo: Path) -> None:
    write_plan(repo, verify="echo broken; exit 3")
    code, out = run(repo, FakeAdapter(action=edit))
    assert code == EXIT_FAILED
    assert plan_of(repo).steps[0].status == "open"
    assert "broken" in out
    assert "verify failed (exit 3)" in plan_of(repo).log[-1]
    assert (repo / "new.txt").exists()


def test_agent_fails(repo: Path) -> None:
    adapter = FakeAdapter(AgentResult(ok=False, error="boom"))
    code, out = run(repo, adapter)
    assert code == EXIT_FAILED
    assert plan_of(repo).steps[0].status == "open"
    assert "boom" in out and "agent failed (boom)" in plan_of(repo).log[-1]


def test_agent_commits(repo: Path) -> None:
    def commit(r: Path) -> None:
        edit(r)
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "sneaky")

    before = head(repo)
    code, out = run(repo, FakeAdapter(action=commit))
    assert code == EXIT_FAILED
    assert plan_of(repo).steps[0].status == "open"
    assert "HEAD moved" in out and before in plan_of(repo).log[-1]


def test_no_verify_command_is_reported(repo: Path) -> None:
    write_plan(repo, verify="")
    code, out = run(repo, FakeAdapter(action=edit))
    assert code == EXIT_OK
    assert "not verified" in out
    assert plan_of(repo).steps[0].status == "pending"


def test_gate_dirty_blocks(repo: Path) -> None:
    write_plan(repo, s1=f"[~] (on {head(repo)})")
    edit(repo)
    adapter = FakeAdapter()
    code, out = run(repo, adapter)
    assert code == EXIT_STOPPED
    assert adapter.prompts == []
    assert "new.txt" in out and 'git commit -m "feat: first"' in out


def test_gate_committed_continues_with_next_step(repo: Path) -> None:
    write_plan(repo, s1=f"[~] (on {head(repo)})")
    (repo / "a.txt").write_text("changed\n")
    git(repo, "commit", "-q", "-am", "feat: first")
    done = head(repo)
    code, out = run(repo, FakeAdapter(action=edit))
    assert code == EXIT_OK
    plan = plan_of(repo)
    assert plan.steps[0].status == "done" and plan.steps[0].commit == done
    assert plan.steps[1].status == "pending" and plan.steps[1].pending_on == done
    assert "Step 2/2: feat: second" in out


def test_gate_discarded_stops(repo: Path) -> None:
    write_plan(repo, s1=f"[~] (on {head(repo)})")
    adapter = FakeAdapter()
    code, out = run(repo, adapter)
    assert code == EXIT_STOPPED
    assert adapter.prompts == []
    assert "discarded" in out
    assert plan_of(repo).steps[0].status == "pending"


def test_dirty_tree_without_pending_step_refuses(repo: Path) -> None:
    edit(repo)
    adapter = FakeAdapter()
    code, out = run(repo, adapter)
    assert code == EXIT_STOPPED
    assert adapter.prompts == []
    assert "new.txt" in out


def test_no_open_step(repo: Path) -> None:
    text = (repo / ".agent-plan.md").read_text(encoding="utf-8")
    text = text.replace("[ ] feat: second", "[-] feat: second").replace(
        "[ ] feat: first", "[x] – abc1234 feat: first"
    )
    (repo / ".agent-plan.md").write_text(text, encoding="utf-8")
    code, out = run(repo, FakeAdapter())
    assert code == EXIT_STOPPED
    assert "No open steps" in out


def test_missing_plan(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    code, out = run(tmp_path, FakeAdapter())
    assert code == EXIT_STOPPED
    assert ".agent-plan.md" in out
