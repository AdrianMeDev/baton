"""Typed view of ``.agent-plan.md`` in the format of the ``/plan-commits`` and ``/next`` skills."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

Status = Literal["open", "pending", "done", "skipped"]
Tracking = Literal["tracked", "local", "none"]

PLAN_FILENAME = ".agent-plan.md"

MARKERS: dict[str, Status] = {" ": "open", "~": "pending", "x": "done", "-": "skipped"}

_STEP_RE = re.compile(r"^### (\d+)\. \[(.)\](?: (.*))?$")
_STEP_LIKE_RE = re.compile(r"^### \d+\.")
_PENDING_RE = re.compile(r"^\(on ([0-9a-fA-F]+)\)\s*")
_DONE_RE = re.compile(r"^[–-] ([0-9a-fA-F]+)\s*")
_BULLET_RE = re.compile(r"^- \*\*([^*]+?):\*\*\s*(.*)$")
_MILESTONE_RE = re.compile(
    r"^(?P<id>M\d+)\s+[–-]\s+(?P<name>.*?)\s*\((?P<path>[^,()]+),\s*(?P<tracking>tracked|local)\)$"
)
_BULLET_FIELDS = {
    "why": "why",
    "changes": "changes",
    "done when": "done_when",
    "verify": "verify",
    "size": "size",
}


class PlanError(ValueError):
    """The plan file is structurally invalid."""

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" if line is not None else message)


@dataclass(frozen=True)
class MilestoneRef:
    """The ``Milestone:`` header line; ``tracking`` is ``none`` for standalone plans."""

    id: str | None
    name: str
    roadmap: str | None
    tracking: Tracking


@dataclass(frozen=True)
class Step:
    number: int
    status: Status
    message: str
    why: str = ""
    changes: str = ""
    done_when: str = ""
    verify: str = ""
    size: str | None = None
    commit: str | None = None  # hash after ``[x] –``
    pending_on: str | None = None  # hash after ``[~] (on …)``
    start_line: int = 0  # 1-based, inclusive: the ``### n.`` heading
    end_line: int = 0  # 1-based, inclusive: last line before the next heading


@dataclass(frozen=True)
class Plan:
    title: str
    created: str | None
    branch: str | None
    base: str | None
    milestone: MilestoneRef
    steps: tuple[Step, ...]
    log: tuple[str, ...] = field(default=())


def parse_milestone(value: str) -> MilestoneRef:
    value = value.strip()
    if value.lower() in ("", "none", "-", "–"):
        return MilestoneRef(None, "", None, "none")
    m = _MILESTONE_RE.match(value)
    if m:
        return MilestoneRef(m["id"], m["name"], m["path"].strip(), cast(Tracking, m["tracking"]))
    return MilestoneRef(None, value, None, "none")


def _parse_header(lines: list[str]) -> tuple[str, dict[str, str]]:
    title = ""
    fields: dict[str, str] = {}
    for line in lines:
        if line.startswith("# ") and not title:
            title = line[2:].removeprefix("Plan:").strip()
        elif line.startswith("## "):
            break
        else:
            for part in line.split(" · "):
                key, sep, value = part.partition(":")
                if sep and key.strip() in ("Created", "Branch", "Base", "Milestone"):
                    fields[key.strip().lower()] = value.strip()
    return title, fields


def _parse_step(number: int, marker: str, rest: str, start: int, body: list[str], end: int) -> Step:
    status = MARKERS[marker]
    commit = pending_on = None
    m = _PENDING_RE.match(rest) if status == "pending" else None
    if m:
        pending_on, rest = m[1], rest[m.end() :]
    m = _DONE_RE.match(rest) if status == "done" else None
    if m:
        commit, rest = m[1], rest[m.end() :]
    values: dict[str, str] = {}
    current: str | None = None
    for line in body:
        b = _BULLET_RE.match(line)
        if b:
            current = _BULLET_FIELDS.get(b[1].strip().lower())
            if current:
                values[current] = b[2].strip()
        elif current and line.startswith((" ", "\t")) and line.strip():
            values[current] += "\n" + line.strip()
        elif not line.strip():
            continue
        else:
            current = None
    size = values.pop("size", None)
    if size is not None:
        size = size.upper() if size.upper() in ("S", "M", "L") else None
    return Step(
        number=number,
        status=status,
        message=rest.strip(),
        size=size,
        commit=commit,
        pending_on=pending_on,
        start_line=start,
        end_line=end,
        **values,
    )


def parse_plan(text: str) -> Plan:
    lines = text.splitlines()
    steps_at = next((i for i, ln in enumerate(lines) if ln.strip() == "## Steps"), None)
    if steps_at is None:
        raise PlanError("missing '## Steps' section")

    title, fields = _parse_header(lines[:steps_at])

    section_end = next(
        (i for i in range(steps_at + 1, len(lines)) if lines[i].startswith("## ")), len(lines)
    )
    log_at = next(
        (i for i in range(steps_at + 1, len(lines)) if lines[i].strip() == "## Log"), None
    )

    headings: list[tuple[int, int, str, str]] = []  # (index, number, marker, rest)
    for i in range(steps_at + 1, section_end):
        line = lines[i]
        if _STEP_LIKE_RE.match(line):
            m = _STEP_RE.match(line)
            if not m or m[2] not in MARKERS:
                raise PlanError(f"invalid step heading {line!r}", i + 1)
            headings.append((i, int(m[1]), m[2], m[3] or ""))

    if not headings:
        raise PlanError("'## Steps' contains no steps", steps_at + 1)

    steps: list[Step] = []
    seen: set[int] = set()
    for idx, (i, number, marker, rest) in enumerate(headings):
        if number in seen:
            raise PlanError(f"duplicate step number {number}", i + 1)
        seen.add(number)
        stop = headings[idx + 1][0] if idx + 1 < len(headings) else section_end
        steps.append(_parse_step(number, marker, rest, i + 1, lines[i + 1 : stop], stop))

    log: list[str] = []
    if log_at is not None:
        for line in lines[log_at + 1 :]:
            if line.startswith("## "):
                break
            if line.strip():
                log.append(line.strip().removeprefix("- ").strip())

    return Plan(
        title=title,
        created=fields.get("created"),
        branch=fields.get("branch"),
        base=fields.get("base"),
        milestone=parse_milestone(fields.get("milestone", "none")),
        steps=tuple(steps),
        log=tuple(log),
    )


def load_plan(path: Path) -> Plan:
    return parse_plan(path.read_text(encoding="utf-8"))


_MARKER_OF: dict[Status, str] = {status: marker for marker, status in MARKERS.items()}
_TRANSITIONS: frozenset[tuple[Status, Status]] = frozenset(
    {
        ("open", "pending"),
        ("open", "skipped"),
        ("pending", "done"),
        ("pending", "skipped"),
        ("pending", "open"),  # the commit was discarded
    }
)


def first_open_step(plan: Plan) -> Step | None:
    return next((s for s in plan.steps if s.status == "open"), None)


def pending_step(plan: Plan) -> Step | None:
    return next((s for s in plan.steps if s.status == "pending"), None)


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def set_step_status(text: str, number: int, status: Status, hash: str | None = None) -> str:
    """Change one step heading in place; every other byte of ``text`` stays untouched.

    ``pending`` and ``done`` need the commit ``hash`` (HEAD at marking time / the new commit).
    """
    step = next((s for s in parse_plan(text).steps if s.number == number), None)
    if step is None:
        raise PlanError(f"no step {number}")
    if (step.status, status) not in _TRANSITIONS:
        raise PlanError(
            f"step {number}: cannot change status from {step.status} to {status}", step.start_line
        )
    if status in ("pending", "done") and not hash:
        raise PlanError(f"step {number}: a commit hash is required for status {status}")

    suffix = {"pending": f" (on {hash})", "done": f" – {hash}"}.get(status, "")
    lines = text.splitlines(keepends=True)
    old = lines[step.start_line - 1]
    eol = old[len(old.rstrip("\r\n")) :]
    lines[step.start_line - 1] = (
        f"### {number}. [{_MARKER_OF[status]}]{suffix} {step.message}".rstrip() + eol
    )
    return "".join(lines)


def append_log(text: str, line: str) -> str:
    """Append ``- <line>`` to the ``## Log`` section, creating it at the end if missing."""
    eol = _eol(text)
    entry = f"- {line.strip()}{eol}"
    lines = text.splitlines(keepends=True)
    log_at = next((i for i, ln in enumerate(lines) if ln.strip() == "## Log"), None)
    if log_at is None:
        sep = "" if not text or text.endswith(eol) else eol
        return f"{text}{sep}{eol}## Log{eol}{entry}"

    end = next((i for i in range(log_at + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    last = next((i for i in range(end - 1, log_at, -1) if lines[i].strip()), log_at)
    if not lines[last].endswith(("\n", "\r")):
        lines[last] += eol
    lines.insert(last + 1, entry)
    return "".join(lines)


def save_plan(path: Path, text: str) -> None:
    """Write ``text`` back after checking that it still parses."""
    parse_plan(text)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
