"""Code audit report: the inventory of tracked files and its batches.

``docs/AUDIT.md`` lists every file tracked at the Base commit under its folder. Files that are
never sent to an agent are marked ``[-]`` with the reason; the scan marks the others ``[x]`` batch
by batch, so an interrupted audit resumes at the next open batch.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from baton.git import GitError

BATCH_CHARS = 60_000
MAX_FILE_CHARS = 60_000

Status = Literal["open", "done", "skipped"]

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


def _inventory_span(text: str) -> tuple[int, int]:
    """Character offsets of the ``## Inventory`` section (up to the next ``## `` heading)."""
    match = re.search(rf"^{re.escape(_INVENTORY_HEADING)}$", text, re.MULTILINE)
    if match is None:
        raise AuditError("audit file has no '## Inventory' section")
    nxt = re.search(r"^## ", text[match.end() :], re.MULTILINE)
    return match.start(), match.end() + nxt.start() if nxt else len(text)


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
