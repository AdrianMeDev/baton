"""M10 acceptance: `baton auto` runs a two-milestone roadmap to merged, offline.

Fake agent executables, a stateful fake ``gh`` that fast-forwards the bare origin's base on
merge, and a ``git`` shim that fetches the GitHub-looking origin from the local bare repository.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from baton.auto import run_auto_command
from baton.autostate import AutoStore
from baton.roadmap import parse_roadmap

ROADMAP = """# Roadmap: Demo
Updated: 2026-11-18

## Vision
Parts exist.

## Milestones

### M1 [ ] First milestone
- **Goal:** Alpha works.
- **Done when:** alpha is tested
- **Depends on:** –
- **Size:** S, ~3 commits

### M2 [ ] Second milestone
- **Goal:** Beta works.
- **Done when:** beta is tested
- **Depends on:** M1
- **Size:** S, ~3 commits

## Definition of done (every milestone)
- tests green

## Changelog
- 2026-11-18: roadmap created
"""

CONFIG = """[agents.planner]
adapter = "claude"
command = "fake-planner"
[agents.coder]
adapter = "claude"
command = "fake-coder"
[agents.checker]
adapter = "claude"
command = "fake-checker"
provider = "independent"
"""
for _role in ("plan_commits", "implement", "fix", "review"):
    _agent = {"plan_commits": "planner", "review": "checker"}.get(_role, "coder")
    CONFIG += f"[routes.{_role}]\n" + "".join(f'{t} = ["{_agent}"]\n' for t in "SML")

FAKE_PLANNER = """#!{python}
import json, re, sys
prompt = sys.argv[2]
m = re.search(r"planning milestone (M\\d+) – (.+?) of the roadmap", prompt)
mid, name = m[1], m[2]
steps = "\\n".join(
    f"### {{n}}. [ ] feat({{mid.lower()}}): add part {{n}}\\n- **Why:** needed.\\n"
    f"- **Changes:** `{{mid.lower()}}_{{n}}.txt` – new file\\n- **Done when:** the file exists\\n"
    f"- **Verify:** `test -f {{mid.lower()}}_{{n}}.txt`\\n- **Size:** S\\n"
    for n in (1, 2, 3)
)
plan = (
    f"# Plan: {{name}}\\nMilestone: {{mid}} – {{name}} (docs/ROADMAP.md, tracked)\\n\\n"
    f"## Goal\\nIt works.\\n\\n## Assumptions\\n- none\\n\\n## Steps\\n\\n{{steps}}\\n## Log\\n"
)
print(json.dumps({{"is_error": False, "result": plan}}))
"""

FAKE_CODER = """#!{python}
import json, re, sys
from pathlib import Path
prompt = sys.argv[2]
for name in re.findall(r"\\*\\*Changes:\\*\\* `([^`]+)`", prompt):
    Path(name).write_text("x\\n")
print(json.dumps({{"is_error": False, "result": "done"}}))
"""

FAKE_CHECKER = """#!{python}
import json
import os
print(json.dumps({{"is_error": False, "result": '{{"findings": []}}'}}))
"""

FAKE_GH = """#!{python}
import json, os, subprocess, sys
from pathlib import Path
bare = os.environ["FAKE_BARE"]
state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
a = sys.argv[1:]
state["calls"].append(a[:2])


def opt(name):
    return a[a.index(name) + 1]


def tip(branch):
    return subprocess.run(
        ["git", "--git-dir", bare, "rev-parse", branch], capture_output=True, text=True, check=True
    ).stdout.strip()


def view(pr):
    return dict(
        number=pr["number"], url=pr["url"], state="MERGED" if pr["merged"] else "OPEN",
        headRefOid=tip(pr["head"]),
        statusCheckRollup=[dict(__typename="CheckRun", status="COMPLETED", conclusion="SUCCESS")],
    )


def by_url(url):
    return next(p for p in state["prs"] if p["url"] == url)


