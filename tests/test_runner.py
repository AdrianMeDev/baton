import sys
import time

from baton.runner import run_command


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_success_captures_stdout(tmp_path):
    res = run_command(py("print('hi')"), tmp_path, timeout=10)
    assert res.stdout == "hi\n"
    assert res.exit_code == 0
    assert not res.timed_out
    assert res.duration_s >= 0


def test_nonzero_exit_and_stderr(tmp_path):
    res = run_command(py("import sys; sys.stderr.write('boom'); sys.exit(3)"), tmp_path, timeout=10)
    assert res.exit_code == 3
    assert res.stderr == "boom"
    assert res.stdout == ""


def test_runs_in_cwd(tmp_path):
    res = run_command(py("import os; print(os.getcwd())"), tmp_path, timeout=10)
    assert res.stdout.strip() == str(tmp_path.resolve())


def test_timeout_kills_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(60)\n"
    )
    start = time.monotonic()
    res = run_command(py(code), tmp_path, timeout=1)
    assert res.timed_out
    assert res.exit_code is None
    assert time.monotonic() - start < 15
    child = int(pidfile.read_text())
    import os

    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("child process still running")


def test_missing_command(tmp_path):
    res = run_command(["definitely-not-a-command-xyz"], tmp_path, timeout=5)
    assert res.exit_code == 127
    assert res.stderr
    assert not res.timed_out
