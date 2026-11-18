import subprocess
from pathlib import Path

import pytest

from baton.git import GitError, dirty_paths, head, is_repo


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


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
