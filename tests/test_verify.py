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
