"""Code audit report: the inventory of tracked files and its batches.

``docs/AUDIT.md`` lists every file tracked at the Base commit under its folder. Files that are
never sent to an agent are marked ``[-]`` with the reason; the scan marks the others ``[x]`` batch
by batch, so an interrupted audit resumes at the next open batch.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from baton.git import GitError
from baton.review import SEVERITIES, Severity
from baton.roadmap import parse_roadmap, section_text, validate_roadmap

BATCH_CHARS = 60_000
MAX_FILE_CHARS = 60_000

Status = Literal["open", "done", "skipped"]
Category = Literal["security", "smell"]
CATEGORIES = ("security", "smell")

_MARKER: dict[Status, str] = {"open": " ", "done": "x", "skipped": "-"}
_STATUS = {marker: status for status, marker in _MARKER.items()}

_LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "go.sum",
    }
)
_SECRET_PATTERNS = (".env*", "*.pem", "*.key", "id_rsa*", "*.p12", "*.pfx")

_INVENTORY_HEADING = "## Inventory"
_PLAN_HEADINGS = ("## Milestones", "## Suggestions", "## Definition of done", "## Changelog")
_FINDING_ID_RE = re.compile(r"\bF(\d+)\b")
MILESTONE_PREFIX = "Audit:"
DEFINITION_OF_DONE = """\
- Builds, tests green, lint clean
- Every included finding is fixed, or explained as a false positive in the commit message
- Fixes add or extend tests where the finding was about untested or unsafe logic"""
_FINDINGS_HEADING = "## Findings"
_FINDING_RE = re.compile(
    r"^F(\d+) · (critical|warning|informational) · (security|smell) · "
    r"(.+?)(?::(\d+))? – (.+)$"
)
_TITLE_RE = re.compile(r"^# Audit: (.+)$", re.MULTILINE)
_BASE_RE = re.compile(r"^Updated: \S+ · Base: (\S+)$", re.MULTILINE)
_FOLDER_RE = re.compile(r"^### (\S.*/) \[[ ~x]\]$")
_FILE_RE = re.compile(r"^- \[([ x-])\] `([^`]+)`(?: – (.+))?$")


class AuditError(ValueError):
    """The audit file or the git data it is built from is not usable."""


@dataclass(frozen=True)
class InventoryFile:
    path: str
    status: Status = "open"
    note: str = ""

    @property
    def folder(self) -> str:
        parent = PurePosixPath(self.path).parent.as_posix()
        return "./" if parent == "." else f"{parent}/"


def _skip_reason(path: str, mode: str, kind: str) -> str | None:
    """Why a file is never sent to an agent, judged from its name and git object alone."""
    if mode == "120000":
        return "symlink"
    if kind != "blob":
        return "submodule"
    if "`" in path or "\n" in path or "\r" in path:
        return "unsafe file name"
    name = PurePosixPath(path).name
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _SECRET_PATTERNS):
        return "possible secret"
    if name in _LOCKFILE_NAMES or name.endswith(".lock"):
        return "lockfile"
    return None


def _git_bytes(repo: Path, args: list[str], stdin: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
        raise GitError(f"git {' '.join(args)} failed in {repo}: {detail}")
    return proc.stdout


def _read_blobs(repo: Path, shas: list[str]) -> list[bytes]:
    """Blob contents for ``shas`` (in order) from one ``git cat-file --batch`` call."""
    if not shas:
        return []
    out = _git_bytes(repo, ["cat-file", "--batch"], "".join(f"{sha}\n" for sha in shas).encode())
    blobs: list[bytes] = []
    pos = 0
    for sha in shas:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise AuditError(f"unexpected git object for {sha}")
        size = int(header[2])
        blobs.append(out[end + 1 : end + 1 + size])
        pos = end + 1 + size + 1
    return blobs


def _decode(blob: bytes) -> str | None:
    if b"\0" in blob:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def build_inventory(repo: Path, base: str) -> list[InventoryFile]:
    """Files tracked at ``base`` by folder then path; the never-sent ones are marked skipped."""
    raw = _git_bytes(repo, ["ls-tree", "-r", "-l", "-z", base])
    entries: list[tuple[str, str, str, str]] = []  # path, mode, kind, sha
    sizes: dict[str, int] = {}
    for record in raw.decode("utf-8", "surrogateescape").split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, kind, sha, size = meta.split()
        entries.append((path, mode, kind, sha))
        sizes[path] = int(size) if size.isdigit() else 0

    skipped: dict[str, str] = {}
    readable: list[tuple[str, str]] = []
    for path, mode, kind, sha in entries:
        reason = _skip_reason(path, mode, kind)
        if reason is None and sizes[path] > MAX_FILE_CHARS * 4:
            reason = "too large"
        if reason is None:
            readable.append((path, sha))
        else:
            skipped[path] = reason

    for (path, _), blob in zip(
        readable, _read_blobs(repo, [sha for _, sha in readable]), strict=True
    ):
        text = _decode(blob)
        if text is None:
            skipped[path] = "binary or not UTF-8"
        elif len(text) > MAX_FILE_CHARS:
            skipped[path] = "too large"

    files = [
        InventoryFile(path, "skipped", skipped[path]) if path in skipped else InventoryFile(path)
        for path, *_ in entries
    ]
    return sorted(files, key=lambda f: (f.folder, f.path))


def read_file(repo: Path, base: str, path: str) -> str:
    """The text of ``path`` at ``base``."""
    text = _decode(_git_bytes(repo, ["show", f"{base}:{path}"]))
    if text is None:
        raise AuditError(f"not a UTF-8 text file: {path}")
    return text


def _folder_marker(files: Iterable[InventoryFile]) -> str:
    statuses = {f.status for f in files}
    if "open" not in statuses:
        return "x"
    return "~" if "done" in statuses else " "


def _render_inventory(files: list[InventoryFile]) -> str:
    folders: dict[str, list[InventoryFile]] = {}
    for f in files:
        folders.setdefault(f.folder, []).append(f)
    lines = [_INVENTORY_HEADING, ""]
    for folder in sorted(folders):
        lines += [f"### {folder} [{_folder_marker(folders[folder])}]", ""]
        for f in sorted(folders[folder], key=lambda f: f.path):
            note = f" – {f.note}" if f.note else ""
            lines.append(f"- [{_MARKER[f.status]}] `{f.path}`{note}")
        lines.append("")
    return "\n".join(lines)


def render_audit(files: list[InventoryFile], *, base: str, date: str, title: str) -> str:
    """The initial audit file: header, scope and inventory."""
    open_count = sum(f.status == "open" for f in files)
    skipped_count = sum(f.status == "skipped" for f in files)
    return (
        f"# Audit: {title}\n\n"
        f"Updated: {date} · Base: {base}\n\n"
        "## Scope\n\n"
        f"All {len(files)} files tracked at `{base}`: {open_count} to review, "
        f"{skipped_count} skipped (symlinks, binary, oversized, lockfiles, possible secrets).\n\n"
        f"{_render_inventory(files)}"
    )


def parse_base(text: str) -> str:
    match = _BASE_RE.search(text)
    if match is None:
        raise AuditError("audit file has no 'Updated: <date> · Base: <commit>' line")
    return match.group(1)


def parse_title(text: str) -> str:
    match = _TITLE_RE.search(text)
    if match is None:
        raise AuditError("audit file has no '# Audit: <title>' heading")
    return match.group(1)


def _section_span(text: str, heading: str) -> tuple[int, int] | None:
    """Character offsets of a ``## `` section (up to the next ``## `` heading), if present."""
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    if match is None:
        return None
    nxt = re.search(r"^## ", text[match.end() :], re.MULTILINE)
    return match.start(), match.end() + nxt.start() if nxt else len(text)


