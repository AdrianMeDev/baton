import sys
import time

from baton.runner import run_command, strip_ansi


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


TTY_ONLY = "import sys; print('tty' if sys.stdout.isatty() else '', end='')"


def test_tty_only_output_needs_terminal(tmp_path):
    assert run_command(py(TTY_ONLY), tmp_path, timeout=10).stdout == ""
    res = run_command(py(TTY_ONLY), tmp_path, timeout=10, tty=True)
    assert res.stdout == "tty"
    assert res.exit_code == 0


def test_tty_merges_streams_and_strips_ansi(tmp_path):
    code = "import sys; print('\\x1b[31mred\\x1b[0m'); sys.stderr.write('err\\n')"
    res = run_command(py(code), tmp_path, timeout=10, tty=True)
    assert res.stdout == "red\nerr\n"
    assert res.stderr == ""


def test_tty_exit_code(tmp_path):
    res = run_command(py("import sys; print('x'); sys.exit(4)"), tmp_path, timeout=10, tty=True)
    assert res.exit_code == 4
    assert res.stdout == "x\n"


def test_tty_timeout(tmp_path):
    start = time.monotonic()
    code = "import time; print('start', flush=True); time.sleep(60)"
    res = run_command(py(code), tmp_path, timeout=1, tty=True)
    assert res.timed_out
    assert res.exit_code is None
    assert res.stdout == "start\n"
    assert time.monotonic() - start < 15


def test_tty_missing_command(tmp_path):
    res = run_command(["definitely-not-a-command-xyz"], tmp_path, timeout=5, tty=True)
    assert res.exit_code == 127
    assert res.stderr


def test_strip_ansi():
    assert strip_ansi("\x1b[1;32mok\x1b[0m\x1b]0;title\x07 done\r\n") == "ok done\n"