out = ""
if a[1] == "list":
    found = [p for p in state["prs"] if p["head"] == opt("--head")]
    if opt("--state") == "merged":
        found = [p for p in found if p["merged"]]
    out = json.dumps([view(p) for p in found])
elif a[1] == "create":
    n = len(state["prs"]) + 1
    pr = dict(number=n, url=f"https://github.com/test/baton/pull/{{n}}", head=opt("--head"),
              merged=False)
    assert "Independent reviewer" in Path(opt("--body-file")).read_text()
    state["prs"].append(pr)
    out = pr["url"]
elif a[1] == "view":
    out = json.dumps(view(by_url(a[2])))
elif a[1] == "merge":
    pr = by_url(a[2])
    assert opt("--match-head-commit") == tip(pr["head"])
    pr["merged"] = True
    subprocess.run(["git", "--git-dir", bare, "update-ref", "refs/heads/main", tip(pr["head"])],
                   check=True)
state_path.write_text(json.dumps(state))
print(out)
"""

GIT_SHIM = """#!/bin/sh
# Fetching the GitHub-looking origin reads the local bare repository instead.
for arg in "$@"; do
  if [ "$arg" = fetch ]; then
    exec {git} -c "url.$FAKE_BARE.insteadOf=https://github.com/test/baton.git" "$@"
  fi
done
exec {git} "$@"
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def script(path: Path, text: str) -> None:
    path.write_text(text.format(python=sys.executable, git=shutil.which("git")))
    path.chmod(0o755)


@pytest.fixture
def world(tmp_path, monkeypatch):
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@example.com")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "docs" / "ROADMAP.md").write_text(ROADMAP)
    (repo / "README.md").write_text("demo\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", "https://github.com/test/baton.git")
    git(repo, "remote", "set-url", "--push", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script(bin_dir / "fake-planner", FAKE_PLANNER)
    script(bin_dir / "fake-coder", FAKE_CODER)
    script(bin_dir / "fake-checker", FAKE_CHECKER)
    script(bin_dir / "gh", FAKE_GH)
    script(bin_dir / "git", GIT_SHIM)
    state = tmp_path / "gh-state.json"
    state.write_text(json.dumps(dict(prs=[], calls=[])))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_BARE", str(bare))
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    return repo, bare, state, config


def test_two_milestone_roadmap_runs_to_merged_base(world, tmp_path):
    repo, bare, state, config = world
    notes: list[tuple[str, str]] = []
    out = tmp_path / "out.txt"
    with out.open("w") as stream:
        code = run_auto_command(
            repo,
            config_path=str(config),
            out=stream,
            notifier=lambda title, message: notes.append((title, message)),
        )
    log = out.read_text()
    assert code == 0, log

    # Both milestones are done on origin's base, each through exactly one merged PR.
    roadmap = parse_roadmap(git(bare, "show", "main:docs/ROADMAP.md"))
    assert [m.status for m in roadmap.milestones] == ["done", "done"]
    prs = json.loads(state.read_text())["prs"]
    assert [(p["head"], p["merged"]) for p in prs] == [
        ("baton/m1-first-milestone", True),
        ("baton/m2-second-milestone", True),
    ]
    subjects = git(bare, "log", "--format=%s", "main").splitlines()
    assert subjects[:6] == [f"feat(m{m}): add part {n}" for m in (2, 1) for n in (3, 2, 1)]
    assert subjects[-1] == "init"
    assert len(subjects) == 7

    # The user's checkout is the fast-forwarded, clean base; merged worktrees are gone.
    assert git(repo, "branch", "--show-current").strip() == "main"
    assert git(repo, "rev-parse", "HEAD") == git(bare, "rev-parse", "main")
    assert git(repo, "status", "--porcelain").strip() == ""
    assert len(git(repo, "worktree", "list").splitlines()) == 1

    state_after = AutoStore().read(repo)
    assert state_after.milestone == "" and state_after.steps_done == 6
    assert [title for title, _ in notes] == [
        "baton: M1 merged",
        "baton: M2 merged",
        "baton: roadmap complete",
    ]
