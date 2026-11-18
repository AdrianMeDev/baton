"""Public command acceptance against local Git and a stateful executable gh."""

import json
import os
import subprocess
import sys

import pytest
from test_next import PLAN, git

from baton.config import AgentConfig
from baton.git import head
from baton.review import ReviewResult, candidate_tree, evidence_entry
from baton.verify import run_verify


@pytest.mark.parametrize("mode", ["publish", "green", "red"])
def test_cli_publication(tmp_path, mode):
    repo = tmp_path / "repo"
    repo.mkdir()
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare")
    git(repo, "init", "-b", "main")
    (repo / ".gitignore").write_text(".agent-plan.md\n")
    (repo / "a").write_text("base")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = head(repo, short=False)
    git(repo, "remote", "add", "origin", "https://github.com/test/baton.git")
    # Resolve GitHub URLs locally so the real push exercises a bare origin offline.
    git(repo, "config", f"url.{origin}.insteadOf", "https://github.com/test/baton.git")
    git(repo, "switch", "-c", "baton/demo")
    (repo / "a").write_text("implementation")
    evidence = evidence_entry(
        1,
        candidate_tree(repo),
        base,
        run_verify("true", repo, 10),
        ReviewResult(AgentConfig(name="reviewer", adapter="claude", command="claude"), ()),
    )
    git(repo, "commit", "-am", "implementation")
    revision = head(repo, short=False)
    text = PLAN.format(s1=f"[x] – {revision}", verify="true").replace("abc1234", base)
    (repo / ".agent-plan.md").write_text(
        text.replace("### 2. [ ]", "### 2. [-]") + "- " + evidence + "\n"
    )
    # Use separate pushurl; origin's fetch URL identifies the GitHub repository.
    git(repo, "config", "--unset", f"url.{origin}.insteadOf")
    git(repo, "remote", "set-url", "--push", "origin", str(origin))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps(dict(exists=False, merged=False, calls=[])))
    executable = bindir / "gh"
    executable.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p = Path(os.environ['FAKE_STATE'])
s = json.loads(p.read_text())
a = sys.argv[1:]
s['calls'].append(a)
result = ''
pr = dict(number=1, url='https://github.com/test/baton/pull/1',
          state='MERGED' if s['merged'] else 'OPEN', headRefOid=os.environ['REVISION'])
if a[1] == 'list':
    result = json.dumps([pr] if s['exists'] and ('merged' not in a or s['merged']) else [])
elif a[1] == 'create':
    assert Path(a[a.index('--body-file')+1]).read_text().find('Independent reviewer') >= 0
    s['exists'] = True
    result = pr['url']
elif a[1] == 'view':
    pr['statusCheckRollup'] = [dict(__typename='CheckRun', status='COMPLETED',
        conclusion='FAILURE' if os.environ['MODE'] == 'red' else 'SUCCESS')]
    result = json.dumps(pr)
elif a[1] == 'merge':
    assert a[a.index('--match-head-commit')+1] == os.environ['REVISION']
    s['merged'] = True
p.write_text(json.dumps(s))
print(result)
""")
    executable.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(bindir) + os.pathsep + os.environ["PATH"],
        FAKE_STATE=str(state),
        REVISION=revision,
        MODE=mode,
    )
    args = [sys.executable, "-m", "baton", "pr", "--path", str(repo)]
    if mode != "publish":
        args.append("--merge")
    for _ in range(2):
        result = subprocess.run(args, env=env, capture_output=True, text=True)
        assert result.returncode == (1 if mode == "red" else 0), result.stdout + result.stderr
        assert "https://github.com/test/baton/pull/1" in result.stdout
    data = json.loads(state.read_text())
    assert data["merged"] == (mode == "green")
    assert sum(c[1] == "create" for c in data["calls"]) == 1
