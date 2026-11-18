"""``baton probe``: start an agent CLI headless with a tiny read-only request."""

from __future__ import annotations

import shlex
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from baton.config import AgentConfig, ConfigError, find_config_path, load_config
from baton.recording import Recording, save
from baton.runner import run_command

DEFAULT_PROMPT = "Reply with exactly: OK"
DEFAULT_TIMEOUT = 120.0
VERSION_TIMEOUT = 15.0


def probe_argv(agent: AgentConfig, prompt: str = DEFAULT_PROMPT) -> list[str]:
    """Headless, read-only invocation for the agent's adapter (see docs/adapters.md)."""
    cmd = agent.command
    if agent.adapter == "claude":
        argv = [cmd, "-p", prompt, "--output-format", "json", "--permission-mode", "plan"]
        model_flag = "--model"
    elif agent.adapter == "codex":
        argv = [cmd, "exec", prompt, "--json", "--sandbox", "read-only", "--skip-git-repo-check"]
        model_flag = "-m"
    elif agent.adapter == "agy":
        argv = [cmd, "-p", prompt, "--output-format", "json", "--mode", "plan"]
        model_flag = "--model"
    elif agent.adapter == "pi":
        argv = [cmd, "-p", prompt, "--mode", "json"]
        model_flag = "--model"
    else:  # unreachable for validated configs
        raise ValueError(f"unknown adapter {agent.adapter!r}")
    if agent.model:
        argv += [model_flag, agent.model]
    return argv


def run_probe(
    agent: AgentConfig,
    prompt: str = DEFAULT_PROMPT,
    timeout: float = DEFAULT_TIMEOUT,
    tty: bool = False,
    record_path: str | Path | None = None,
) -> Recording:
    """Run the probe in an empty temporary directory; optionally save a redacted recording."""
    argv = probe_argv(agent, prompt)
    with tempfile.TemporaryDirectory(prefix="baton-probe-") as tmp:
        version = run_command([agent.command, "--version"], tmp, VERSION_TIMEOUT)
        result = run_command(argv, tmp, timeout, tty=tty)
        rec = Recording(
            adapter=agent.adapter,
            agent=agent.name,
            argv=argv,
            tty=tty,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_s=round(result.duration_s, 3),
            stdout=result.stdout,
            stderr=result.stderr,
            recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
            cli_version=(version.stdout or version.stderr).strip(),
        )
        if record_path is not None:
            save(rec, record_path, workdir=tmp)
    return rec


def run_probe_command(
    name: str,
    config_path: str | None = None,
    prompt: str = DEFAULT_PROMPT,
    model: str | None = None,
    tty: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    record: str | None = None,
    dry_run: bool = False,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Exit 0 only if the agent exited 0; 1 for unknown agents, config errors or failures."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err

    try:
        config = load_config(find_config_path(config_path))
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return 1
    agent = config.agents.get(name)
    if agent is None:
        known = ", ".join(config.agents) or "none"
        print(f"error: unknown agent {name!r} (configured: {known})", file=err)
        return 1
    if model:
        agent = replace(agent, model=model)

    if dry_run:
        print(shlex.join(probe_argv(agent, prompt)), file=out)
        return 0

    rec = run_probe(agent, prompt, timeout, tty, record)
    print(f"$ {shlex.join(rec.argv)}", file=out)
    print(
        f"exit_code={rec.exit_code} timed_out={rec.timed_out} duration={rec.duration_s}s"
        f" tty={rec.tty} version={rec.cli_version or '?'}",
        file=out,
    )
    if rec.stdout:
        print(f"--- stdout ---\n{rec.stdout.rstrip()}", file=out)
    if rec.stderr:
        print(f"--- stderr ---\n{rec.stderr.rstrip()}", file=out)
    if record:
        print(f"recorded: {record}", file=out)
    return 0 if rec.exit_code == 0 and not rec.timed_out else 1