def _inventory_span(text: str) -> tuple[int, int]:
    span = _section_span(text, _INVENTORY_HEADING)
    if span is None:
        raise AuditError("audit file has no '## Inventory' section")
    return span


def parse_inventory(text: str) -> list[InventoryFile]:
    start, end = _inventory_span(text)
    files: list[InventoryFile] = []
    folder: str | None = None
    for line in text[start:end].splitlines()[1:]:
        if not line.strip():
            continue
        if (m := _FOLDER_RE.match(line)) is not None:
            folder = m.group(1)
            continue
        m = _FILE_RE.match(line)
        if m is None or folder is None:
            raise AuditError(f"unreadable inventory line: {line!r}")
        entry = InventoryFile(m.group(2), _STATUS[m.group(1)], m.group(3) or "")
        if entry.folder != folder:
            raise AuditError(f"{entry.path} is listed under {folder}")
        if entry.status == "skipped" and not entry.note:
            raise AuditError(f"skipped file without a reason: {entry.path}")
        files.append(entry)
    return files


def set_file_status(text: str, paths: Iterable[str], status: Status, note: str = "") -> str:
    """``text`` with ``paths`` set to ``status`` (and ``note``); folder markers are recomputed."""
    wanted = set(paths)
    files = parse_inventory(text)
    missing = wanted - {f.path for f in files}
    if missing:
        raise AuditError(f"not in the inventory: {', '.join(sorted(missing))}")
    updated = [InventoryFile(f.path, status, note) if f.path in wanted else f for f in files]
    start, end = _inventory_span(text)
    tail = text[end:]
    return text[:start] + _render_inventory(updated) + (("\n" + tail) if tail else "")


