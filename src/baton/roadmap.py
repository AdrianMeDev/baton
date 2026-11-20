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


def resolve_roadmap(root: Path, path: str | None = None) -> Path | None:
    """The roadmap file below ``root``: ``path`` if given, else ``find_roadmap``; None if absent.

    An explicit ``path`` must be relative and stay inside the checkout (no ``..``, no symlink
    that leaves it).
    """
    if path is None:
        return find_roadmap(root)
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RoadmapError(f"roadmap {path!r} must be a relative path within the checkout")
    target = root / relative
    if not target.resolve().is_relative_to(root.resolve()):
        raise RoadmapError(f"roadmap {path!r} escapes the checkout")
    return target if target.is_file() else None


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


QUESTION_MARKER = "BATON-QUESTION:"
MAX_QUESTIONS = 6
DEFAULT_ANSWER = "decide yourself and list it as a decision"
MAX_MILESTONE_COMMITS = 8
MIN_MILESTONE_COMMITS = 3

_QUESTION_RE = re.compile(rf"^[ \t]*{re.escape(QUESTION_MARKER)}[ \t]*(.+?)[ \t]*$", re.M)
_DEFAULT_SPLIT_RE = re.compile(r"\s*\|\s*default:\s*", re.I)
_COMMITS_HINT_RE = re.compile(r"~\s*(\d+)")


@dataclass(frozen=True)
class Question:
    text: str
    default: str


def parse_questions(reply: str) -> list[Question]:
    """The ``BATON-QUESTION: <question> | default: <answer>`` lines of a reply, at most six.

    A question without a default gets ``DEFAULT_ANSWER``.
    """
    questions: list[Question] = []
    for match in _QUESTION_RE.finditer(reply):
        text, _, default = _DEFAULT_SPLIT_RE.sub("\0", match[1], count=1).partition("\0")
        questions.append(Question(text.strip(), default.strip() or DEFAULT_ANSWER))
    return [q for q in questions if q.text][:MAX_QUESTIONS]


def assemble_roadmap(reply: str, date: str) -> str:
    """Turn the agent's reply into roadmap text with baton's ``Updated:`` date.

    Prose or a Markdown fence before ``# Roadmap:`` and everything from a closing fence on are
    dropped. Without a ``# Roadmap:`` heading the reply is returned stripped, so that validation
    reports it.
    """
    lines = reply.strip().splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("# Roadmap:")), None)
    if start is None:
        return reply.strip() + "\n"
    body = lines[start:]
    end = next((i for i, ln in enumerate(body) if ln.strip().startswith("```")), len(body))
    body = body[:end]
    while body and not body[-1].strip():
        body.pop()
    rest = body[1:]
    if rest and rest[0].startswith("Updated:"):
        rest = rest[1:]
    return "\n".join([body[0].rstrip(), f"Updated: {date}", *rest]) + "\n"


def validate_roadmap(text: str, *, fresh: bool = True) -> list[str]:
    """Readable reasons why ``text`` is not an acceptable roadmap; empty means valid.

    ``fresh`` (a roadmap that was just planned) also requires every milestone to be open.
    """
    try:
        roadmap = parse_roadmap(text)
    except RoadmapError as exc:
        return [f"roadmap does not parse: {exc}"]

    errors: list[str] = []
    if not any(ln.startswith("# Roadmap:") and ln[10:].strip() for ln in text.splitlines()):
        errors.append("missing '# Roadmap: <name>' heading")
    numbers = [int(m.id[1:]) for m in roadmap.milestones]
    first = numbers[0]
    if first not in (0, 1) or numbers != list(range(first, first + len(numbers))):
        errors.append(f"milestones must be numbered M0.. or M1.. in order, found {numbers}")
    for milestone in roadmap.milestones:
        where = milestone.id
        if fresh and milestone.status != "open":
            errors.append(f"{where}: must be open '[ ]', found {milestone.status}")
        if not milestone.name:
            errors.append(f"{where}: missing name")
        if not milestone.goal.strip():
            errors.append(f"{where}: missing '**Goal:**'")
        if not milestone.done_when.strip():
            errors.append(f"{where}: missing '**Done when:**'")
        if milestone.size is None:
            errors.append(f"{where}: '**Size:**' must start with S, M or L")
        hint = _COMMITS_HINT_RE.search(_size_line(text, milestone))
        if hint and not MIN_MILESTONE_COMMITS <= int(hint[1]) <= MAX_MILESTONE_COMMITS:
            errors.append(
                f"{where}: a milestone is {MIN_MILESTONE_COMMITS}-{MAX_MILESTONE_COMMITS} "
                f"commits, found ~{hint[1]}; split or merge it"
            )
    if section_text(text, "Definition of done") is None:
        errors.append("missing '## Definition of done' section")
    return errors


def _size_line(text: str, milestone: Milestone) -> str:
    for line in milestone_text(text, milestone).splitlines():
        match = _BULLET_RE.match(line)
        if match and match[1].strip().lower() == "size":
            return match[2]
    return ""
