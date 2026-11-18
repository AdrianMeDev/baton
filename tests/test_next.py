import io
import subprocess
from pathlib import Path

import pytest

from baton.adapter import AgentResult
from baton.git import head
from baton.next import EXIT_FAILED, EXIT_OK, EXIT_STOPPED, run_next, run_next_command
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


def test_command_checks_gate_before_resolving_agent(repo: Path) -> None:
    write_plan(repo, s1=f"[~] (on {head(repo)})")
    edit(repo)
    out = io.StringIO()
    code = run_next_command(repo, config_path=str(repo / "missing.toml"), out=out)
    assert code == EXIT_STOPPED
    assert "Step 1 is implemented but not committed" in out.getvalue()


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


@pytest.fixture(autouse=True)
def isolated_limit_state(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("limits-state")))


@pytest.fixture
def chain(repo, tmp_path_factory, monkeypatch):
    config = tmp_path_factory.mktemp("config") / "config.toml"
    config.write_text("""[agents.first]
adapter="claude"
command="first"
[agents.second]
adapter="codex"
command="second"
[routes.implement]
M=["first", "first", "second"]
""")
    agents = {"first": FakeAdapter(), "second": FakeAdapter(action=edit)}
    monkeypatch.setattr("baton.next.make_adapter", lambda agent: agents[agent.name])
    return config, agents


def invoke_chain(repo, chain, **kw):
    config, _ = chain
    out = io.StringIO()
    code = run_next_command(repo, config_path=str(config), out=out, **kw)
    return code, out.getvalue()


def test_limit_handoff_preserves_partial_work_and_verifies_once(repo, chain):
    from baton.limits import LimitStore, UsageLimit

    _, agents = chain
    write_plan(repo, verify="echo checked >> checks.txt; test -s new.txt")

    def partial(r):
        (r / "a.txt").write_text("staged partial\n")
        git(r, "add", "a.txt")
        (r / "a.txt").write_text("unstaged partial\n")
        (r / "scratch.txt").write_text("untracked partial\n")

    agents["first"] = FakeAdapter(AgentResult(ok=False, error="quota", limit=UsageLimit()), partial)
    before = head(repo)
    code, out = invoke_chain(repo, chain)
    assert code == EXIT_OK
    assert len(agents["first"].prompts) == len(agents["second"].prompts) == 1
    prompt = agents["second"].prompts[0]
    for detail in ("staged partial", "unstaged partial", "scratch.txt", "feat: first", "Handoff"):
        assert detail in prompt
    assert "feat: second" not in prompt
    assert (repo / "scratch.txt").read_text() == "untracked partial\n"
    assert (repo / "checks.txt").read_text() == "checked\n"
    assert (repo / "a.txt").read_text() == "unstaged partial\n"
    assert head(repo) == before
    assert plan_of(repo).steps[0].pending_on == before
    assert sum("waiting for commit" in line for line in plan_of(repo).log) == 1
    assert "1h fallback" in out
    assert "first" in LimitStore().active()


@pytest.mark.parametrize("pinned", [False, True])
def test_limit_exhaustion_leaves_edits_and_step_open(repo, chain, pinned):
    from baton.limits import UsageLimit

    _, agents = chain
    for name in agents:
        agents[name] = FakeAdapter(AgentResult(ok=False, error="quota", limit=UsageLimit()), edit)
    code, out = invoke_chain(repo, chain, agent_name="first" if pinned else None)
    assert code == EXIT_FAILED
    assert plan_of(repo).steps[0].status == "open"
    assert (repo / "new.txt").read_text() == "hello\n"
    assert len(agents["first"].prompts) == 1
    assert len(agents["second"].prompts) == (0 if pinned else 1)
    assert "No available agents" in out and "preserve partial edits" in out


@pytest.mark.parametrize("limited", [False, True])
def test_moved_head_stops_before_successor_even_on_failure(repo, chain, limited):
    from baton.limits import LimitStore, UsageLimit

    _, agents = chain

    def commit(r):
        edit(r)
        git(r, "add", "-A")
        git(r, "commit", "-m", "unexpected")

    result = AgentResult(ok=False, error="quota", limit=UsageLimit() if limited else None)
    agents["first"] = FakeAdapter(result, commit)
    code, out = invoke_chain(repo, chain)
    assert code == EXIT_FAILED
    assert "HEAD moved" in out
    assert not agents["second"].prompts
    assert not LimitStore().read()
    assert plan_of(repo).steps[0].status == "open"


def test_ordinary_failure_does_not_handoff(repo, chain):
    _, agents = chain
    agents["first"] = FakeAdapter(AgentResult(ok=False, error="bad model"), edit)
    code, out = invoke_chain(repo, chain)
    assert code == EXIT_FAILED and "bad model" in out
    assert not agents["second"].prompts