def next_batch(
    files: list[InventoryFile], budget: int = BATCH_CHARS, sizes: Mapping[str, int] | None = None
) -> list[InventoryFile]:
    """Open files of the first folder with open files, up to ``budget`` characters.

    ``sizes`` maps a path to its length in characters; a file without an entry counts as 0. The
    first file is always taken, so a file larger than the budget still forms a batch of its own.
    """
    sizes = sizes or {}
    batch: list[InventoryFile] = []
    used = 0
    folder: str | None = None
    for f in files:
        if f.status != "open" or (folder is not None and f.folder != folder):
            continue
        size = sizes.get(f.path, 0)
        if batch and used + size > budget:
            break
        folder = f.folder
        batch.append(f)
        used += size
    return batch


@dataclass(frozen=True)
class AuditFinding:
    category: Category
    severity: Severity
    path: str
    message: str
    line: int | None = None
    id: int = 0

    def render(self) -> str:
        location = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"F{self.id} · {self.severity} · {self.category} · {location} – {self.message}"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AuditError(f"Duplicate audit field: {key}")
        result[key] = value
    return result


def parse_audit_reply(text: str, batch_paths: Iterable[str]) -> tuple[AuditFinding, ...]:
    """Accept only ``{"findings": [...]}`` about files of the batch; ids are assigned on record."""
    allowed = set(batch_paths)
    try:
        data = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise AuditError(f"Invalid audit JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"findings"}:
        raise AuditError("Audit reply must be an object containing only 'findings'")
    if not isinstance(data["findings"], list):
        raise AuditError("Audit 'findings' must be an array")
    findings = []
    for item in data["findings"]:
        if not isinstance(item, dict) or set(item) - {
            "severity",
            "category",
            "message",
            "path",
            "line",
        }:
            raise AuditError(
                "Each finding must contain severity, category, message, path and optional line"
            )
        severity, category = item.get("severity"), item.get("category")
        message, path, line = item.get("message"), item.get("path"), item.get("line")
        if severity not in SEVERITIES:
            raise AuditError("Finding severity must be critical, warning or informational")
        if category not in CATEGORIES:
            raise AuditError("Finding category must be security or smell")
        if not isinstance(message, str) or not message.strip():
            raise AuditError("Finding message must be a non-empty string")
        if not isinstance(path, str) or path not in allowed:
            raise AuditError(f"Finding path is not a file of this batch: {path!r}")
        if line is not None and (type(line) is not int or line < 1):
            raise AuditError("Finding line must be a positive integer or null")
        findings.append(AuditFinding(category, severity, path, " ".join(message.split()), line))
    return tuple(findings)


def parse_findings(text: str) -> list[AuditFinding]:
    """The ``F<n>`` lines of the ``## Findings`` section, in file order."""
    span = _section_span(text, _FINDINGS_HEADING)
    if span is None:
        return []
    findings: list[AuditFinding] = []
    for line in text[span[0] : span[1]].splitlines()[1:]:
        if not line.strip():
            continue
        m = _FINDING_RE.match(line)
        if m is None:
            raise AuditError(f"unreadable finding line: {line!r}")
        findings.append(
            AuditFinding(
                cast(Category, m.group(3)),
                cast(Severity, m.group(2)),
                m.group(4),
                m.group(6),
                int(m.group(5)) if m.group(5) else None,
                int(m.group(1)),
            )
        )
    return findings


def record_batch(text: str, batch: list[InventoryFile], findings: Iterable[AuditFinding]) -> str:
    """Append ``findings`` as ``F<n>`` lines and mark the batch files done with their counts."""
    findings = list(findings)
    paths = {f.path for f in batch}
    foreign = {f.path for f in findings} - paths
    if foreign:
        raise AuditError(f"findings for files outside the batch: {', '.join(sorted(foreign))}")
    next_id = max((f.id for f in parse_findings(text)), default=0) + 1
    numbered = [
        AuditFinding(f.category, f.severity, f.path, f.message, f.line, next_id + i)
        for i, f in enumerate(findings)
    ]
    for file in batch:
        count = sum(f.path == file.path for f in numbered)
        text = set_file_status(
            text, [file.path], "done", f"{count} finding{'' if count == 1 else 's'}"
        )
    if not numbered:
        return text
    lines = "".join(f"{f.render()}\n" for f in numbered)
    span = _section_span(text, _FINDINGS_HEADING)
    if span is None:
        return f"{text.rstrip(chr(10))}\n\n{_FINDINGS_HEADING}\n\n{lines}"
    head, section, tail = text[: span[0]], text[span[0] : span[1]].rstrip("\n"), text[span[1] :]
    if section == _FINDINGS_HEADING:
        section += "\n"
    return f"{head}{section}\n{lines}" + (f"\n{tail}" if tail else "")


def _reply_section(reply: str, heading: str) -> str:
    """The body of ``heading`` in an agent reply, without surrounding prose or fences."""
    lines = reply.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == heading), None)
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## ") or line.strip().startswith("```"):
            break
        body.append(line.rstrip())
    return "\n".join(body).strip()


