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
[agents.reviewer]
adapter="claude"
command="reviewer"
provider="independent"
[routes.review]
S=["reviewer"]
M=["reviewer"]
L=["reviewer"]
[routes.implement]
M=["first", "first", "second"]
""")
    agents = {
        "first": FakeAdapter(),
        "second": FakeAdapter(action=edit),
        "reviewer": FakeAdapter(AgentResult(ok=True, text='{"findings": []}')),
    }
    monkeypatch.setattr("baton.next.make_adapter", lambda agent: agents[agent.name])
    return config, agents


@pytest.fixture
def routed_executions(monkeypatch):
    from baton.next import _RoutedAdapter

    executions = []

    def capture(*args, **kwargs):
        execution = _RoutedAdapter(*args, **kwargs)
        if not kwargs.get("review"):
            executions.append(execution)
        return execution

    monkeypatch.setattr("baton.next._RoutedAdapter", capture)
    return executions


def invoke_chain(repo, chain, **kw):
    config, _ = chain
    out = io.StringIO()
    code = run_next_command(repo, config_path=str(config), out=out, **kw)
    return code, out.getvalue()


def test_limit_handoff_preserves_partial_work_and_verifies_once(repo, chain, routed_executions):
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
    execution = routed_executions[0]
    assert execution.route.tier == "M"
    assert [agent.name for agent in execution.attempted_agents] == ["first", "second"]
    assert [agent.adapter for agent in execution.attempted_agents] == ["claude", "codex"]


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


def test_cooldown_skips_agent_and_explicit_pin_does_not_fallback(repo, chain, routed_executions):
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
    assert routed_executions[0].attempted_agents == []
    assert [agent.name for agent in routed_executions[1].attempted_agents] == ["second"]


def test_provider_reset_time_is_persisted(repo, chain):
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore, UsageLimit

    _, agents = chain
    reset = datetime.now(UTC) + timedelta(hours=3)
    agents["first"] = FakeAdapter(AgentResult(ok=False, error="quota", limit=UsageLimit(reset)))
    assert invoke_chain(repo, chain)[0] == EXIT_OK
    assert LimitStore().read()["first"] == reset


def test_dry_run_selects_after_gate_and_skips_cooldown_without_writes(
    repo, chain, routed_executions
):
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
    assert routed_executions[0].attempted_agents == []


def test_cli_limit_recovery(repo, chain, monkeypatch, capsys, routed_executions):
    import json
    import sys

    from baton.adapter import make_adapter
    from baton.cli import main
    from baton.limits import LimitStore

    config, _ = chain
    first = config.parent / "first"
    second = config.parent / "second"
    reviewer = config.parent / "reviewer"
    reviewer.write_text(
        f"#!{sys.executable}\nimport json\n"
        "print(json.dumps({'is_error': False, 'result': '{\"findings\": []}'}))\n"
    )
    reviewer.chmod(0o755)
    first.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\nfrom pathlib import Path\n"
        "if '--version' in sys.argv:\n    print('fake 1.0'); sys.exit(0)\n"
        "if 'plan' in sys.argv:\n"
        "    print(json.dumps({'is_error': False, 'result': 'OK'})); sys.exit(0)\n"
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
        .replace('command="reviewer"', f"command={json.dumps(str(reviewer))}")
    )
    monkeypatch.setattr("baton.next.make_adapter", make_adapter)
    before = head(repo)
    args = ["--config", str(config)]
    assert main([*args, "next", "--path", str(repo)]) == EXIT_OK
    assert "1h fallback" in capsys.readouterr().out
    assert (repo / "a.txt").read_text() == "finished implementation\n"
    assert head(repo) == before
    assert plan_of(repo).steps[0].status == "pending"
    assert "first" in LimitStore().active()
    execution = routed_executions[0]
    assert execution.route.tier == "M"
    assert [agent.name for agent in execution.attempted_agents] == ["first", "second"]
    assert [agent.adapter for agent in execution.attempted_agents] == ["claude", "codex"]
    assert main([*args, "status", "--path", str(repo)]) == EXIT_OK
    assert "first: cooldown until" in capsys.readouterr().out
    assert main([*args, "recheck", "first"]) == EXIT_OK
    assert "probe succeeded" in capsys.readouterr().out
    assert "first" not in LimitStore().active()
    assert main([*args, "status", "--path", str(repo)]) == EXIT_OK
    assert "first: available" in capsys.readouterr().out
    assert (repo / "notes.txt").read_text() == "partial notes\n"
    assert head(repo) == before


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


@pytest.mark.parametrize(
    ("size", "tier", "agent_name", "expected_tier", "expected_agent"),
    [
        (None, None, None, "M", "first"),
        ("S", None, None, "S", "first"),
        ("S", "L", None, "L", "first"),
        ("L", None, "second", "L", "second"),
        ("L", "S", "second", "S", "second"),
    ],
)
def test_execution_retains_effective_route(
    repo, chain, routed_executions, size, tier, agent_name, expected_tier, expected_agent
):
    config, _ = chain
    with config.open("a") as stream:
        stream.write('S=["first", "second"]\nL=["first", "second"]\n')
    if size:
        plan = repo / ".agent-plan.md"
        plan.write_text(plan.read_text().replace("- **Why:** w", f"- **Size:** {size}"))
    assert invoke_chain(repo, chain, tier=tier, agent_name=agent_name)[0] == EXIT_OK
    execution = routed_executions[0]
    assert execution.route.tier == expected_tier
    assert [agent.name for agent in execution.attempted_agents] == [expected_agent]
    if agent_name:
        assert [agent.name for agent in execution.route.agents] == [agent_name]


@pytest.fixture
def repair_chain(repo, chain):
    config, agents = chain
    with config.open("a") as stream:
        stream.write('\n[routes.fix]\nM=["second"]\nL=["second"]\n')
    write_plan(repo, verify="echo repair-needed; test -s new.txt")
    return config, agents


def test_failed_verify_is_repaired_in_same_tree(repo, repair_chain, routed_executions):
    _, agents = repair_chain
    agents["first"].action = lambda r: (r / "partial.txt").write_text("keep me")
    before = head(repo)
    code, out = invoke_chain(repo, repair_chain, agent_name="first", tier="L")
    assert code == EXIT_OK
    assert "Attempting same-tier repair" in out
    prompt = agents["second"].prompts[0]
    assert "repair-needed" in prompt and "Outcome: exit 1" in prompt
    assert "feat: first" in prompt and "feat: second" not in prompt
    assert (repo / "partial.txt").read_text() == "keep me"
    assert (repo / "new.txt").exists()
    assert plan_of(repo).steps[0].status == "pending"
    assert head(repo) == before
    assert [e.route.tier for e in routed_executions] == ["L", "L"]
    assert [a.name for a in routed_executions[0].attempted_agents] == ["first", "second"]
    assert "Agent cost: $0.0200" in out


@pytest.mark.parametrize("mode", ["failed", "committed", "ineffective", "cooldown", "quota"])
def test_unsuccessful_fix_leaves_step_open(repo, repair_chain, mode):
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore, UsageLimit

    _, agents = repair_chain
    agents["first"].action = lambda r: (r / "partial.txt").write_text("keep me")
    if mode == "failed":
        agents["second"] = FakeAdapter(AgentResult(ok=False, error="fix failed"))
    elif mode == "committed":

        def commit(r):
            edit(r)
            git(r, "add", "-A")
            git(r, "commit", "-m", "unwanted fix commit")

        agents["second"].action = commit
    elif mode == "ineffective":
        agents["second"].action = None
    elif mode == "cooldown":
        LimitStore().set("second", datetime.now(UTC) + timedelta(hours=2))
    else:
        agents["second"] = FakeAdapter(AgentResult(ok=False, error="quota", limit=UsageLimit()))
    code, out = invoke_chain(repo, repair_chain)
    assert code == EXIT_FAILED
    assert plan_of(repo).steps[0].status == "open"
    assert (repo / "partial.txt").read_text() == "keep me"
    if mode == "committed":
        assert "HEAD moved" in out
    elif mode == "ineffective":
        assert "Verify failed" in out and "repair-needed" in out
    else:
        assert "fix agent failed" in out


@pytest.mark.parametrize("verify", ["true", ""])
def test_passing_or_absent_verify_does_not_resolve_fix(repo, chain, verify):
    # No fix route exists: resolution must remain lazy.
    write_plan(repo, verify=verify)
    assert invoke_chain(repo, chain)[0] == EXIT_OK
    assert not chain[1]["second"].prompts


def test_failed_verify_diagnoses_missing_fix_route(repo, chain):
    write_plan(repo, verify="false")
    code, out = invoke_chain(repo, chain)
    assert code == EXIT_FAILED and "missing route" in out and "[routes.fix] M" in out
    assert plan_of(repo).steps[0].status == "open"


def test_legacy_config_does_not_require_fix_route(repo, chain):
    config, agents = chain
    config.write_text('[agents.first]\nadapter="claude"\ncommand="first"\n')
    write_plan(repo, verify="false")
    code, out = invoke_chain(repo, chain)
    assert code == EXIT_FAILED and "Verify failed" in out
    assert len(agents["first"].prompts) == 1
    assert "missing route" not in out


def test_fix_quota_handoff_keeps_all_participants(repo, repair_chain, routed_executions):
    from baton.limits import UsageLimit

    config, agents = repair_chain
    config.write_text(
        config.read_text()
        .replace(
            "[routes.fix]",
            '[agents.third]\nadapter="pi"\ncommand="third"\nprovider="google"\n[routes.fix]',
        )
        .replace('M=["second"]', 'M=["second", "third"]')
    )
    agents["second"] = FakeAdapter(AgentResult(ok=False, error="quota", limit=UsageLimit()))
    agents["third"] = FakeAdapter(action=edit)
    assert invoke_chain(repo, repair_chain)[0] == EXIT_OK
    assert [a.name for a in routed_executions[0].attempted_agents] == ["first", "second", "third"]
    assert "Handoff" in agents["third"].prompts[0]
    assert "repair-needed" in agents["third"].prompts[0]


def test_timed_out_verify_repairs_and_reruns_original_command(repo, repair_chain, monkeypatch):
    from baton.verify import VerifyResult, run_verify

    calls = []

    def verify(command, cwd, timeout):
        calls.append((command, cwd, timeout))
        if len(calls) == 1:
            return VerifyResult(True, False, None, True, "test hung", command)
        return run_verify(command, cwd, timeout)

    monkeypatch.setattr("baton.next.run_verify", verify)
    assert invoke_chain(repo, repair_chain, verify_timeout=3)[0] == EXIT_OK
    assert len(calls) == 2 and calls[0] == calls[1]
    assert "Outcome: timed out" in repair_chain[1]["second"].prompts[0]
    assert "test hung" in repair_chain[1]["second"].prompts[0]


@pytest.mark.parametrize("tier", ["S", "M", "L"])
@pytest.mark.parametrize("succeeds", [False, True])
def test_verification_escalates_once_then_stops(
    repo, repair_chain, routed_executions, monkeypatch, tier, succeeds
):
    config, agents = repair_chain
    config.write_text(
        config.read_text()
        .replace(
            "[routes.implement]",
            '[agents.strong]\nadapter="pi"\ncommand="strong"\nprovider="google"\n[routes.implement]',
        )
        .replace('L=["second"]', 'L=["strong"]\nS=["second"]')
    )
    events = []
    agents["first"].action = lambda r: events.append("implement")
    agents["second"].action = lambda r: events.append("same-tier fix")

    def strong(r):
        events.append("L fix")
        if succeeds and (tier != "L" or len(agents["strong"].prompts) == 2):
            edit(r)

    agents["strong"] = FakeAdapter(action=strong)
    from baton.verify import run_verify

    def verify(command, cwd, timeout):
        events.append("verify")
        return run_verify(command, cwd, timeout)

    monkeypatch.setattr("baton.next.run_verify", verify)
    # Changing diagnostic output makes it observable which failure the fixer receives.
    write_plan(
        repo,
        verify=(
            "echo failure-$(test -f checked && echo second || echo first); "
            "touch checked; test -s new.txt"
        ),
    )
    before = head(repo)
    code, out = invoke_chain(repo, repair_chain, agent_name="first", tier=tier)
    assert code == (EXIT_OK if succeeds else EXIT_FAILED)
    assert events == [
        "implement",
        "verify",
        "L fix" if tier == "L" else "same-tier fix",
        "verify",
        "L fix",
        "verify",
    ]
    assert [e.route.tier for e in routed_executions] == [tier, tier, "L"]
    assert "failure-second" in agents["strong"].prompts[-1]
    if tier == "L":
        assert len(agents["strong"].prompts) == 2
    else:
        assert "failure-first" in agents["second"].prompts[0]
    assert out.index("Attempting same-tier repair") < out.index("Escalating repair to tier L")
    assert head(repo) == before
    assert (repo / "checked").exists()
    assert plan_of(repo).steps[0].status == ("pending" if succeeds else "open")
    assert "after 2 repair attempts" in plan_of(repo).log[-1]
    if not succeeds:
        assert "failure-second" in out and "step stays open" in out


@pytest.mark.parametrize("mode", ["missing", "cooldown", "quota", "failed", "committed"])
def test_escalation_route_failure_stops_with_partial_work(repo, repair_chain, mode):
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore, UsageLimit

    config, agents = repair_chain
    config.write_text(
        config.read_text()
        .replace(
            "[routes.fix]",
            '[agents.strong]\nadapter="pi"\ncommand="strong"\nprovider="google"\n[routes.fix]',
        )
        .replace('L=["second"]', "" if mode == "missing" else 'L=["strong"]')
    )
    agents["second"].action = lambda r: (r / "partial-fix").write_text("preserved")
    agents["strong"] = FakeAdapter(AgentResult(ok=False, error="broken"))
    if mode == "cooldown":
        LimitStore().set("strong", datetime.now(UTC) + timedelta(hours=2))
    elif mode == "quota":
        agents["strong"].result = AgentResult(ok=False, error="quota", limit=UsageLimit())
    elif mode == "committed":

        def commit(r):
            git(r, "add", "-A")
            git(r, "commit", "-m", "unexpected L commit")

        agents["strong"].action = commit
    code, out = invoke_chain(repo, repair_chain)
    assert code == EXIT_FAILED
    assert plan_of(repo).steps[0].status == "open"
    assert (repo / "partial-fix").read_text() == "preserved"
    assert len(agents["second"].prompts) == 1
    if mode == "missing":
        assert "[routes.fix] L" in out
    elif mode == "committed":
        assert "HEAD moved" in out
    else:
        assert "fix agent failed" in out


def test_escalation_handoff_keeps_retry_budget_and_participants(
    repo, repair_chain, routed_executions
):
    from baton.limits import UsageLimit

    config, agents = repair_chain
    config.write_text(
        config.read_text()
        .replace(
            "[routes.fix]",
            '[agents.limited]\nadapter="claude"\ncommand="limited"\n'
            '[agents.final]\nadapter="pi"\ncommand="final"\nprovider="google"\n[routes.fix]',
        )
        .replace('L=["second"]', 'L=["limited", "final"]')
    )
    agents["second"].action = None
    agents["limited"] = FakeAdapter(
        AgentResult(ok=False, error="quota", limit=UsageLimit()),
        lambda r: (r / "partial-L").write_text("preserve L work"),
    )

    def finish(r):
        assert (r / "partial-L").read_text() == "preserve L work"
        edit(r)

    agents["final"] = FakeAdapter(action=finish)
    write_plan(repo, verify="echo checked >> checks; echo latest-failure; test -s new.txt")
    code, out = invoke_chain(repo, repair_chain)
    assert code == EXIT_OK
    assert (repo / "checks").read_text().splitlines() == ["checked"] * 3
    assert [a.name for a in routed_executions[0].attempted_agents] == [
        "first",
        "second",
        "limited",
        "final",
    ]
    prompt = agents["final"].prompts[0]
    assert "Handoff" in prompt and "latest-failure" in prompt and "partial-L" in prompt
    assert out.count("Escalating repair to tier L") == 1
    assert plan_of(repo).steps[0].status == "pending"


@pytest.mark.parametrize("verify", ["true", ""])
@pytest.mark.parametrize("critical", [False, True])
def test_cli_reports_independent_review_before_commit(repo, chain, capsys, verify, critical):
    import json

    from baton.cli import main

    config, agents = chain
    write_plan(repo, verify=verify)
    agents["first"].action = edit
    findings = (
        [
            {"severity": "informational", "message": "context"},
            {"severity": "warning", "message": "missing edge test"},
            {"severity": "critical", "message": "data loss", "path": "new.txt", "line": 1},
        ]
        if critical
        else []
    )
    agents["reviewer"].result = AgentResult(ok=True, text=json.dumps({"findings": findings}))
    locations = []

    def inspect(cwd):
        locations.append(cwd)
        assert cwd != repo and not (cwd / ".git").exists()
        assert not (cwd / "new.txt").exists()
        (cwd / "reviewer-scratch").write_text("disposable")

    agents["reviewer"].action = inspect
    before = head(repo)
    assert main(["--config", str(config), "next", "--path", str(repo)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Independent reviewer: reviewer (claude)" in out
    assert out.index("Checks:") < out.index("Independent reviewer:") < out.index("Commit:")
    if critical:
        assert out.index("CRITICAL:") < out.index("WARNING:") < out.index("INFORMATIONAL:")
        assert "new.txt:1: data loss" in out
    else:
        assert "No findings." in out
    if not verify:
        assert "not verified" in out
    assert "review reviewer: critical=" in plan_of(repo).log[-1]
    assert plan_of(repo).steps[0].status == "pending"
    assert head(repo) == before
    assert not locations[0].exists()
    assert not (repo / "reviewer-scratch").exists()
    assert "hello" in agents["reviewer"].prompts[0]


@pytest.mark.parametrize(
    "mode",
    [
        "malformed",
        "failed",
        "quota",
        "cooldown",
        "missing",
        "same-provider",
        "unknown-author",
        "stale",
    ],
)
def test_review_failure_leaves_candidate_open(repo, chain, mode):
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore, UsageLimit

    config, agents = chain
    agents["first"].action = edit
    if mode == "malformed":
        agents["reviewer"].result = AgentResult(ok=True, text="looks good")
    elif mode == "failed":
        agents["reviewer"].result = AgentResult(ok=False, error="review crashed")
    elif mode == "quota":
        agents["reviewer"].result = AgentResult(ok=False, error="quota", limit=UsageLimit())
    elif mode == "cooldown":
        LimitStore().set("reviewer", datetime.now(UTC) + timedelta(hours=1))
    elif mode == "missing":
        config.write_text(config.read_text().replace("[routes.review]", "[routes.fix]"))
    elif mode == "same-provider":
        config.write_text(
            config.read_text().replace('provider="independent"', 'provider="anthropic"')
        )
    elif mode == "unknown-author":
        config.write_text(
            config.read_text().replace(
                'adapter="claude"\ncommand="first"', 'adapter="pi"\ncommand="first"'
            )
        )
    else:
        agents["reviewer"].action = lambda cwd: (repo / "new.txt").write_text(
            "changed during review"
        )
    before = head(repo)
    code, out = invoke_chain(repo, chain)
    assert code == EXIT_FAILED
    assert "Independent review failed" in out and "step stays open" in out
    assert "Commit:" not in out
    assert "independent review failed" in plan_of(repo).log[-1]
    assert plan_of(repo).steps[0].status == "open"
    assert (repo / "new.txt").exists() and head(repo) == before
    if mode == "quota":
        assert "reviewer" in LimitStore().active()


def test_reviewer_quota_uses_next_independent_reviewer_in_fresh_directory(repo, chain):
    from baton.limits import LimitStore, UsageLimit

    config, agents = chain
    config.write_text(
        config.read_text()
        .replace(
            "[routes.review]",
            '[agents.backup]\nadapter="pi"\ncommand="backup"\nprovider="another"\n[routes.review]',
        )
        .replace('M=["reviewer"]', 'M=["reviewer", "reviewer", "backup"]')
    )
    agents["first"].action = edit
    locations = []
    agents["reviewer"].result = AgentResult(ok=False, error="quota", limit=UsageLimit())
    agents["reviewer"].action = lambda cwd: locations.append(cwd)
    agents["backup"] = FakeAdapter(AgentResult(ok=True, text='{"findings": []}'))
    agents["backup"].action = lambda cwd: locations.append(cwd)
    code, out = invoke_chain(repo, chain)
    assert code == EXIT_OK
    assert "Independent reviewer: backup" in out
    assert len(agents["reviewer"].prompts) == len(agents["backup"].prompts) == 1
    assert locations[0] != locations[1] and all(not p.exists() for p in locations)
    assert agents["reviewer"].prompts == agents["backup"].prompts
    assert "reviewer" in LimitStore().active()


def test_review_excludes_partial_implementer_and_fixer_providers(repo, repair_chain):
    from baton.limits import UsageLimit

    config, agents = repair_chain
    config.write_text(
        config.read_text()
        .replace('M=["first", "first", "second"]', 'M=["first", "second"]')
        .replace('M=["reviewer"]', 'M=["first", "second", "reviewer"]')
    )
    agents["first"] = FakeAdapter(AgentResult(ok=False, error="quota", limit=UsageLimit()))
    agents["second"].action = None

    # The second author implements and then repairs; both providers remain excluded.
    def repair_after_implementation(cwd):
        if len(agents["second"].prompts) == 2:
            edit(cwd)

    agents["second"].action = repair_after_implementation
    assert invoke_chain(repo, repair_chain)[0] == EXIT_OK
    assert len(agents["first"].prompts) == 1
    assert len(agents["second"].prompts) == 2
    assert len(agents["reviewer"].prompts) == 1


def test_dry_run_does_not_start_review_or_require_its_route(repo, chain):
    from baton.adapter import ClaudeAdapter

    config, agents = chain
    config.write_text(config.read_text().replace("[routes.review]", "[routes.fix]"))
    agents["first"] = ClaudeAdapter()
    assert invoke_chain(repo, chain, dry_run=True)[0] == EXIT_OK
    assert not agents["reviewer"].prompts


def test_cli_quality_loop_with_partial_handoff_and_independent_review(
    repo, tmp_path_factory, capsys
):
    import json
    import sys

    from baton.cli import main
    from baton.limits import LimitStore

    support = tmp_path_factory.mktemp("quality-loop")
    executables = support / "executables"
    executables.mkdir()
    events = support / "events"

    def executable(name, action, response):
        path = executables / name
        path.write_text(
            f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
            f'with Path({str(events)!r}).open("a") as log: log.write({name!r} + "\\n")\n'
            "prompt = sys.argv[sys.argv.index('-p') + 1]\n"
            + action
            + "\n"
            + f"print(json.dumps({response!r}))\n"
        )
        path.chmod(0o755)
        return str(path)

    commands = {}
    commands["limited"] = executable(
        "limited",
        "Path('a.txt').write_text('partial implementation\\n')\n"
        "Path('notes.txt').write_text('preserved partial notes\\n')",
        {"is_error": True, "result": "Usage limit reached"},
    )
    commands["implementer"] = executable(
        "implementer",
        "assert 'partial implementation' in prompt and 'notes.txt' in prompt\n"
        "assert Path('notes.txt').read_text() == 'preserved partial notes\\n'\n"
        "Path('a.txt').write_text('implemented but broken\\n')",
        {"is_error": False, "result": "implemented"},
    )
    commands["fixer"] = executable(
        "fixer",
        "assert 'Outcome: exit 1' in prompt and 'quality-failure' in prompt\n"
        "Path('a.txt').write_text('first repair still broken\\n')",
        {"is_error": False, "result": "first fix"},
    )
    commands["strong"] = executable(
        "strong",
        "assert 'Outcome: exit 1' in prompt and 'quality-failure' in prompt\n"
        "assert Path('a.txt').read_text() == 'first repair still broken\\n'\n"
        "Path('a.txt').write_text('correct\\n')\n"
        "Path('new.txt').write_text('new implementation content\\n')",
        {"is_error": False, "result": "L fix"},
    )
    commands["reviewer"] = executable(
        "reviewer",
        f"assert Path.cwd() != Path({str(repo)!r})\n"
        "assert not Path('a.txt').exists()\n"
        "assert 'new implementation content' in prompt and 'preserved partial notes' in prompt\n"
        "assert 'correct' in prompt\nPath('scratch').write_text('temporary reviewer edit')",
        {"is_error": False, "result": '{"findings": []}'},
    )
    providers = {
        "limited": "anthropic",
        "implementer": "openai",
        "fixer": "anthropic",
        "strong": "openai",
        "reviewer": "google",
    }
    config = support / "quality.toml"
    config.write_text(
        "\n".join(
            f'[agents.{name}]\nadapter="claude"\ncommand={json.dumps(command)}\n'
            f"provider={json.dumps(providers[name])}\n"
            for name, command in commands.items()
        )
        + """
