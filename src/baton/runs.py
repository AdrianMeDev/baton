"""Shared append-only dispatch journal. Contains usage metadata only."""

import fcntl
import json
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
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            raise LimitStateError(f"Cannot read {self.path}: {exc}") from exc
        records = []
        starts = set()
        finished = set()
        for number, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
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
                            type(value) not in (int, float)
                            or value < 0
                            or not __import__("math").isfinite(value)
                        ):
                            raise ValueError(f"invalid {key}")
                    if row["tokens"] is not None and type(row["tokens"]) is not int:
                        raise ValueError("tokens must be integer")
                    finished.add(ident)
                records.append(row)
            except (ValueError, KeyError, TypeError) as exc:
                raise LimitStateError(f"Invalid journal {self.path}:{number}: {exc}") from exc
        return records

    @contextmanager
    def locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield
        except OSError as exc:
            raise LimitStateError(f"Cannot update {self.path}: {exc}") from exc

    def _append(self, row):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

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