def _drop_section(text: str, heading: str) -> str:
    span = _section_span(text, heading)
    if span is None:
        return text
    return text[: span[0]].rstrip("\n") + "\n" + (f"\n{text[span[1] :]}" if text[span[1] :] else "")


def assemble_audit_plan(text: str, reply: str, date: str) -> str:
    """``text`` plus the planner's Milestones and Suggestions, Definition of done and Changelog.

    Sections from an earlier planning run are replaced; the Changelog keeps its old lines.
    """
    changelog = section_text(text, "Changelog") or ""
    old_log = [ln for ln in changelog.splitlines()[1:] if ln.strip()]
    for heading in _PLAN_HEADINGS:
        text = _drop_section(text, heading)
    text = re.sub(r"^Updated: \S+", f"Updated: {date}", text, count=1, flags=re.MULTILINE)
    sections = [
        ("## Milestones", _reply_section(reply, "## Milestones")),
        ("## Suggestions", _reply_section(reply, "## Suggestions")),
        ("## Definition of done", DEFINITION_OF_DONE),
        ("## Changelog", "\n".join([*old_log, f"- {date}: audit planned"])),
    ]
    out = text.rstrip("\n") + "\n"
    for heading, body in sections:
        if body:
            out += f"\n{heading}\n\n{body}\n"
    return out


def validate_audit_plan(text: str) -> list[str]:
    """Readable reasons why the planned audit is not acceptable; empty means valid."""
    errors: list[str] = []
    try:
        findings = parse_findings(text)
    except AuditError as exc:
        return [f"findings do not parse: {exc}"]
    required = {f.id for f in findings if f.severity in ("critical", "warning")}
    known = {f.id for f in findings}

    covered: dict[int, list[str]] = {}
    if section_text(text, "Milestones") is None:
        if required:
            errors.append("missing '## Milestones' section")
    else:
        errors += _milestone_errors(text, covered)
    for number, where in sorted(covered.items()):
        if number not in known:
            errors.append(f"{', '.join(where)}: unknown finding F{number}")
        elif len(where) > 1:
            errors.append(f"finding F{number} is covered by {', '.join(where)}; use exactly one")
    for number in sorted(required - set(covered)):
        errors.append(f"finding F{number} is not covered by any milestone")

    suggestions = section_text(text, "Suggestions")
    if suggestions is None or not suggestions.splitlines()[1:]:
        errors.append("missing '## Suggestions' section with at least one suggestion")
    return errors


def _milestone_errors(text: str, covered: dict[int, list[str]]) -> list[str]:
    """Roadmap rules plus audit rules for the milestones; fills ``covered`` with finding ids."""
    as_roadmap = re.sub(r"^# Audit:", "# Roadmap:", text, count=1, flags=re.MULTILINE)
    errors = validate_roadmap(as_roadmap)
    if any(error.startswith("roadmap does not parse") for error in errors):
        return errors
    first = parse_roadmap(text).milestones[0]
    if first.id != "M1":
        errors.append(f"milestones must be numbered M1.. in order, first is {first.id}")
    for milestone in parse_roadmap(text).milestones:
        if not milestone.name.startswith(MILESTONE_PREFIX):
            errors.append(f"{milestone.id}: the name must start with '{MILESTONE_PREFIX}'")
        ids = {int(n) for n in _FINDING_ID_RE.findall(milestone.includes)}
        if not ids:
            errors.append(f"{milestone.id}: '**Includes:**' lists no finding ids (F<n>)")
        for number in ids:
            covered.setdefault(number, []).append(milestone.id)
    return errors
