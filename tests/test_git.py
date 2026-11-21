import subprocess
from pathlib import Path

import pytest

from baton.git import (
    GitError,
    branch_exists,
    branch_slug,
    commit_all,
    current_branch,
    dirty_paths,
    ensure_excluded,
    head,
    is_repo,
    is_tracked,
    milestone_branch,
    push,
    switch_branch,
)
from baton.plan import MilestoneRef, Plan


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # commit_all runs plain `git commit`, which needs an identity on CI runners.
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@example.com")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_is_repo(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    assert is_repo(repo)
    assert not is_repo(tmp_path_factory.mktemp("plain"))


def test_head_short_and_full(repo: Path) -> None:
    full = head(repo, short=False)
    assert len(full) == 40
    assert full.startswith(head(repo))
    assert len(head(repo)) < 40


def test_head_without_commits(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    with pytest.raises(GitError, match="no commits"):
        head(tmp_path)


def test_non_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="not a git repository"):
        head(tmp_path)
    with pytest.raises(GitError, match="not a git repository"):
        dirty_paths(tmp_path)


def test_clean_tree(repo: Path) -> None:
    assert dirty_paths(repo) == []


def test_modified_staged_and_untracked(repo: Path) -> None:
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "b.txt")
    (repo / "sub").mkdir()
    (repo / "sub" / "c.txt").write_text("c\n")
    assert sorted(dirty_paths(repo)) == ["a.txt", "b.txt", "sub/c.txt"]


def test_plan_file_is_ignored_by_default(repo: Path) -> None:
    (repo / ".agent-plan.md").write_text("x\n")
    assert dirty_paths(repo) == []
    assert dirty_paths(repo, ignore=()) == [".agent-plan.md"]


def test_rename_reports_new_path_once(repo: Path) -> None:
    git(repo, "mv", "a.txt", "z.txt")
    assert dirty_paths(repo) == ["z.txt"]


def test_handoff_context_includes_both_diffs_and_untracked_paths(repo):
    from baton.git import handoff_context

    (repo / "a.txt").write_text("staged\n")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("unstaged\n")
    (repo / "new\nfile.txt").write_text("read me")
    context = handoff_context(repo)
    assert "Staged diff:" in context and "+staged" in context
    assert "Unstaged diff:" in context and "+unstaged" in context
    assert repr("new\nfile.txt") in context
    assert sorted(dirty_paths(repo)) == ["a.txt", "new\nfile.txt"]


def _plan(milestone: MilestoneRef, title: str = "Some Plan") -> Plan:
    return Plan(title=title, created=None, branch=None, base=None, milestone=milestone, steps=())


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Auto-Commit und Branches", "auto-commit-und-branches"),
        ("Größe über Äpfel & Öl", "groesse-ueber-aepfel-oel"),
        ("  --Weiß!!  ", "weiss"),
    ],
)
def test_branch_slug(text: str, slug: str) -> None:
    assert branch_slug(text) == slug


def test_milestone_branch_with_and_without_milestone() -> None:
    ref = MilestoneRef("M7", "Auto-Commit und Branches", "docs/ROADMAP.md", "tracked")
    assert milestone_branch(_plan(ref)) == "baton/m7-auto-commit-und-branches"
    none = MilestoneRef(None, "", None, "none")
    assert milestone_branch(_plan(none, "Fix the Bug")) == "baton/fix-the-bug"


def test_branch_helpers(repo: Path) -> None:
    first = current_branch(repo)
    assert first is not None
    assert not branch_exists(repo, "baton/x")
    switch_branch(repo, "baton/x", create=True)
    assert branch_exists(repo, "baton/x")
    assert current_branch(repo) == "baton/x"
    switch_branch(repo, first)
    assert current_branch(repo) == first
    with pytest.raises(GitError):
        switch_branch(repo, "missing")


def test_current_branch_detached(repo: Path) -> None:
    git(repo, "checkout", "-q", "--detach")
    assert current_branch(repo) is None