[routes.implement]
M=["limited", "implementer"]
[routes.fix]
M=["fixer"]
L=["strong"]
[routes.review]
M=["limited", "fixer", "implementer", "strong", "reviewer"]
"""
    )
    write_plan(repo, verify="echo checked >> checks; echo quality-failure; grep -qx correct a.txt")
    before = head(repo)
    assert main(["--config", str(config), "next", "--path", str(repo)]) == EXIT_OK
    out = capsys.readouterr().out
    assert events.read_text().splitlines() == [
        "limited",
        "implementer",
        "fixer",
        "strong",
        "reviewer",
    ]
    assert (repo / "checks").read_text().splitlines() == ["checked"] * 3
    assert (repo / "notes.txt").read_text() == "preserved partial notes\n"
    assert (repo / "new.txt").read_text() == "new implementation content\n"
    assert not (repo / "scratch").exists()
    assert "limited" in LimitStore().active()
    assert out.index("same-tier repair") < out.index("Escalating repair to tier L")
    assert "Independent reviewer: reviewer" in out and "No findings." in out
    assert plan_of(repo).steps[0].status == "pending"
    assert "after 2 repair attempts; review reviewer: critical=0" in plan_of(repo).log[-1]
    assert head(repo) == before


@pytest.fixture
def git_identity(monkeypatch):
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@example.com")


def _log_subjects(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s"], check=True, capture_output=True, text=True
    )
    return out.stdout.splitlines()


def test_commit_flag_commits_with_step_message(repo, git_identity) -> None:
    before = head(repo)
    code, out = run(repo, FakeAdapter(action=edit), commit=True)
    assert code == EXIT_OK
    assert head(repo) != before
    assert _log_subjects(repo)[:2] == ["feat: first", "init"]
    step = plan_of(repo).steps[0]
    assert step.status == "done" and step.commit == head(repo)
    assert f"Committed: {head(repo)} feat: first" in out
    assert "git commit" not in out
    assert "run `baton next` for step 2" in out
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], check=True, capture_output=True, text=True
    ).stdout.split()
    assert "new.txt" in tracked and ".agent-plan.md" not in tracked
    assert "committed (" in plan_of(repo).log[-1]


def test_commit_flag_without_verify_command(repo, git_identity) -> None:
    write_plan(repo, verify="")
    code, out = run(repo, FakeAdapter(action=edit), commit=True)
    assert code == EXIT_OK and "not verified" in out
    assert plan_of(repo).steps[0].status == "done"


def test_without_commit_flag_behaviour_is_unchanged(repo, git_identity) -> None:
    before = head(repo)
    code, out = run(repo, FakeAdapter(action=edit))
    assert code == EXIT_OK and head(repo) == before
    assert plan_of(repo).steps[0].status == "pending"
    assert "git commit -m" in out


def test_failed_verify_does_not_commit(repo, git_identity) -> None:
    write_plan(repo, verify="false")
    before = head(repo)
    code, _ = run(repo, FakeAdapter(action=edit), commit=True)
    assert code == EXIT_FAILED and head(repo) == before
    assert plan_of(repo).steps[0].status == "open"


def test_critical_finding_blocks_auto_commit(repo, chain, git_identity, capsys) -> None:
    import json

    from baton.cli import main

    config, agents = chain
    agents["first"].action = edit
    findings = [{"severity": "critical", "message": "data loss"}]
    agents["reviewer"].result = AgentResult(ok=True, text=json.dumps({"findings": findings}))
    before = head(repo)
    argv = ["--config", str(config), "next", "--path", str(repo), "--commit"]
    assert main(argv) == EXIT_FAILED
    out = capsys.readouterr().out
    assert "blocked by critical review findings" in out
    assert head(repo) == before
    assert plan_of(repo).steps[0].status == "pending"
    assert "auto-commit: critical review findings" in plan_of(repo).log[-1]


def test_warning_finding_does_not_block_auto_commit(repo, chain, git_identity, capsys) -> None:
    import json

    from baton.cli import main

    config, agents = chain
    agents["first"].action = edit
    findings = [{"severity": "warning", "message": "edge case"}]
    agents["reviewer"].result = AgentResult(ok=True, text=json.dumps({"findings": findings}))
    argv = ["--config", str(config), "next", "--path", str(repo), "--commit"]
    assert main(argv) == EXIT_OK
    assert "WARNING:" in capsys.readouterr().out
    assert plan_of(repo).steps[0].status == "done"
    assert _log_subjects(repo)[0] == "feat: first"


def test_failing_pre_commit_hook_leaves_step_pending(repo, git_identity) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho nope >&2\nexit 1\n")
    hook.chmod(0o755)
    before = head(repo)
    code, out = run(repo, FakeAdapter(action=edit), commit=True)
    assert code == EXIT_FAILED and head(repo) == before
    assert "Auto-commit failed" in out and "nope" in out
    step = plan_of(repo).steps[0]
    assert step.status == "pending" and step.pending_on == before
    assert "auto-commit: commit failed" in plan_of(repo).log[-1]


def test_commit_flag_second_run_continues_after_auto_commit(repo, git_identity) -> None:
    run(repo, FakeAdapter(action=edit), commit=True)
    code, out = run(
        repo, FakeAdapter(action=lambda r: (r / "b.txt").write_text("b\n")), commit=True
    )
    assert code == EXIT_OK and "Step 2/2" in out
    assert _log_subjects(repo)[:2] == ["feat: second", "feat: first"]
    assert [s.status for s in plan_of(repo).steps] == ["done", "done"]


def _files_of(repo: Path, rev: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", rev],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


def test_roadmap_changes_only_in_first_and_last_commit(repo, git_identity) -> None:
    (repo / ".git" / "info" / "exclude").write_text(".agent-plan.md\n")
    (repo / "docs").mkdir()
    roadmap = repo / "docs" / "ROADMAP.md"
    roadmap.write_text("# Roadmap: Demo\n\n## Milestones\n\n### M1 [ ] First\n- **Goal:** g\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "roadmap")
    plan = (repo / ".agent-plan.md").read_text()
    tracked = "Milestone: M1 – First (docs/ROADMAP.md, tracked)"
    (repo / ".agent-plan.md").write_text(plan.replace("Milestone: none", tracked))

    code, out = run(repo, FakeAdapter(action=edit), commit=True)
    assert code == EXIT_OK, out
    assert "docs/ROADMAP.md" in _files_of(repo, "HEAD")
    assert "### M1 [~] First" in roadmap.read_text()

    code, _ = run(repo, FakeAdapter(action=lambda r: (r / "b.txt").write_text("b\n")), commit=True)
    assert code == EXIT_OK
    assert "docs/ROADMAP.md" in _files_of(repo, "HEAD")
    assert "### M1 [x] First" in roadmap.read_text()
    assert "M1 done (Demo)" in roadmap.read_text()
    dirty = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert dirty == ""


def test_missing_tracked_roadmap_blocks_commit(repo, git_identity) -> None:
    plan = (repo / ".agent-plan.md").read_text()
    tracked = "Milestone: M1 – First (docs/ROADMAP.md, tracked)"
    (repo / ".agent-plan.md").write_text(plan.replace("Milestone: none", tracked))
    before = head(repo)
    code, out = run(repo, FakeAdapter(action=edit), commit=True)
    assert code == EXIT_FAILED and head(repo) == before
    assert "roadmap docs/ROADMAP.md not found" in out
    assert plan_of(repo).steps[0].status == "pending"
