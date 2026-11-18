"""Usage-limit classification. Simulated provider diagnostics are covered by tests."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Deliberately exclude generic HTTP 429: it can mean a transient request-rate limit.
DEFAULT_LIMIT_PATTERNS = (
    r"\busage[_ -]limit(?:[_ -](?:reached|exceeded))?\b",
    r"\b(?:quota|credit balance)\b.{0,40}\b(?:exceeded|exhausted|depleted)\b",
    r"\binsufficient[_ -](?:quota|credits)\b",
    r"\b(?:hit|reached|exceeded) your (?:usage |weekly |daily )?limit\b",
)


@dataclass(frozen=True)
class UsageLimit:
    """A detected quota failure; an unknown reset time remains None."""

    reset_at: datetime | None = None


def classify_limit(
    diagnostic: str,
    fields: Any = None,
    *,
    patterns: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> UsageLimit | None:
    """Classify failure diagnostics and structured error fields, never assistant content.

    Custom patterns replace defaults. Absolute timestamps must include a timezone;
    relative retry/reset seconds are measured from the supplied clock.
    """
    now = now or datetime.now(UTC)
    messages = [diagnostic]
    resets: list[datetime] = []

    def visit(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in {"code", "type", "message", "errorMessage"} and isinstance(item, str):
                messages.append(item)
            elif key in {"error", "errors", "message"}:
                if isinstance(item, str):
                    messages.append(item)
                elif isinstance(item, list):
                    for entry in item:
                        visit(entry)
                else:
                    visit(item)
            if key in {"reset_at", "resets_at", "reset_time"}:
                parsed = _absolute_reset(item)
                if parsed is not None:
                    resets.append(parsed)
            elif key in {"retry_after", "retry_after_seconds", "reset_after_seconds"}:
                try:
                    seconds = float(item) if not isinstance(item, bool) else -1
                    if seconds >= 0:
                        resets.append(now + timedelta(seconds=seconds))
                except (ValueError, TypeError, OverflowError):
                    pass

    visit(fields)
    text = "\n".join(messages)
    if not any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in (DEFAULT_LIMIT_PATTERNS if patterns is None else patterns)
    ):
        return None
    if not resets:
        for timestamp in re.findall(r"\b\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2})", text):
            parsed = _absolute_reset(timestamp)
            if parsed is not None:
                resets.append(parsed)
        relative = re.search(
            r"(?:retry|resets?) (?:after|in)\s+(\d+(?:\.\d+)?)"
            r"\s*(s(?:econds?)?|m(?:inutes?)?|h(?:ours?)?)\b",
            text,
            re.IGNORECASE,
        )
        if relative:
            scale = {"s": 1, "m": 60, "h": 3600}[relative[2][0].lower()]
            with suppress(OverflowError):
                resets.append(now + timedelta(seconds=float(relative[1]) * scale))
    return UsageLimit(max(resets) if resets else None)


def _absolute_reset(value: Any) -> datetime | None:
    try:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        pass
    return None


class LimitStateError(Exception):
    """Cooldown state cannot safely be read or updated."""


def limit_state_path() -> Path:
    """Cooldowns are shared across repositories, keyed by configured agent name."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "baton" / "limits.json"


def parse_duration(value: str) -> timedelta:
    """Accept positive whole seconds, minutes or hours, such as 30m or 2h."""
    match = re.fullmatch(r"([0-9]+)([smh])", value)
    if match is None:
        raise ValueError("duration must be a positive number followed by s, m or h (e.g. 2h)")
    try:
        duration = timedelta(seconds=int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]])
    except (ValueError, OverflowError):
        raise ValueError("duration is too large") from None
    if duration <= timedelta(0):
        raise ValueError("duration must be greater than zero (e.g. 2h)")
    return duration


class LimitStore:
    """UTC deadlines with serialized read/modify/write and atomic replacement.

    The persistent sibling lock file is never replaced or deleted. Readers see
    either complete version of limits.json and do not create directories or files.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = limit_state_path() if path is None else path

    def read(self) -> dict[str, datetime]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as exc:
            raise LimitStateError(
                f"Cannot read {self.path}: {exc}. Check file permissions."
            ) from exc
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("expected an object mapping agent names to UTC deadlines")
            deadlines = {}
            for name, value in data.items():
                if not isinstance(name, str) or not name.strip() or not isinstance(value, str):
                    raise ValueError("expected non-empty agent names and timestamp strings")
                deadline = _absolute_reset(value)
                if deadline is None:
                    raise ValueError(f"invalid timezone-aware deadline for {name!r}")
                deadlines[name] = deadline
            return deadlines
        except (ValueError, TypeError) as exc:
            raise LimitStateError(
                f"Invalid cooldown state in {self.path}: {exc}. "
                "Repair or move this file before retrying; it was not overwritten."
            ) from exc

    def active(self, *, now: datetime | None = None) -> dict[str, datetime]:
        """Expired entries remain stored but are no longer active at their exact deadline."""
        now = datetime.now(UTC) if now is None else now
        return {name: deadline for name, deadline in self.read().items() if deadline > now}

    def set(self, agent: str, deadline: datetime) -> None:
        if not agent.strip():
            raise ValueError("agent name must not be empty")
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("cooldown deadline must include a timezone")
        deadline = deadline.astimezone(UTC)
        self._update(agent, deadline)

    def clear_if(self, agent: str, observed: datetime) -> bool:
        """Remove only the deadline observed before a probe, under the writer lock."""
        return self._update(agent, None, observed=observed)

    def _update(
        self, agent: str, deadline: datetime | None, *, observed: datetime | None = None
    ) -> bool:
        temp_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                deadlines = self.read()
                if deadline is None:
                    if deadlines.get(agent) != observed:
                        return False
                    deadlines.pop(agent, None)
                else:
                    deadlines[agent] = deadline
                payload = {name: dt.isoformat() for name, dt in deadlines.items()}
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=".limits-",
                    suffix=".tmp",
                    delete=False,
                ) as temp:
                    temp_path = Path(temp.name)
                    json.dump(payload, temp, indent=2, sort_keys=True)
                    temp.write("\n")
                    temp.flush()
                    os.fsync(temp.fileno())
                os.replace(temp_path, self.path)
                return True
        except OSError as exc:
            raise LimitStateError(
                f"Cannot update {self.path}: {exc}. "
                "Check state directory permissions and free space."
            ) from exc
        finally:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)
