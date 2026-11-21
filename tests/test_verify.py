from baton.verify import run_verify


def test_passing_command(tmp_path):
    res = run_verify("true", tmp_path, timeout=10)
    assert res.verified and res.passed
    assert res.exit_code == 0
    assert not res.timed_out


def test_failing_command(tmp_path):
    res = run_verify("echo nope >&2; exit 3", tmp_path, timeout=10)
    assert res.verified and not res.passed
    assert res.exit_code == 3
    assert "nope" in res.output_tail


def test_timeout(tmp_path):
    res = run_verify("sleep 30", tmp_path, timeout=1)
    assert res.verified and not res.passed
    assert res.timed_out
    assert res.exit_code is None


def test_empty_command_is_not_verified(tmp_path):
    for cmd in ("", "   "):
        res = run_verify(cmd, tmp_path, timeout=10)
        assert not res.verified and not res.passed


def test_output_tail_is_limited(tmp_path):
    res = run_verify("seq 1 500", tmp_path, timeout=10)
    lines = res.output_tail.splitlines()
    assert lines[-1] == "500"
    assert len(lines) == 40
    assert "1" not in lines[:1]


def test_runs_in_repo(tmp_path):
    res = run_verify("pwd", tmp_path, timeout=10)
    assert res.output_tail.strip() == str(tmp_path.resolve())


def test_backticks_from_plan_are_stripped(tmp_path):
    res = run_verify("`echo hi && true`", tmp_path, timeout=10)
    assert res.passed
    assert res.output_tail == "hi"


def _repo(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("base\n")
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@e.com", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    return repo


def _worktrees(repo):
    from baton.git import worktree_paths

    return worktree_paths(repo)


def test_run_verify_at_head_ignores_uncommitted_changes(tmp_path):
    from baton.verify import run_verify_at_head

    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("dirty\n")
    (repo / "new.txt").write_text("x")
    cmd = "grep -q base a.txt && test ! -e new.txt"
    assert not run_verify(cmd, repo, timeout=10).passed
    assert run_verify_at_head(cmd, repo, timeout=10).passed
    assert len(_worktrees(repo)) == 1


def test_run_verify_at_head_failure_and_timeout_remove_worktree(tmp_path):
    from baton.verify import run_verify_at_head

    repo = _repo(tmp_path)
    assert not run_verify_at_head("exit 1", repo, timeout=10).passed
    assert run_verify_at_head("sleep 5", repo, timeout=0.3).timed_out
    assert len(_worktrees(repo)) == 1
