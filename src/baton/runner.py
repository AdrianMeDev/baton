"""Run external commands with a timeout and capture their output."""

from __future__ import annotations

import os
import pty
import re
import select
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KILL_GRACE_S = 5.0
READ_CHUNK = 4096
POLL_S = 0.1

# CSI sequences, OSC sequences (BEL or ST terminated) and other two-byte escapes.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    """Remove terminal escape sequences and normalise CRLF line endings."""
    return _ANSI_RE.sub("", text).replace("\r\n", "\n")


@dataclass(frozen=True)
class RunResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    duration_s: float


def _terminate(proc: subprocess.Popen[Any]) -> None:
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


def _run_tty(
    argv: Sequence[str],
    cwd: Path | str,
    timeout: float,
    env: Mapping[str, str] | None,
    start: float,
) -> RunResult:
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=None if env is None else dict(env),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
    except OSError as exc:
        os.close(master)
        return RunResult("", str(exc), 127, False, time.monotonic() - start)
    finally:
        os.close(slave)

    chunks: list[bytes] = []
    deadline = start + timeout
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([master], [], [], min(remaining, POLL_S))
            if ready:
                try:
                    data = os.read(master, READ_CHUNK)
                except OSError:  # EIO: every slave end is closed
                    break
                if not data:
                    break
                chunks.append(data)
            elif proc.poll() is not None:
                break
        if not timed_out:
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), 0.0))
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out:
            _terminate(proc)
        # Drain whatever is still buffered.
        while select.select([master], [], [], 0)[0]:
            try:
                data = os.read(master, READ_CHUNK)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        os.close(master)
    text = strip_ansi(b"".join(chunks).decode("utf-8", errors="replace"))
    return RunResult(
        stdout=text,
        stderr="",
        exit_code=None if timed_out else proc.returncode,
        timed_out=timed_out,
        duration_s=time.monotonic() - start,
    )


def run_command(
    argv: Sequence[str],
    cwd: Path | str,
    timeout: float,
    env: Mapping[str, str] | None = None,
    tty: bool = False,
) -> RunResult:
    """Run ``argv`` in ``cwd`` and capture stdout and stderr.

    With ``tty=True`` the command runs on a pseudo-terminal: stdout and stderr arrive
    interleaved in ``stdout`` (``stderr`` stays empty) with ANSI escapes removed.

    On timeout the process group is terminated and ``timed_out`` is True. A command
    that cannot be started yields exit code 127 and the error text in stderr.
    """
    start = time.monotonic()
    if tty:
        return _run_tty(argv, cwd, timeout, env, start)
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
