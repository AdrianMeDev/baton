"""Adapters that run an agent CLI headless to implement a plan step."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from baton.runner import RunResult, run_command

ERROR_TAIL_CHARS = 500


@dataclass(frozen=True)
class AgentResult:
    ok: bool
    text: str = ""
    error: str = ""
    cost: float | None = None
    raw: str = ""


class Adapter(Protocol):
    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult: ...


def _failure(message: str, raw: str = "") -> AgentResult:
    return AgentResult(ok=False, error=message, raw=raw)


class ClaudeAdapter:
    """``claude -p`` with edits allowed and git blocked (see docs/adapters.md)."""

    def __init__(self, command: str = "claude", model: str | None = None) -> None:
        self.command = command
        self.model = model

    def argv(self, prompt: str) -> list[str]:
        argv = [self.command, "-p", prompt, "--output-format", "json"]
        if self.model:
            argv += ["--model", self.model]
        argv += [
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Bash",
            "--disallowedTools",
            "Bash(git:*)",
        ]
        return argv

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
        return self.parse(run_command(self.argv(prompt), repo, timeout))


class CodexAdapter:
    """Run ``codex exec`` with workspace edits and JSONL events."""

    def __init__(self, command: str = "codex", model: str | None = None) -> None:
        self.command = command
        self.model = model

    def argv(self, prompt: str) -> list[str]:
        argv = [self.command, "exec", "--json", "--sandbox", "workspace-write"]
        if self.model:
            argv += ["-m", self.model]
        return [*argv, "--", prompt]

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
        return self.parse(run_command(self.argv(prompt), repo, timeout))
