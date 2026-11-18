"""M7 acceptance: a milestone ends as a branch with exactly one green commit per step."""

import io
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from baton.adapter import AgentResult
from baton.git import head
from baton.next import EXIT_OK, run_next

PLAN = """# Plan: Demo feature
Created: 2026-11-18 · Branch: main · Base: abc1234
Milestone: M1 – First milestone (docs/ROADMAP.md, tracked)

## Steps

### 1. [ ] feat: add alpha
- **Why:** w
- **Verify:** `test -f alpha.txt && ! test -e beta.txt`

### 2. [ ] feat: add beta
- **Why:** w
- **Verify:** `test -f alpha.txt && test -f beta.txt && ! test -e gamma.txt`

### 3. [ ] feat: add gamma
- **Why:** w
- **Verify:** `test -f alpha.txt && test -f beta.txt && test -f gamma.txt`

## Log
- 2026-11-18: created
"""

ROADMAP = (
    "# Roadmap: Demo\n\n## Milestones\n\n### M1 [ ] First milestone\n- **Goal:** g\n\n"
    "## Changelog\n- 2026-11-18: roadmap created\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


class StepAdapter:
    """Writes one file per call: alpha, beta, gamma."""

    names = ["alpha", "beta", "gamma"]

    def __init__(self) -> None:
        self.calls = 0

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        (repo / f"{self.names[self.calls]}.txt").write_text("x\n")
        self.calls += 1
        return AgentResult(ok=True, text="done", cost=0.0)


@pytest.fixture
def milestone_repo(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@example.com")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".git" / "info" / "exclude").write_text(".agent-plan.md\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "ROADMAP.md").write_text(ROADMAP)
    (repo / "README.md").write_text("demo\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / ".agent-plan.md").write_text(PLAN)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    return repo, remote


def _verify_commands() -> list[str]:
    return re.findall(r"- \*\*Verify:\*\* `([^`]+)`", PLAN)


def test_milestone_ends_as_branch_with_one_green_commit_per_step(milestone_repo, tmp_path) -> None:
    repo, remote = milestone_repo
    base = head(repo)
    adapter = StepAdapter()

    for _ in range(3):
        out = io.StringIO()
        assert run_next(repo, adapter, out, today="2026-11-19", push=True) == EXIT_OK, (
            out.getvalue()
        )

    branch = "baton/m1-first-milestone"
    assert _git(repo, "branch", "--show-current").strip() == branch
    assert _git(repo, "rev-parse", "main").strip().startswith(base)

    commits = _git(repo, "rev-list", "--reverse", f"main..{branch}").split()
    subjects = [_git(repo, "log", "-1", "--format=%s", c).strip() for c in commits]
    assert subjects == ["feat: add alpha", "feat: add beta", "feat: add gamma"]
    assert _git(remote, "rev-parse", branch).strip() == _git(repo, "rev-parse", "HEAD").strip()

    for i, (commit, verify) in enumerate(zip(commits, _verify_commands(), strict=True)):
        checkout = tmp_path / f"checkout-{i}"
        checkout.mkdir()
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", commit],
            check=True,
            capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(checkout, filter="data")
        result = subprocess.run(["bash", "-c", verify], cwd=checkout, capture_output=True)
        assert result.returncode == 0, f"commit {i + 1} fails its Verify: {verify}"
        assert not (checkout / ".agent-plan.md").exists()

    def roadmap_at(rev: str) -> str:
        return _git(repo, "show", f"{rev}:docs/ROADMAP.md")

    assert "### M1 [~] First milestone" in roadmap_at(commits[0])
    assert roadmap_at(commits[1]) == roadmap_at(commits[0])
    assert "### M1 [x] First milestone" in roadmap_at(commits[2])
    assert "M1 done (Demo feature)" in roadmap_at(commits[2])
    assert _git(repo, "status", "--porcelain").strip() == ""
