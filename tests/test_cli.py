import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from baton import __version__
from baton.cli import main
from baton.plan import parse_plan


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"baton {__version__}"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: baton" in capsys.readouterr().out


def test_status_command_without_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--path", str(tmp_path)]) == 0
    assert "No .agent-plan.md" in capsys.readouterr().out


PLAN = """# Plan: Demo
Created: 2026-11-18 · Branch: main · Base: abc1234
Milestone: none

## Steps

### 1. [ ] feat: first
- **Why:** w
- **Size:** S
- **Verify:** `test -s new.txt`

### 2. [ ] feat: second
- **Why:** w
- **Size:** L
- **Verify:** `test -s new.txt`

## Log
- 2026-11-18: created
"""

CONFIG = '[agents.fake]\nadapter = "claude"\ncommand = "claude"\n'
FIXTURES = Path(__file__).parent / "fixtures"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A git repo with a plan, plus a config; a fake ``claude`` script is first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_agent(bin_dir, "claude", "claude")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    (repo / ".agent-plan.md").write_text(PLAN, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    config = tmp_path / "config.toml"
    config.write_text(CONFIG, encoding="utf-8")
    return repo, config


def _fake_agent(bin_dir: Path, name: str, adapter: str, *, delay: bool = False) -> None:
    fixture = FIXTURES / adapter / "ok.json"
    stdout = json.loads(fixture.read_text(encoding="utf-8"))["stdout"]
    payload = bin_dir / f"{name}.jsonl"
    payload.write_text(stdout, encoding="utf-8")
    args = bin_dir / f"{name}.argv"
    script = (
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {shlex.quote(str(args))}\n'
        f"echo {shlex.quote(name)} > new.txt\n"
    )
    if delay:
        script += "sleep 10\n"
    fake = bin_dir / name
    fake.write_text(script + f"cat {shlex.quote(str(payload))}\n", encoding="utf-8")
    fake.chmod(0o755)


def _next(repo: Path, config: Path, *args: str) -> int:
    return main(["--config", str(config), "next", "--path", str(repo), *args])


def test_next_implements_verifies_and_marks_step(
    sample: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, config = sample
    before = _git(repo, "rev-parse", "HEAD")
    assert main(["--config", str(config), "next", "--path", str(repo)]) == 0
    out = capsys.readouterr().out
    assert (repo / "new.txt").is_file()
    assert _git(repo, "rev-parse", "HEAD") == before
    plan = (repo / ".agent-plan.md").read_text(encoding="utf-8")
    assert f"### 1. [~] (on {before[:7]})" in plan
    assert "Step 1/2 done: feat: first" in out
    assert 'git commit -m "feat: first"' in out


def test_next_dry_run_changes_nothing(
    sample: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, config = sample
    plan_before = (repo / ".agent-plan.md").read_text(encoding="utf-8")
    assert main(["--config", str(config), "next", "--path", str(repo), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "--permission-mode" in out and "feat: first" in out
    assert not (repo / "new.txt").exists()
    assert (repo / ".agent-plan.md").read_text(encoding="utf-8") == plan_before
    assert _git(repo, "status", "--porcelain") == ""


def test_next_without_claude_agent(sample, capsys: pytest.CaptureFixture[str]) -> None:
    repo, config = sample
    config.write_text('[agents.x]\nadapter = "codex"\ncommand = "codex"\n', encoding="utf-8")
    assert _next(repo, config) == 1
    assert 'no agent with adapter = "claude"' in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "adapter", "model_flag", "model"),
    [
        ("named-claude", "claude", "--model", "sonnet"),
        ("named-codex", "codex", "-m", "custom-codex"),
        ("named-agy", "agy", "--model", "Gemini 4 Argon"),
        ("local-pi", "pi", "--model", "ollama/qwen2.5-coder"),
        ("api-pi", "pi", "--model", "openrouter/deepseek/deepseek-chat"),
    ],
)
def test_next_agent_override_runs_each_adapter(sample, capsys, name, adapter, model_flag, model):
    repo, config = sample
    bin_dir = config.parent / "bin"
    _fake_agent(bin_dir, name, adapter)
    config.write_text(
        CONFIG
        + f'\n[agents.{name}]\nadapter = "{adapter}"\ncommand = "{name}"\nmodel = "{model}"\n',
        encoding="utf-8",
    )
    before = _git(repo, "rev-parse", "HEAD")
    assert _next(repo, config, "--agent", name) == 0
    out = capsys.readouterr().out
    assert f"Tier S, agent {name} ({adapter})" in out
    assert "test -s new.txt -> passed" in out
    assert (repo / "new.txt").read_text().strip() == name
    argv = (bin_dir / f"{name}.argv").read_text().splitlines()
    assert argv[argv.index(model_flag) + 1] == model
    assert _git(repo, "rev-parse", "HEAD") == before
    assert f"### 1. [~] (on {before[:7]})" in (repo / ".agent-plan.md").read_text()


@pytest.fixture
def routed_sample(sample):
    repo, config = sample
    _fake_agent(config.parent / "bin", "routed-codex", "codex")
    config.write_text(
        CONFIG
        + """
[agents.large]
adapter = "codex"
command = "routed-codex"

[routes.implement]
S = ["fake", "large"]
L = ["large"]
""",
        encoding="utf-8",
    )
    return repo, config


def test_next_tier_override_matches_dry_run(routed_sample, capsys):
    repo, config = routed_sample
    assert _next(repo, config, "--tier", "L", "--dry-run") == 0
    preview = capsys.readouterr().out
    assert "Tier L, agent large (codex)" in preview
    assert "routed-codex exec --json --sandbox workspace-write" in preview
    assert not (config.parent / "bin" / "routed-codex.argv").exists()
    assert _git(repo, "status", "--porcelain") == ""

    assert _next(repo, config, "--tier", "L") == 0
    assert "Tier L, agent large (codex)" in capsys.readouterr().out
    assert (repo / "new.txt").read_text().strip() == "routed-codex"


def test_next_routes_actual_step_after_commit_gate(routed_sample, capsys):
    repo, config = routed_sample
    assert _next(repo, config) == 0
    assert "Tier S, agent fake (claude)" in capsys.readouterr().out
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feat: first")
    before = _git(repo, "rev-parse", "HEAD")

    assert _next(repo, config) == 0
    out = capsys.readouterr().out
    assert "Step 1 marked done" in out
    assert "Tier L, agent large (codex)" in out
    assert (repo / "new.txt").read_text().strip() == "routed-codex"
    assert _git(repo, "rev-parse", "HEAD") == before
    first, second = parse_plan((repo / ".agent-plan.md").read_text()).steps
    assert first.status == "done" and first.commit == before[:7]
    assert second.status == "pending" and second.pending_on == before[:7]


def test_next_timed_out_adapter_leaves_step_open(routed_sample, capsys):
    repo, config = routed_sample
    _fake_agent(config.parent / "bin", "routed-codex", "codex", delay=True)
    assert _next(repo, config, "--agent", "large", "--timeout", "0.1") == 1
    assert "The agent failed: timed out" in capsys.readouterr().out
    assert parse_plan((repo / ".agent-plan.md").read_text()).steps[0].status == "open"


def test_cooldown_persists_for_fresh_cli_invocation(tmp_path, monkeypatch, capsys):
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore

    now = datetime(2026, 11, 18, 12, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("baton.cli.datetime", Clock)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = tmp_path / "config.toml"
    config.write_text(CONFIG + '\n[agents.other]\nadapter="pi"\ncommand="pi"\n')
    assert main(["--config", str(config), "cooldown", "fake", "2h"]) == 0
    assert "2026-11-18T14:00:00+00:00" in capsys.readouterr().out
    assert LimitStore().active(now=now) == {"fake": now + timedelta(hours=2)}
    proc = subprocess.run(
        [os.sys.executable, "-m", "baton", "--config", str(config), "cooldown", "other", "1h"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert set(LimitStore().read()) == {"fake", "other"}


@pytest.mark.parametrize("agent,duration", [("missing", "2h"), ("fake", "0h"), ("fake", "oops")])
def test_invalid_cooldown_creates_no_state(tmp_path, monkeypatch, capsys, agent, duration):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    assert main(["--config", str(config), "cooldown", agent, duration]) == 1
    assert "Cannot set cooldown" in capsys.readouterr().out
    assert not state.exists()


def test_cli_cooldown_malformed_state_is_not_overwritten(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = tmp_path / "baton/limits.json"
    path.parent.mkdir()
    path.write_text("broken")
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    assert main(["--config", str(config), "cooldown", "fake", "2h"]) == 1
    assert "Repair or move" in capsys.readouterr().out
    assert path.read_text() == "broken"


@pytest.fixture(autouse=True)
def isolated_limit_state(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("limits-state")))