def test_commit_all_skips_plan_file(repo: Path) -> None:
    (repo / "a.txt").write_text("changed\n")
    (repo / "new.txt").write_text("n\n")
    (repo / ".agent-plan.md").write_text("plan\n")
    git(repo, "add", ".agent-plan.md")  # even a staged plan file stays out
    before = head(repo)
    short = commit_all(repo, "feat: thing")
    assert short == head(repo) != before
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%s", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "feat:" in log[0] and ".agent-plan.md" not in log
    assert {"a.txt", "new.txt"} <= set(log)
    assert dirty_paths(repo, ignore=()) == [".agent-plan.md"]


def test_commit_all_nothing_to_commit(repo: Path) -> None:
    (repo / ".agent-plan.md").write_text("plan\n")
    with pytest.raises(GitError, match="nothing to commit"):
        commit_all(repo, "feat: nothing")


def test_commit_all_hook_failure(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho nope >&2\nexit 1\n")
    hook.chmod(0o755)
    (repo / "b.txt").write_text("b\n")
    before = head(repo)
    with pytest.raises(GitError):
        commit_all(repo, "feat: blocked")
    assert head(repo) == before


def test_push_to_bare_remote(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    remote = tmp_path_factory.mktemp("remote")
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    switch_branch(repo, "baton/m1-x", create=True)
    push(repo, "baton/m1-x")
    pushed = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "baton/m1-x"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert pushed == head(repo, short=False)


def test_push_without_remote(repo: Path) -> None:
    with pytest.raises(GitError):
        push(repo, "main")


def test_commit_all_with_gitignored_plan_file(repo: Path) -> None:
    tmp_path = repo
    (tmp_path / ".gitignore").write_text(".agent-plan.md\n")
    (tmp_path / ".agent-plan.md").write_text("plan\n")
    (tmp_path / "f.txt").write_text("x\n")
    commit_all(tmp_path, "feat: x")
    tracked = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files"], check=True, capture_output=True, text=True
    ).stdout.split()
    assert "f.txt" in tracked and ".agent-plan.md" not in tracked


def test_is_tracked(repo: Path) -> None:
    (repo / "new.txt").write_text("n\n")
    assert is_tracked(repo, "a.txt")
    assert not is_tracked(repo, "new.txt")
    assert not is_tracked(repo, "missing.txt")


def test_ensure_excluded_appends_once(repo: Path) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("# comment", encoding="utf-8")
    assert ensure_excluded(repo) is True
    assert ensure_excluded(repo) is False
    assert exclude.read_text(encoding="utf-8") == "# comment\n.agent-plan.md\n"


def test_fetch_ff_merge_and_worktree_remove(tmp_path: Path) -> None:
    from baton.git import fetch, ff_merge, worktree_add, worktree_paths, worktree_remove

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    work, other = tmp_path / "work", tmp_path / "other"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "a").write_text("1")
    git(work, "add", ".")
    git(work, "commit", "-qm", "base")
    git(work, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(other))
    (other / "a").write_text("2")
    git(other, "commit", "-qam", "next")
    git(other, "push", "-q", "origin", "main")

    fetch(work)
    ff_merge(work, "origin/main")
    assert (work / "a").read_text() == "2"

    (work / "a").write_text("local")
    git(work, "commit", "-qam", "local")
    (other / "a").write_text("3")
    git(other, "commit", "-qam", "again")
    git(other, "push", "-q", "origin", "main")
    fetch(work)
    with pytest.raises(GitError):
        ff_merge(work, "origin/main")  # local and remote have diverged

    path = tmp_path / "wt"
    worktree_add(work, path, "feature", create=True)
    assert path.resolve() in {p.resolve() for p in worktree_paths(work)}
    worktree_remove(work, path)
    assert not path.exists()


def test_head_checkout_is_removed_also_on_error(tmp_path: Path) -> None:
    from baton.git import head_checkout, worktree_paths

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    (repo / "a.txt").write_text("dirty\n")
    with pytest.raises(RuntimeError, match="boom"), head_checkout(repo) as checkout:
        assert (checkout / "a.txt").read_text() == "base\n"
        raise RuntimeError("boom")
    assert not checkout.exists()
    assert len(worktree_paths(repo)) == 1