def test_cooldown_skips_agent_and_explicit_pin_does_not_fallback(repo, chain):
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore

    _, agents = chain
    LimitStore().set("first", datetime.now(UTC) + timedelta(hours=2))
    code, out = invoke_chain(repo, chain, agent_name="first")
    assert code == EXIT_FAILED and "Skipping first" in out
    assert not agents["first"].prompts and not agents["second"].prompts
    code, out = invoke_chain(repo, chain)
    assert code == EXIT_OK and "Skipping first" in out
    assert not agents["first"].prompts and len(agents["second"].prompts) == 1


def test_provider_reset_time_is_persisted(repo, chain):
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore, UsageLimit

    _, agents = chain
    reset = datetime.now(UTC) + timedelta(hours=3)
    agents["first"] = FakeAdapter(AgentResult(ok=False, error="quota", limit=UsageLimit(reset)))
    assert invoke_chain(repo, chain)[0] == EXIT_OK
    assert LimitStore().read()["first"] == reset


def test_dry_run_selects_after_gate_and_skips_cooldown_without_writes(repo, chain):
    from datetime import UTC, datetime, timedelta

    from baton.adapter import CodexAdapter
    from baton.limits import LimitStore

    _, agents = chain
    agents["second"] = CodexAdapter()
    write_plan(repo, s1=f"[~] (on {head(repo)})")
    (repo / "a.txt").write_text("previous step\n")
    git(repo, "commit", "-am", "previous")
    LimitStore().set("first", datetime.now(UTC) + timedelta(hours=2))
    plan_before = (repo / ".agent-plan.md").read_bytes()
    state_before = LimitStore().path.read_bytes()
    code, out = invoke_chain(repo, chain, dry_run=True)
    assert code == EXIT_OK
    assert "Step 2/2" in out and "agent second (codex)" in out
    assert not agents["first"].prompts
    assert (repo / ".agent-plan.md").read_bytes() == plan_before
    assert LimitStore().path.read_bytes() == state_before
    assert not (repo / "new.txt").exists()


def test_fake_executables_resume_the_same_tree(repo, chain, monkeypatch):
    import json
    import sys

    from baton.adapter import make_adapter
    from baton.limits import LimitStore

    config, _ = chain
    first = config.parent / "first"
    second = config.parent / "second"
    first.write_text(
        f"#!{sys.executable}\n"
        "import json\nfrom pathlib import Path\n"
        "Path('a.txt').write_text('partial implementation\\n')\n"
        "Path('notes.txt').write_text('partial notes\\n')\n"
        "print(json.dumps({'is_error': True, 'result': 'Usage limit reached'}))\n"
    )
    second.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\nfrom pathlib import Path\n"
        "prompt = sys.argv[-1]\n"
        "assert 'partial implementation' in prompt and 'notes.txt' in prompt\n"
        "assert Path('notes.txt').read_text() == 'partial notes\\n'\n"
        "Path('a.txt').write_text('finished implementation\\n')\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': 'done'}}))\n"
        "print(json.dumps({'type': 'turn.completed'}))\n"
    )
    first.chmod(0o755)
    second.chmod(0o755)
    config.write_text(
        config.read_text()
        .replace('command="first"', f"command={json.dumps(str(first))}")
        .replace('command="second"', f"command={json.dumps(str(second))}")
    )
    monkeypatch.setattr("baton.next.make_adapter", make_adapter)
    before = head(repo)
    assert invoke_chain(repo, chain)[0] == EXIT_OK
    assert (repo / "a.txt").read_text() == "finished implementation\n"
    assert head(repo) == before
    assert plan_of(repo).steps[0].status == "pending"
    assert "first" in LimitStore().active()


def test_dry_run_without_state_creates_no_files(repo, chain, monkeypatch, tmp_path_factory):
    from baton.adapter import ClaudeAdapter
    from baton.limits import LimitStore

    state = tmp_path_factory.mktemp("empty-state") / "not-created"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    _, agents = chain
    agents["first"] = ClaudeAdapter()
    before = (repo / ".agent-plan.md").read_bytes()
    assert invoke_chain(repo, chain, dry_run=True)[0] == EXIT_OK
    assert not state.exists()
    assert not LimitStore().read()
    assert (repo / ".agent-plan.md").read_bytes() == before


def test_malformed_state_stops_before_agent(repo, chain):
    from baton.limits import LimitStore

    store = LimitStore()
    store.path.parent.mkdir(parents=True)
    store.path.write_text("broken")
    _, agents = chain
    code, out = invoke_chain(repo, chain)
    assert code == EXIT_FAILED and "Repair or move" in out
    assert not agents["first"].prompts and not agents["second"].prompts
    assert store.path.read_text() == "broken"
