import json
import os
import subprocess
from pathlib import Path

import pytest

from baton import __version__
from baton.cli import main


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
- **Verify:** `test -f new.txt`

### 2. [ ] feat: second
- **Why:** w
- **Verify:** `true`

## Log
- 2026-11-18: created
"""

CONFIG = '[agents.fake]\nadapter = "claude"\ncommand = "claude"\n'
FIXTURE = Path(__file__).parent / "fixtures" / "claude" / "ok.json"


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
    stdout = json.loads(FIXTURE.read_text(encoding="utf-8"))["stdout"]
    (bin_dir / "ok.json").write_text(stdout, encoding="utf-8")
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/sh\necho hello > new.txt\ncat "{bin_dir}/ok.json"\n', encoding="utf-8")
    fake.chmod(0o755)
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


def test_next_without_claude_agent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[agents.x]\nadapter = "codex"\ncommand = "codex"\n', encoding="utf-8")
    assert main(["--config", str(config), "next", "--path", str(tmp_path)]) == 1
    assert 'no agent with adapter = "claude"' in capsys.readouterr().out
