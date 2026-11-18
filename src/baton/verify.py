"""Run a plan step's Verify command and summarise the outcome."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from baton.runner import run_command

TAIL_LINES = 40


@dataclass(frozen=True)
class VerifyResult:
    verified: bool  # False: there was no command to run
    passed: bool
    exit_code: int | None
    timed_out: bool
    output_tail: str
    command: str = ""


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])


def _clean(command: str) -> str:
    """Drop the markdown backticks that wrap a Verify command in the plan."""
    command = command.strip()
    if len(command) >= 2 and command.startswith("`") and command.endswith("`"):
        command = command[1:-1].strip()
    return command


def run_verify(command: str, repo: Path, timeout: float) -> VerifyResult:
    """Run ``command`` via ``bash -c`` in ``repo``.

    An empty command yields a "not verified" result (``verified=False``, ``passed=False``).
    """
    command = _clean(command)
    if not command:
        return VerifyResult(False, False, None, False, "")
    res = run_command(["bash", "-c", command], repo, timeout)
    output = res.stdout + (("\n" + res.stderr) if res.stderr else "")
    return VerifyResult(
        verified=True,
        passed=not res.timed_out and res.exit_code == 0,
        exit_code=res.exit_code,
        timed_out=res.timed_out,
        output_tail=_tail(output),
        command=command,
    )
