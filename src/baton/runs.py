"""Shared append-only dispatch journal. Contains usage metadata only."""

import fcntl
import json
import math
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from baton.limits import LimitStateError, limit_state_path


class RunStore:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else limit_state_path().with_name("runs.jsonl")

    def read(self):
        return self._load()[0]

    def _load(self):
        """Parse the journal; also report whether a torn final line was ignored.

        A crash or full disk can leave a last line without its newline that is cut off. That
        line alone is ignored; a corrupt line anywhere else still raises with its line number.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [], False
        except (OSError, UnicodeError) as exc:
            raise LimitStateError(f"Cannot read {self.path}: {exc}") from exc
        lines = text.splitlines()
        unterminated = bool(text) and not text.endswith("\n")
        records = []
        starts = set()
        finished = set()
        for number, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
                if isinstance(row, dict) and row.get("event") == "stop":
                    continue  # auto-mode stop event; not an attempt
                if not isinstance(row, dict) or row.get("event") not in ("dispatch", "completion"):
                    raise ValueError("expected dispatch or completion")
                ident = row["id"]
                if not isinstance(ident, str) or not ident:
                    raise ValueError("invalid attempt id")
                if row["event"] == "dispatch":
                    if ident in starts or not isinstance(row["agent"], str) or not row["agent"]:
                        raise ValueError("invalid or duplicate dispatch")
                    timestamp = datetime.fromisoformat(row["at"])
                    if timestamp.utcoffset() is None:
                        raise ValueError("dispatch timestamp requires timezone")
                    starts.add(ident)
                else:
                    if ident not in starts or ident in finished:
                        raise ValueError("unmatched or duplicate completion")
                    if row["outcome"] not in ("ok", "failed", "limit"):
                        raise ValueError("invalid outcome")
                    for key in ("tokens", "duration_s"):
                        value = row[key]
                        if value is not None and (
                            type(value) not in (int, float) or value < 0 or not math.isfinite(value)
                        ):
                            raise ValueError(f"invalid {key}")
                    if row["tokens"] is not None and type(row["tokens"]) is not int:
                        raise ValueError("tokens must be integer")
                    finished.add(ident)
                records.append(row)
            except (ValueError, KeyError, TypeError) as exc:
                if unterminated and number == len(lines):
                    return records, True
                raise LimitStateError(f"Invalid journal {self.path}:{number}: {exc}") from exc
        return records, False

    @contextmanager
    def locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield
        except OSError as exc:
            raise LimitStateError(f"Cannot update {self.path}: {exc}") from exc

    def _repair_tail(self):
        """Make the file end on a line boundary: drop a torn last line or terminate a valid one."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return
        if not data or data.endswith(b"\n"):
            return
        if self._load()[1]:
            with self.path.open("r+b") as stream:
                stream.truncate(data.rfind(b"\n") + 1)
        else:
            with self.path.open("ab") as stream:
                stream.write(b"\n")

    def _append(self, row):
        self._repair_tail()
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def record_stop(self, reason, kind, *, now=None):
        """Journal why ``baton auto`` stopped; readers of attempts skip these rows."""
        with self.locked():
            self._append(
                dict(
                    event="stop",
                    reason=reason,
                    kind=kind,
                    at=(now or datetime.now(UTC)).isoformat(),
                )
            )

    def dispatch(self, agent, *, now=None):
        with self.locked():
            self.read()
            ident = uuid.uuid4().hex
            self._append(
                dict(
                    event="dispatch",
                    id=ident,
                    agent=agent,
                    at=(now or datetime.now(UTC)).isoformat(),
                )
            )
            return ident

    def select(self, agents, *, claim=False, now=None, weighted=None):
        """Select and optionally reserve capacity using one serialized snapshot."""
        from contextlib import nullcontext

        now = now or datetime.now(UTC)
        with self.locked() if claim else nullcontext():
            counts = daily_counts(self.read(), now)
            eligible = [
                a
                for a in agents
                if a.daily_run_budget is None or counts.get(a.name, 0) < a.daily_run_budget
            ]
            if not eligible:
                return None, None
            from baton.router import select_by_weight

            if weighted is None:
                weighted = any(a.weight is not None for a in agents)
            agent = select_by_weight(eligible, counts, weighted=weighted)
            ident = None
            if claim:
                ident = uuid.uuid4().hex
                self._append(dict(event="dispatch", id=ident, agent=agent.name, at=now.isoformat()))
            return agent, ident

    def complete(self, ident, result):
        with self.locked():
            self.read()
            self._append(
                dict(
                    event="completion",
                    id=ident,
                    outcome="ok" if result.ok else ("limit" if result.limit else "failed"),
                    duration_s=result.duration_s,
                    tokens=result.tokens,
                )
            )


def summarize(records):
    """Aggregate dispatch identities, keeping missing completion measurements visible."""
    completed = {r["id"]: r for r in records if r["event"] == "completion"}
    totals = {}
    for row in records:
        if row["event"] != "dispatch":
            continue
        stats = totals.setdefault(
            row["agent"],
            dict(attempts=0, duration_s=0.0, tokens=0, missing_duration=0, missing_tokens=0),
        )
        stats["attempts"] += 1
        end = completed.get(row["id"], {})
        for key, missing in [("duration_s", "missing_duration"), ("tokens", "missing_tokens")]:
            if end.get(key) is None:
                stats[missing] += 1
            else:
                stats[key] += end[key]
    return totals


def print_stats(out):
    totals = summarize(RunStore().read())
    if not totals:
        print("No recorded attempts.", file=out)
        return
    print("Agent  Attempts  Seconds  Tokens", file=out)
    for name, row in sorted(totals.items()):
        values = []
        for key, missing in [("duration_s", "missing_duration"), ("tokens", "missing_tokens")]:
            value = f"{row[key]:.3f}" if key == "duration_s" else str(row[key])
            if row[missing] == row["attempts"]:
                value = "unknown"
            elif row[missing]:
                value += f" (partial; {row[missing]} unknown)"
            values.append(value)
        print(f"{name}  {row['attempts']}  {values[0]}  {values[1]}", file=out)


def daily_counts(records, now):
    counts = {}
    for row in records:
        if (
            row["event"] == "dispatch"
            and datetime.fromisoformat(row["at"]).astimezone(UTC).date()
            == now.astimezone(UTC).date()
        ):
            counts[row["agent"]] = counts.get(row["agent"], 0) + 1
    return counts


def budget_reset_message():
    from datetime import timedelta

    reset = (datetime.now(UTC) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return f"Daily run budget exhausted; resets at {reset.isoformat()} (UTC). Resume after reset."
