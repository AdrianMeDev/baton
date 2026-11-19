import subprocess
from pathlib import Path

import pytest

from baton.milestone import sync_milestone
from baton.plan import parse_plan
from baton.roadmap import RoadmapError, parse_roadmap

ROADMAP = """# Roadmap: Demo

## Milestones

### M1 [{marker}] First
- **Goal:** g

## Changelog
- 2026-11-01: roadmap created
"""

PLAN = """# Plan: Demo plan
Created: 2026-11-18 · Branch: main · Base: abc1234
Milestone: {milestone}

## Steps

### 1. {s1} feat: first
- **Verify:** `true`

### 2. {s2} feat: second
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


TRACKED = "M1 – First (docs/ROADMAP.md, tracked)"


def make_plan(s1="[ ]", s2="[ ]", milestone=TRACKED):
    return parse_plan(PLAN.format(s1=s1, s2=s2, milestone=milestone))


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@example.com")
    git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "info" / "exclude").write_text(".agent-plan.md\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ROADMAP.md").write_text(ROADMAP.format(marker=" "))
    (tmp_path / ".agent-plan.md").write_text(PLAN.format(s1="[ ]", s2="[ ]", milestone=TRACKED))
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def roadmap(repo: Path):
    return parse_roadmap((repo / "docs" / "ROADMAP.md").read_text())


def test_first_step_marks_milestone_pending(repo: Path) -> None:
    plan = make_plan()
    assert sync_milestone(repo, plan, plan.steps[0], "2026-11-19") is not None
    m = roadmap(repo)
    assert m.milestones[0].status == "pending"
    assert len(m.changelog) == 1


def test_middle_step_changes_nothing_when_already_pending(repo: Path) -> None:
    (repo / "docs" / "ROADMAP.md").write_text(ROADMAP.format(marker="~"))
    plan3 = parse_plan(
        PLAN.format(s1="[x] – abc1234", s2="[ ]", milestone=TRACKED).replace(
            "## Log", "### 3. [ ] feat: third\n\n## Log"
        )
    )
    assert sync_milestone(repo, plan3, plan3.steps[1], "2026-11-19") is None


def test_last_step_marks_done_with_changelog(repo: Path) -> None:
    (repo / "docs" / "ROADMAP.md").write_text(ROADMAP.format(marker="~"))
    plan = make_plan("[x] – abc1234")
    assert sync_milestone(repo, plan, plan.steps[1], "2026-11-19") is not None
    m = roadmap(repo)
    assert m.milestones[0].status == "done"
    assert m.changelog[-1] == "2026-11-19: M1 done (Demo plan)"


def test_already_done_roadmap_is_left_alone(repo: Path) -> None:
    (repo / "docs" / "ROADMAP.md").write_text(ROADMAP.format(marker="x"))
    plan = make_plan("[x] – abc1234")
    assert sync_milestone(repo, plan, plan.steps[1], "2026-11-19") is None
    assert len(roadmap(repo).changelog) == 1


def test_skipped_steps_count_as_finished(repo: Path) -> None:
    (repo / "docs" / "ROADMAP.md").write_text(ROADMAP.format(marker="~"))
    plan = make_plan("[-]")
    sync_milestone(repo, plan, plan.steps[1], "2026-11-19")
    assert roadmap(repo).milestones[0].status == "done"


@pytest.mark.parametrize("milestone", ["none", "M1 – First (docs/ROADMAP.md, local)"])
def test_local_and_standalone_plans_are_skipped(repo: Path, milestone: str) -> None:
    plan = make_plan(milestone=milestone)
    assert sync_milestone(repo, plan, plan.steps[0], "2026-11-19") is None
    assert roadmap(repo).milestones[0].status == "open"


def test_missing_roadmap_or_milestone_raises(repo: Path) -> None:
    plan = make_plan(milestone="M9 – X (docs/ROADMAP.md, tracked)")
    with pytest.raises(RoadmapError):
        sync_milestone(repo, plan, plan.steps[0], "2026-11-19")
    (repo / "docs" / "ROADMAP.md").unlink()
    with pytest.raises(RoadmapError):
        sync_milestone(repo, make_plan(), plan.steps[0], "2026-11-19")


@pytest.mark.parametrize(
    "target", ["/tmp/outside-roadmap", "../outside.md", "untracked.md", "escape.md"]
)
def test_invalid_tracked_targets_are_rejected(repo, tmp_path, target):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text(ROADMAP.format(marker=" "))
    if target == "/tmp/outside-roadmap":
        target = str(outside)
    elif target == "../outside.md":
        target = "../" + outside.name
    elif target == "untracked.md":
        (repo / target).write_text(outside.read_text())
    else:
        (repo / target).symlink_to(outside)
        git(repo, "add", target)
    before = outside.read_bytes()
    plan = make_plan(milestone=f"M1 – First ({target}, tracked)")
    with pytest.raises(RoadmapError):
        sync_milestone(repo, plan, plan.steps[0], "2026-11-19")
    assert outside.read_bytes() == before


def test_tracked_roadmap_in_linked_worktree(repo, tmp_path):
    work = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "linked", str(work))
    plan = make_plan()
    sync_milestone(work, plan, plan.steps[0], "2026-11-19")
    assert roadmap(work).milestones[0].status == "pending"
    assert roadmap(repo).milestones[0].status == "open"
