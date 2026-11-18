import io
import subprocess
from pathlib import Path

import pytest

from baton.status import run_status

PLAN = """# Plan: Demo
Created: 2026-11-18 · Branch: main · Base: abc1234
Milestone: none

## Steps

### 1. [x] – aaaaaaa feat: first
- **Why:** w

### 2. {marker} feat: second
- **Why:** w
- **Size:** S

### 3. [ ] feat: third
- **Why:** w

## Log
- 2026-11-18: step 1 committed (aaaaaaa)
"""

DONE = PLAN.replace("{marker}", "[x] – bbbbbbb").replace("### 3. [ ]", "### 3. [x] – ccccccc")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def status(root: Path) -> tuple[int, str]:
    buf = io.StringIO()
    code = run_status(root, buf)
    return code, buf.getvalue()


def test_no_plan(tmp_path: Path) -> None:
    code, out = status(tmp_path)
    assert code == 0
    assert "No .agent-plan.md" in out
    assert "/plan-commits" in out


def test_open_plan_shows_next_step(repo: Path) -> None:
    (repo / ".agent-plan.md").write_text(PLAN.replace("{marker}", "[ ]"))
    code, out = status(repo)
    assert code == 0
    assert "[x] 1/3 feat: first" in out
    assert "[ ] 2/3 feat: second (S)" in out
    assert "Next: step 2/3 feat: second" in out
    assert "Gate" not in out


def test_blocked_gate_shows_files_and_commit_hint(repo: Path) -> None:
    head = git(repo, "rev-parse", "--short", "HEAD")
    (repo / ".agent-plan.md").write_text(PLAN.replace("{marker}", f"[~] (on {head})"))
    (repo / "a.txt").write_text("changed\n")
    code, out = status(repo)
    assert code == 0
    assert "Current: step 2/3" in out
    assert "BLOCKED" in out
    assert "  a.txt" in out
    assert 'git commit -m "feat: second"' in out


def test_committed_but_unmarked_step(repo: Path) -> None:
    old = git(repo, "rev-parse", "--short", "HEAD")
    (repo / "a.txt").write_text("more\n")
    git(repo, "commit", "-q", "-am", "step")
    (repo / ".agent-plan.md").write_text(PLAN.replace("{marker}", f"[~] (on {old})"))
    code, out = status(repo)
    assert code == 0
    assert "is committed" in out
    assert "not yet marked [x]" in out


def test_discarded_step(repo: Path) -> None:
    head = git(repo, "rev-parse", "--short", "HEAD")
    (repo / ".agent-plan.md").write_text(PLAN.replace("{marker}", f"[~] (on {head})"))
    _, out = status(repo)
    assert "discarded or stashed" in out


def test_finished_plan(repo: Path) -> None:
    (repo / ".agent-plan.md").write_text(DONE)
    code, out = status(repo)
    assert code == 0
    assert "All steps are finished." in out


def test_malformed_plan_exits_1(tmp_path: Path) -> None:
    (tmp_path / ".agent-plan.md").write_text("# Plan: Broken\n\nno steps here\n")
    code, out = status(tmp_path)
    assert code == 1
    assert "Cannot read" in out


def test_pending_outside_repo_reports_gate_error(tmp_path: Path) -> None:
    (tmp_path / ".agent-plan.md").write_text(PLAN.replace("{marker}", "[~] (on abc1234)"))
    code, out = status(tmp_path)
    assert code == 0
    assert "cannot be evaluated" in out


def test_status_does_not_modify_files(repo: Path) -> None:
    path = repo / ".agent-plan.md"
    path.write_text(PLAN.replace("{marker}", "[ ]"))
    before = path.read_bytes()
    status(repo)
    assert path.read_bytes() == before


def test_milestone_status_from_roadmap(repo: Path) -> None:
    text = PLAN.replace("Milestone: none", "Milestone: M2 – Engine (docs/ROADMAP.md, tracked)")
    (repo / ".agent-plan.md").write_text(text.replace("{marker}", "[ ]"))
    (repo / "docs").mkdir()
    (repo / "docs" / "ROADMAP.md").write_text(
        "# Roadmap: X\n\n## Milestones\n\n### M2 [~] Engine\n- **Goal:** g\n\n## Changelog\n"
    )
    _, out = status(repo)
    assert "Milestone: M2 Engine [~]" in out


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state") / "uncreated"))


def test_status_shows_local_cooldowns_and_preserves_state_without_plan(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore

    now = datetime.now(UTC)
    deadline = now + timedelta(hours=2)
    store = LimitStore()
    store.set("cool", deadline)
    store.set("expired", now - timedelta(seconds=1))
    config = tmp_path / "config.toml"
    config.write_text(
        "".join(
            f'[agents.{name}]\nadapter="pi"\ncommand="pi"\n'
            for name in ("cool", "expired", "fresh")
        )
    )
    before = store.path.read_bytes()
    buf = io.StringIO()
    assert run_status(tmp_path, buf, config_path=str(config)) == 0
    out = buf.getvalue()
    assert "No .agent-plan.md" in out
    assert f"cool: cooldown until {deadline.astimezone():%Y-%m-%d %H:%M:%S %Z (%z)}" in out
    assert "expired: available" in out and "fresh: available" in out
    assert store.path.read_bytes() == before


def test_empty_config_and_missing_state_are_read_only(tmp_path):
    from baton.limits import LimitStore

    config = tmp_path / "empty.toml"
    config.write_text("")
    buf = io.StringIO()
    assert run_status(tmp_path, buf, config_path=str(config)) == 0
    assert "none configured" in buf.getvalue()
    assert not LimitStore().path.parent.exists()


def test_status_reports_state_error_but_keeps_plan_output(repo, tmp_path_factory):
    from baton.limits import LimitStore

    (repo / ".agent-plan.md").write_text(PLAN.replace("{marker}", "[ ]"))
    config = tmp_path_factory.mktemp("config") / "config.toml"
    config.write_text('[agents.a]\nadapter="pi"\ncommand="pi"\n')
    store = LimitStore()
    store.path.parent.mkdir(parents=True)
    store.path.write_text("broken")
    buf = io.StringIO()
    assert run_status(repo, buf, config_path=str(config)) == 1
    assert "Next: step 2/3" in buf.getvalue()
    assert "Repair or move" in buf.getvalue()
    assert store.path.read_text() == "broken"
