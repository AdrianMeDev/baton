"""Typed view of ``docs/ROADMAP.md`` in the format of the ``/plan-roadmap`` skill."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from baton.plan import MARKERS, Status, append_bullet

ROADMAP_PATHS = ("docs/ROADMAP.md", "ROADMAP.md")

_MARKER_OF: dict[Status, str] = {status: marker for marker, status in MARKERS.items()}
_MILESTONE_RE = re.compile(r"^### (M\d+) \[(.)\](?: (.*))?$")
_MILESTONE_LIKE_RE = re.compile(r"^### M\d+\b")
_BULLET_RE = re.compile(r"^- \*\*([^*]+?):\*\*\s*(.*)$")
_ID_RE = re.compile(r"M\d+")
_BULLET_FIELDS = {
    "goal": "goal",
    "includes": "includes",
    "done when": "done_when",
    "depends on": "depends_on",
    "size": "size",
}


class RoadmapError(ValueError):
    """The roadmap file is structurally invalid."""

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" if line is not None else message)


@dataclass(frozen=True)
class Milestone:
    id: str
    name: str
    status: Status
    goal: str = ""
    includes: str = ""
    done_when: str = ""
    depends_on: tuple[str, ...] = ()
    size: str | None = None  # "S" | "M" | "L"; the "~n commits" estimate is ignored
    start_line: int = 0  # 1-based: the ``### Mn`` heading
    end_line: int = 0  # 1-based, inclusive: last line before the next heading


@dataclass(frozen=True)
class Roadmap:
    title: str
    milestones: tuple[Milestone, ...]
    changelog: tuple[str, ...] = ()

    def get(self, milestone_id: str) -> Milestone | None:
        return next((m for m in self.milestones if m.id == milestone_id), None)


def _parse_depends(value: str, line: int) -> tuple[str, ...]:
    if value.strip() in ("", "-", "–", "none"):
        return ()
    ids = tuple(part.strip() for part in value.split(","))
    for item in ids:
        if not _ID_RE.fullmatch(item):
            raise RoadmapError(f"invalid dependency {item!r}", line)
    return ids


def _parse_milestone(
    number: str, marker: str, name: str, start: int, body: list[str], end: int
) -> Milestone:
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
        elif line.strip():
            current = None
    size = values.get("size", "")[:1].upper()
    return Milestone(
        id=number,
        name=name.strip(),
        status=MARKERS[marker],
        goal=values.get("goal", ""),
        includes=values.get("includes", ""),
        done_when=values.get("done_when", ""),
        depends_on=_parse_depends(values.get("depends_on", ""), start),
        size=size if size in ("S", "M", "L") else None,
        start_line=start,
        end_line=end,
    )


def parse_roadmap(text: str) -> Roadmap:
    lines = text.splitlines()
    at = next((i for i, ln in enumerate(lines) if ln.strip() == "## Milestones"), None)
    if at is None:
        raise RoadmapError("missing '## Milestones' section")
    end = next((i for i in range(at + 1, len(lines)) if lines[i].startswith("## ")), len(lines))

    title = next(
        (ln[2:].removeprefix("Roadmap:").strip() for ln in lines if ln.startswith("# ")), ""
    )

    headings: list[tuple[int, str, str, str]] = []
    for i in range(at + 1, end):
        if _MILESTONE_LIKE_RE.match(lines[i]):
            m = _MILESTONE_RE.match(lines[i])
            if not m or m[2] not in MARKERS:
                raise RoadmapError(f"invalid milestone heading {lines[i]!r}", i + 1)
            headings.append((i, m[1], m[2], m[3] or ""))
    if not headings:
        raise RoadmapError("'## Milestones' contains no milestones", at + 1)

    milestones: list[Milestone] = []
    for idx, (i, number, marker, name) in enumerate(headings):
        if any(m.id == number for m in milestones):
            raise RoadmapError(f"duplicate milestone {number}", i + 1)
        stop = headings[idx + 1][0] if idx + 1 < len(headings) else end
        milestones.append(_parse_milestone(number, marker, name, i + 1, lines[i + 1 : stop], stop))

    known = {m.id for m in milestones}
    for m in milestones:
        for dep in m.depends_on:
            if dep not in known:
                raise RoadmapError(f"{m.id} depends on unknown milestone {dep}", m.start_line)

    changelog: list[str] = []
    log_at = next((i for i, ln in enumerate(lines) if ln.strip() == "## Changelog"), None)
    if log_at is not None:
        for line in lines[log_at + 1 :]:
            if line.startswith("## "):
                break
            if line.strip():
                changelog.append(line.strip().removeprefix("- ").strip())

    return Roadmap(title=title, milestones=tuple(milestones), changelog=tuple(changelog))


def load_roadmap(path: Path) -> Roadmap:
    return parse_roadmap(path.read_text(encoding="utf-8"))


def find_roadmap(root: Path) -> Path | None:
    """``docs/ROADMAP.md``, else ``ROADMAP.md``, below ``root``."""
    return next((p for rel in ROADMAP_PATHS if (p := root / rel).is_file()), None)


def next_milestone(roadmap: Roadmap) -> Milestone | None:
    """First open milestone whose dependencies are all done."""
    done = {m.id for m in roadmap.milestones if m.status == "done"}
    return next(
        (m for m in roadmap.milestones if m.status == "open" and set(m.depends_on) <= done), None
    )


def set_milestone_status(text: str, milestone_id: str, status: Status) -> str:
    """Change one milestone heading in place; every other byte of ``text`` stays untouched."""
    milestone = parse_roadmap(text).get(milestone_id)
    if milestone is None:
        raise RoadmapError(f"no milestone {milestone_id}")
    lines = text.splitlines(keepends=True)
    old = lines[milestone.start_line - 1]
    eol = old[len(old.rstrip("\r\n")) :]
    lines[milestone.start_line - 1] = (
        f"### {milestone_id} [{_MARKER_OF[status]}] {milestone.name}".rstrip() + eol
    )
    return "".join(lines)


def append_changelog(text: str, line: str) -> str:
    """Append ``- <line>`` to the ``## Changelog`` section, creating it at the end if missing."""
    return append_bullet(text, "Changelog", line)


def save_roadmap(path: Path, text: str) -> None:
    """Write ``text`` back after checking that it still parses."""
    parse_roadmap(text)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def section_text(text: str, heading: str) -> str | None:
    """The raw ``## <heading>…`` section up to the next ``## `` heading; prefix match, any case.

    ``heading="Definition of done"`` also finds ``## Definition of done (every milestone)``.
    """
    lines = text.splitlines()
    wanted = f"## {heading}".lower()
    at = next((i for i, ln in enumerate(lines) if ln.strip().lower().startswith(wanted)), None)
    if at is None:
        return None
    end = next((i for i in range(at + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[at:end]).strip()


def milestone_text(text: str, milestone: Milestone) -> str:
    """The raw lines of ``milestone`` (heading to last body line) from the roadmap text."""
    lines = text.splitlines()
    return "\n".join(lines[milestone.start_line - 1 : milestone.end_line]).strip()
