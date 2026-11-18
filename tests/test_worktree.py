import subprocess
from pathlib import Path

import pytest

from baton.git import GitError, worktree_add, worktree_path_for, worktree_paths
from baton.plan import parse_plan
from baton.worktree import ensure_worktree, mirror_plan

PLAN = """# Plan: Demo
Created: 2026-11-18 · Branch: main · Base: abc1234
Milestone: M7 – Auto-Commit (docs/ROADMAP.md, local)

## Steps

### 1. [ ] feat: first
- **Why:** w

## Log
- 2026-11-18: created
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def state_home(monkeypatch, tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("state")
    monkeypatch.setenv("XDG_STATE_HOME", str(path))
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / ".agent-plan.md").write_text(PLAN, encoding="utf-8")
    return tmp_path


def test_worktree_path_is_under_xdg_state(repo: Path, state_home: Path) -> None:
    path = worktree_path_for(repo, "baton/m7-auto-commit")
    assert path.parent.parent == state_home / "baton" / "worktrees"
    assert path.parent.name.startswith(f"{repo.name}-")
    assert path.name == "baton-m7-auto-commit"
    assert path == worktree_path_for(repo, "baton/m7-auto-commit")
    assert path != worktree_path_for(repo, "baton/other")


def test_ensure_creates_branch_worktree_and_copies_plan(repo: Path) -> None:
    plan = parse_plan((repo / ".agent-plan.md").read_text(encoding="utf-8"))
    path, created = ensure_worktree(repo, plan)
    assert created and path == worktree_path_for(repo, "baton/m7-auto-commit")
    assert (path / ".agent-plan.md").read_text(encoding="utf-8") == PLAN
    assert path.resolve() in {p.resolve() for p in worktree_paths(repo)}
    assert (path / "a.txt").is_file()


def test_ensure_reuses_and_keeps_worktree_plan(repo: Path) -> None:
    plan = parse_plan(PLAN)
    path, _ = ensure_worktree(repo, plan)
    (path / ".agent-plan.md").write_text(PLAN + "more\n", encoding="utf-8")
    again, created = ensure_worktree(repo, plan)
    assert again == path and not created
    assert (path / ".agent-plan.md").read_text(encoding="utf-8").endswith("more\n")


def test_ensure_uses_existing_branch(repo: Path) -> None:
    git(repo, "branch", "baton/m7-auto-commit")
    path, created = ensure_worktree(repo, parse_plan(PLAN))
    assert created and (path / "a.txt").is_file()


def test_ensure_refuses_branch_checked_out_in_main(repo: Path) -> None:
    git(repo, "switch", "-c", "baton/m7-auto-commit")
    with pytest.raises(GitError):
        ensure_worktree(repo, parse_plan(PLAN))


def test_ensure_refuses_foreign_directory_at_the_path(repo: Path) -> None:
    path = worktree_path_for(repo, "baton/m7-auto-commit")
    path.mkdir(parents=True)
    with pytest.raises(GitError, match="not a worktree"):
        ensure_worktree(repo, parse_plan(PLAN))


def test_mirror_copies_plan_back(repo: Path) -> None:
    path, _ = ensure_worktree(repo, parse_plan(PLAN))
    (path / ".agent-plan.md").write_text("changed\n", encoding="utf-8")
    mirror_plan(path, repo)
    assert (repo / ".agent-plan.md").read_text(encoding="utf-8") == "changed\n"


def test_worktree_add_creates_missing_parents(repo: Path, tmp_path_factory) -> None:
    target = tmp_path_factory.mktemp("wt") / "deep" / "dir"
    worktree_add(repo, target, "feature/x", create=True)
    assert (target / "a.txt").is_file()
