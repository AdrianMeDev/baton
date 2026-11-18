"""Usage-limit classification. Simulated provider diagnostics are covered by tests."""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
