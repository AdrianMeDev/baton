"""M8 acceptance: `baton plan` turns the example roadmap into a plan `baton next` accepts."""

import io
import subprocess
from pathlib import Path

import pytest

from baton.adapter import AgentResult
from baton.next import EXIT_FAILED, EXIT_OK, run_next_command
from baton.plan import first_open_step, parse_plan
from baton.planner import run_plan

ROADMAP = """# Roadmap: Demo
Updated: 2026-11-18

## Architecture
- One module per feature.

## Milestones

### M1 [ ] First milestone
- **Goal:** Alpha works.
- **Done when:** alpha is tested
- **Depends on:** –
- **Size:** S, ~3 commits

### M2 [ ] Second milestone
- **Goal:** Beta works.
- **Depends on:** M1
- **Size:** M, ~3 commits

## Definition of done (every milestone)
- tests green

## Changelog
- 2026-11-18: roadmap created
"""

STEP = """### {n}. [ ] feat(alpha): add part {n}
- **Why:** part {n} is needed.
- **Changes:** `alpha_{n}.txt` – new file
- **Done when:** `alpha_{n}.txt` exists
- **Verify:** `test -f alpha_{n}.txt`
- **Size:** S
"""

REPLY = (
    "Here is the plan.\n\n```markdown\n# Plan: Alpha\n"
    "Milestone: M1 – First milestone (docs/ROADMAP.md, tracked)\n\n"
    "## Goal\nAlpha works for users.\n\n## Assumptions\n- none\n\n## Steps\n\n"
    + "\n".join(STEP.format(n=n) for n in (1, 2, 3))
    + "\n## Log\n```\n"
)
INVALID = REPLY.replace("- **Verify:** `test -f alpha_2.txt`\n", "")


class FakePlanner:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        self.prompts.append(prompt)
        return AgentResult(ok=True, text=self.replies.pop(0))


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def isolated_limit_state(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("limits-state")))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "docs" / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def test_valid_plan_for_example_roadmap_is_accepted_by_next(repo: Path, tmp_path: Path):
    planner = FakePlanner(REPLY)
    out = io.StringIO()
    assert run_plan(repo, planner, out, today="2026-11-19") == EXIT_OK, out.getvalue()
    assert "### M1 [ ] First milestone" in planner.prompts[0]
    assert "M2 [ ] Second milestone" not in planner.prompts[0]

    plan = parse_plan((repo / ".agent-plan.md").read_text(encoding="utf-8"))
    assert plan.title == "Alpha"
    assert plan.milestone.id == "M1"
    assert [s.size for s in plan.steps] == ["S", "S", "S"]
    step = first_open_step(plan)
    assert step is not None and step.message == "feat(alpha): add part 1"

    config = tmp_path / "config.toml"
    config.write_text(
        '[agents.fake]\nadapter = "claude"\ncommand = "claude"\n[routes.implement]\nS = ["fake"]\n',
        encoding="utf-8",
    )
    preview = io.StringIO()
    code = run_next_command(repo, config_path=str(config), dry_run=True, out=preview)
    assert code == EXIT_OK, preview.getvalue()
    assert "Step 1/3: feat(alpha): add part 1" in preview.getvalue()
    assert "Tier S, agent fake (claude)" in preview.getvalue()


def test_invalid_plan_is_rejected_twice_and_leaves_no_plan(repo: Path):
    planner = FakePlanner(INVALID, INVALID)
    out = io.StringIO()
    assert run_plan(repo, planner, out, today="2026-11-19") == EXIT_FAILED
    assert len(planner.prompts) == 2
    assert "step 2: missing '**Verify:**'" in planner.prompts[1]
    assert not (repo / ".agent-plan.md").exists()
    assert "No valid plan after 2 attempts" in out.getvalue()
