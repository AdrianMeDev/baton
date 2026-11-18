"""Run external commands with a timeout and capture their output."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

KILL_GRACE_S = 5.0


@dataclass(frozen=True)
class RunResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    duration_s: float


def _terminate(proc: subprocess.Popen[str]) -> None:
    """Stop the whole process group: SIGTERM first, SIGKILL after a grace period."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=KILL_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue


def run_command(
    argv: Sequence[str],
    cwd: Path | str,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> RunResult:
    """Run ``argv`` in ``cwd`` and capture stdout and stderr.

    On timeout the process group is terminated and ``timed_out`` is True. A command
    that cannot be started yields exit code 127 and the error text in stderr.
    """
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return RunResult("", str(exc), 127, False, time.monotonic() - start)

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(proc)
        stdout, stderr = proc.communicate()
    return RunResult(
        stdout=stdout or "",
        stderr=stderr or "",
        exit_code=None if timed_out else proc.returncode,
        timed_out=timed_out,
        duration_s=time.monotonic() - start,
    )
