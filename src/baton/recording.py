"""Recordings of agent CLI runs: redacted JSON files used as test fixtures."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


@dataclass
class Recording:
    adapter: str
    agent: str
    argv: list[str]
    tty: bool
    exit_code: int | None
    timed_out: bool
    duration_s: float
    stdout: str
    stderr: str
    recorded_at: str
    cli_version: str


def redact(text: str, home: str | Path | None = None, workdir: str | Path | None = None) -> str:
    """Replace personal data: workdir -> <workdir>, home -> ~, emails, UUIDs.

    The workdir is replaced first because it usually lives below the home directory.
    """
    for value, token in ((workdir, "<workdir>"), (home if home is not None else Path.home(), "~")):
        raw = str(value) if value is not None else ""
        if raw and raw != "/":
            text = text.replace(raw, token)
    text = EMAIL_RE.sub("<email>", text)
    return UUID_RE.sub("<uuid>", text)


def redact_recording(
    rec: Recording, home: str | Path | None = None, workdir: str | Path | None = None
) -> Recording:
    def r(s: str) -> str:
        return redact(s, home, workdir)

    return Recording(
        adapter=rec.adapter,
        agent=rec.agent,
        argv=[r(a) for a in rec.argv],
        tty=rec.tty,
        exit_code=rec.exit_code,
        timed_out=rec.timed_out,
        duration_s=rec.duration_s,
        stdout=r(rec.stdout),
        stderr=r(rec.stderr),
        recorded_at=rec.recorded_at,
        cli_version=r(rec.cli_version),
    )


def save(
    rec: Recording,
    path: str | Path,
    home: str | Path | None = None,
    workdir: str | Path | None = None,
) -> None:
    """Redact ``rec`` and write it as JSON; nothing unredacted reaches the disk."""
    clean = redact_recording(rec, home, workdir)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(clean), indent=2, ensure_ascii=False) + "\n")


def load(path: str | Path) -> Recording:
    return Recording(**json.loads(Path(path).read_text()))
