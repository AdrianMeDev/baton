"""Adapters that run an agent CLI headless to implement a plan step."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any, Protocol

from baton.config import AgentConfig
from baton.limits import UsageLimit, classify_limit
from baton.runner import RunResult, run_command

ERROR_TAIL_CHARS = 500
# Linux caps a single argv string at 128 KiB (MAX_ARG_STRLEN); stay well below it.
ARGV_PROMPT_LIMIT = 100_000


@dataclass(frozen=True)
class AgentResult:
    ok: bool
    text: str = ""
    error: str = ""
    cost: float | None = None
    raw: str = ""
    limit: UsageLimit | None = None
    tokens: int | None = None
    duration_s: float | None = None


class Adapter(Protocol):
    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult: ...


class CommandAdapter(Adapter, Protocol):
    def parse(self, result: RunResult) -> AgentResult: ...

    transport: str

    def argv(self, prompt: str) -> list[str]: ...


def _failure(message: str, raw: str = "") -> AgentResult:
    return AgentResult(ok=False, error=message, raw=raw)


def _classify_failure(parse):
    """Keep success and timeout semantics; inspect only failure diagnostics."""

    @wraps(parse)
    def wrapped(self, result: RunResult) -> AgentResult:
        parsed = replace(
            parse(self, result),
            tokens=_reported_tokens(type(self).__name__, result.stdout),
            duration_s=result.duration_s,
        )
        if parsed.ok or result.timed_out:
            return parsed
        try:
            fields = [json.loads(result.stdout)]
        except json.JSONDecodeError:
            fields = []
            for line in result.stdout.split("\n"):
                with suppress(json.JSONDecodeError):
                    fields.append(json.loads(line))
        diagnostic = parsed.error + "\n" + result.stderr
        limit = classify_limit(diagnostic, patterns=self.limit_patterns)
        for field in fields:
            detected = classify_limit(diagnostic, field, patterns=self.limit_patterns)
            if detected is not None and (limit is None or detected.reset_at is not None):
                limit = detected
        # Non-JSON failure output may itself contain the provider diagnostic.
        if not fields and result.exit_code != 0:
            limit = classify_limit(diagnostic + "\n" + result.stdout, patterns=self.limit_patterns)
        return replace(parsed, limit=limit)

    return wrapped


def _run_argv_adapter(adapter: Any, prompt: str, repo: Path, timeout: float) -> RunResult:
    """Run an adapter whose CLI takes the prompt as an argument.

    Prompts above ``ARGV_PROMPT_LIMIT`` go into a private file (mode 0600) in a temporary
    directory outside the checkout; argv then carries a short instruction to read it.
    """
    if len(prompt.encode("utf-8")) <= ARGV_PROMPT_LIMIT:
        return run_command(adapter.argv(prompt), repo, timeout)
    with tempfile.TemporaryDirectory(prefix="baton-prompt-") as tmp:
        path = Path(tmp) / "prompt.md"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
        instruction = (
            f"Read the complete task from the file {path} and carry it out exactly as written."
        )
        return run_command(adapter.argv(instruction), repo, timeout)


class ClaudeAdapter:
    """``claude -p`` with edits allowed and git blocked (see docs/adapters.md)."""

    transport = "stdin"

    def __init__(
        self,
        command: str = "claude",
        model: str | None = None,
        limit_patterns: tuple[str, ...] | None = None,
        read_only: bool = False,
    ) -> None:
        self.command = command
        self.model = model
        self.limit_patterns = limit_patterns
        self.read_only = read_only

    def argv(self, prompt: str) -> list[str]:
        """Argv without the prompt: ``claude -p`` reads it from stdin."""
        argv = [self.command, "-p", "--output-format", "json"]
        if self.model:
            argv += ["--model", self.model]
        if self.read_only:
            return [*argv, "--permission-mode", "plan"]
        argv += [
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Bash",
            "--disallowedTools",
            "Bash(git:*)",
        ]
        return argv

    @_classify_failure
    def parse(self, result: RunResult) -> AgentResult:
        """Interpret a finished run. Checks ``is_error``, not ``subtype`` (always "success")."""
        raw = result.stdout
        if result.timed_out:
            return _failure(f"timed out after {result.duration_s:.0f}s", raw)
        if not raw.strip():
            detail = result.stderr.strip()[-ERROR_TAIL_CHARS:]
            return _failure(f"no output (exit {result.exit_code}): {detail}".rstrip(": "), raw)
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError:
            return _failure("output is not valid JSON", raw)
        if not isinstance(data, dict):
            return _failure("output is not a JSON object", raw)
        text = data.get("result")
        text = text if isinstance(text, str) else ""
        cost = data.get("total_cost_usd")
        cost = float(cost) if isinstance(cost, int | float) else None
        if data.get("is_error") is not False:
            return AgentResult(
                ok=False, text=text, error=text or "agent reported an error", cost=cost, raw=raw
            )
        if result.exit_code != 0:
            return AgentResult(
                ok=False, text=text, error=f"exit code {result.exit_code}", cost=cost, raw=raw
            )
        return AgentResult(ok=True, text=text, cost=cost, raw=raw)

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        return self.parse(run_command(self.argv(prompt), repo, timeout, input=prompt))


class CodexAdapter:
    """Run ``codex exec`` with workspace edits and JSONL events."""

    transport = "stdin"

    def __init__(
        self,
        command: str = "codex",
        model: str | None = None,
        limit_patterns: tuple[str, ...] | None = None,
        read_only: bool = False,
    ) -> None:
        self.command = command
        self.model = model
        self.limit_patterns = limit_patterns
        self.read_only = read_only

    def argv(self, prompt: str) -> list[str]:
        """Argv without the prompt: ``codex exec -`` reads it from stdin."""
        argv = [
            self.command,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only" if self.read_only else "workspace-write",
        ]
        if self.model:
            argv += ["-m", self.model]
        return [*argv, "-"]

    @_classify_failure
    def parse(self, result: RunResult) -> AgentResult:
        raw = result.stdout
        if result.timed_out:
            return _failure(f"timed out after {result.duration_s:.0f}s", raw)

        text: str | None = None
        completed = False
        stream_error = ""
        for line_number, line in enumerate(raw.split("\n"), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return _failure(f"invalid JSON on output line {line_number}", raw)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                return _failure(f"invalid Codex event on output line {line_number}", raw)

            match event["type"]:
                case "turn.started":
                    text = None
                    completed = False
                    stream_error = ""
                case "item.completed" if not completed:
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") == "agent_message":
                        text = item.get("text")
                        if not isinstance(text, str):
                            return _failure(
                                f"invalid agent message on output line {line_number}", raw
                            )
                case "error":
                    message = event.get("message")
                    if isinstance(message, str):
                        stream_error = message
                case "turn.failed":
                    error = event.get("error")
                    message = error.get("message") if isinstance(error, dict) else None
                    detail = message if isinstance(message, str) and message else stream_error
                    return _failure(f"Codex turn failed: {detail}".rstrip(": "), raw)
                case "turn.completed":
                    completed = True

        detail = stream_error or result.stderr.strip()[-ERROR_TAIL_CHARS:]
        if result.exit_code != 0:
            return _failure(f"exit code {result.exit_code}: {detail}".rstrip(": "), raw)
        if not completed:
            return _failure(f"missing turn.completed event: {detail}".rstrip(": "), raw)
        if text is None:
            return _failure("completed Codex turn has no agent message", raw)
        return AgentResult(ok=True, text=text, raw=raw)

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        return self.parse(run_command(self.argv(prompt), repo, timeout, input=prompt))


class AgyAdapter:
    """Run Antigravity in headless accept-edits mode over ordinary pipes."""

    transport = "argv"

    def __init__(
        self,
        command: str = "agy",
        model: str | None = None,
        limit_patterns: tuple[str, ...] | None = None,
        read_only: bool = False,
    ) -> None:
        self.command = command
        self.model = model
        self.limit_patterns = limit_patterns
        self.read_only = read_only

    def argv(self, prompt: str) -> list[str]:
        mode = "plan" if self.read_only else "accept-edits"
        argv = [self.command, "-p", prompt, "--output-format", "json", "--mode", mode]
        if self.model:
            argv += ["--model", self.model]
        return argv

    @_classify_failure
    def parse(self, result: RunResult) -> AgentResult:
        raw = result.stdout
        if result.timed_out:
            return _failure(f"timed out after {result.duration_s:.0f}s", raw)
        stderr = result.stderr.strip()[-ERROR_TAIL_CHARS:]
        if not raw.strip():
            return _failure(f"no output (exit {result.exit_code}): {stderr}".rstrip(": "), raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return _failure("output is not valid JSON", raw)
        if not isinstance(data, dict):
            return _failure("output is not a JSON object", raw)

        status = data.get("status")
        error = data.get("error")
        detail = error.strip() if isinstance(error, str) and error.strip() else stderr
        if status == "ERROR":
            return _failure(detail or "agy reported an error", raw)
        if result.exit_code != 0:
            return _failure(f"exit code {result.exit_code}: {detail}".rstrip(": "), raw)
        if status != "SUCCESS":
            return _failure(f"unexpected agy status {status!r}", raw)
        response = data.get("response")
        if not isinstance(response, str):
            return _failure("agy SUCCESS output has no string response", raw)
        return AgentResult(ok=True, text=response, raw=raw)

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        return self.parse(_run_argv_adapter(self, prompt, repo, timeout))


class PiAdapter:
    """Run pi with JSONL events and pass its provider-qualified model through."""

    transport = "argv"

    def __init__(
        self,
        command: str = "pi",
        model: str | None = None,
        limit_patterns: tuple[str, ...] | None = None,
        read_only: bool = False,
    ) -> None:
        self.command = command
        self.model = model
        self.limit_patterns = limit_patterns
        self.read_only = read_only

    def argv(self, prompt: str) -> list[str]:
        argv = [self.command, "-p", prompt, "--mode", "json"]
        if self.model:
            argv += ["--model", self.model]
        return argv

    @_classify_failure
    def parse(self, result: RunResult) -> AgentResult:
        raw = result.stdout
        if result.timed_out:
            return _failure(f"timed out after {result.duration_s:.0f}s", raw)

        assistant: dict[str, Any] | None = None
        cost: float | None = None
        settled = False
        for line_number, line in enumerate(raw.split("\n"), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return _failure(f"invalid JSON on output line {line_number}", raw)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                return _failure(f"invalid pi event on output line {line_number}", raw)

            match event["type"]:
                case "agent_start":
                    settled = False
                    assistant = None
                case "message_end":
                    message = event.get("message")
                    if not isinstance(message, dict):
                        return _failure(f"invalid pi message on output line {line_number}", raw)
                    if message.get("role") == "assistant":
                        assistant = message
                        message_cost = _pi_message_cost(message)
                        if message_cost is not None:
                            cost = (cost or 0.0) + message_cost
                case "agent_settled":
                    if event.get("aborted") is True:
                        return _failure("pi run aborted", raw)
                    settled = True

        if assistant is not None:
            reason = assistant.get("stopReason")
            error = assistant.get("errorMessage")
            if reason != "stop" or (isinstance(error, str) and error):
                detail = error if isinstance(error, str) else ""
                return _failure(f"pi assistant stopped ({reason}): {detail}".rstrip(": "), raw)
        if result.exit_code != 0:
            detail = result.stderr.strip()[-ERROR_TAIL_CHARS:]
            return _failure(f"exit code {result.exit_code}: {detail}".rstrip(": "), raw)
        if not settled:
            return _failure("missing agent_settled event", raw)
        if assistant is None:
            return _failure("pi run has no final assistant message", raw)

        content = assistant.get("content")
        if not isinstance(content, list):
            return _failure("invalid pi assistant content", raw)
        parts = [
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        if not parts:
            return _failure("pi assistant message has no text", raw)
        return AgentResult(ok=True, text="\n".join(parts), cost=cost, raw=raw)

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        return self.parse(_run_argv_adapter(self, prompt, repo, timeout))


def _pi_message_cost(message: dict[str, Any]) -> float | None:
    usage = message.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    total = cost.get("total") if isinstance(cost, dict) else None
    if isinstance(total, int | float) and not isinstance(total, bool):
        return float(total)
    return None


def make_adapter(agent: AgentConfig, read_only: bool = False) -> CommandAdapter:
    """Build the adapter for ``agent``; ``read_only`` is for planning and review roles.

    pi has no read-only flag, so it ignores ``read_only`` (see docs/adapters.md).
    """
    adapters = {"claude": ClaudeAdapter, "codex": CodexAdapter, "agy": AgyAdapter, "pi": PiAdapter}
    return adapters[agent.adapter](agent.command, agent.model, agent.limit_patterns, read_only)


def _reported_tokens(adapter: str, raw: str) -> int | None:
    """Read terminal usage only; cache/reasoning breakdowns are not extra output."""
    try:
        records = [json.loads(raw)]
    except json.JSONDecodeError:
        records = []
        for line in raw.splitlines():
            with suppress(json.JSONDecodeError):
                records.append(json.loads(line))
    totals = []
    for event in records:
        if not isinstance(event, dict):
            continue
        if adapter == "CodexAdapter" and event.get("type") != "turn.completed":
            continue
        if adapter == "PiAdapter":
            if event.get("type") != "message_end":
                continue
            event = event.get("message", {})
            if not isinstance(event, dict) or event.get("role") != "assistant":
                continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        if adapter == "ClaudeAdapter":
            keys = (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        elif adapter == "CodexAdapter":
            keys = ("input_tokens", "output_tokens")
        elif adapter == "PiAdapter":
            keys = ("totalTokens",)
        else:
            keys = ("total_tokens",)
        values = [usage.get(key) for key in keys]
        if all(type(value) is int and value >= 0 for value in values):
            totals.append(sum(values))
    return sum(totals) if totals else None
