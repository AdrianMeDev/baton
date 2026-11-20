"""``baton audit``: review every tracked file batch by batch and write ``docs/AUDIT.md``.

The inventory is taken once at the Base commit and every batch is read from that commit, so a
rerun after any stop continues at the next open batch. The reviewer runs read-only in an empty
temp dir and only ever sees file contents inside the prompt.
"""

from __future__ import annotations

import datetime
import os
import shlex
import sys
from pathlib import Path
from typing import TextIO

from baton.adapter import Adapter
from baton.audit import (
    BATCH_CHARS,
    AuditError,
    InventoryFile,
    build_inventory,
    next_batch,
    parse_audit_reply,
    parse_base,
    parse_findings,
    parse_inventory,
    read_file,
    record_batch,
    render_audit,
    set_file_status,
)
from baton.config import ConfigError, find_config_path, load_config
from baton.git import GitError, head, is_repo
from baton.git import _git as git_output
from baton.limits import LimitStateError, LimitStore
from baton.next import (
    DEFAULT_AGENT_TIMEOUT,
    EXIT_EXHAUSTED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_STOPPED,
)
from baton.prompt import audit_prompt, audit_retry_prompt
from baton.review import run_isolated_review
from baton.routed import RoutedAdapter, say
from baton.router import resolve_route

AUDIT_FILE = "docs/AUDIT.md"
ATTEMPTS = 2  # the first reply plus one retry with the validation error
_ERRORS = (AuditError, ConfigError, GitError, LimitStateError, OSError)


class _Stop(Exception):
    """End the run with ``code`` after ``message`` was reported."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


def _write(path: Path, text: str) -> None:
    """Replace ``path`` atomically so an interrupted run never leaves a half-written report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _check_repo(repo: Path) -> str:
    if not is_repo(repo):
        raise _Stop(EXIT_STOPPED, f"{repo} is not a git repository.")
    try:
        return head(repo, short=False)
    except GitError as exc:
        raise _Stop(EXIT_STOPPED, "The repository has no commits yet; nothing to audit.") from exc


def _load_or_create(repo: Path, out: TextIO, date: str, *, write: bool = True) -> str:
    """The audit text: the existing file, or a new inventory of HEAD."""
    path = repo / AUDIT_FILE
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        base = parse_base(text)
        try:
            base_full = git_output(repo, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
        except GitError as exc:
            raise _Stop(EXIT_FAILED, f"Base commit {base} of {AUDIT_FILE} does not exist.") from exc
        if base_full != head(repo, short=False):
            say(out, f"Warning: HEAD moved since the audit began; files are read at {base}.")
        return text
    base = head(repo)
    files = build_inventory(repo, base)
    text = render_audit(files, base=base, date=date, title=repo.resolve().name)
    if write:
        _write(path, text)
        skipped = sum(f.status == "skipped" for f in files)
        say(out, f"Created {AUDIT_FILE}: {len(files)} files at {base}, {skipped} skipped.")
    return text


def _open_batch(repo: Path, text: str) -> list[tuple[InventoryFile, str]]:
    """The next batch with its contents read at Base; unreadable files are marked skipped."""
    base = parse_base(text)
    files = parse_inventory(text)
    first = next_batch(files, 0)  # only to find the first folder with open files
    if not first:
        return []
    folder = first[0].folder
    contents: dict[str, str] = {}
    for f in files:
        if f.status == "open" and f.folder == folder:
            try:
                contents[f.path] = read_file(repo, base, f.path)
            except (AuditError, GitError):
                continue
    return [
        (f, contents[f.path])
        for f in next_batch(files, BATCH_CHARS, {p: len(c) for p, c in contents.items()})
        if f.path in contents
    ]


def _batches(repo: Path, text: str) -> list[list[tuple[InventoryFile, str]]]:
    """All remaining batches, for ``--dry-run`` (the scan itself fetches one at a time)."""
    batches = []
    while batch := _open_batch(repo, text):
        batches.append(batch)
        text = set_file_status(text, [f.path for f, _ in batch], "done", "dry run")
    return batches


def _ask(adapter: Adapter, prompt: str, paths: list[str], timeout: float, out: TextIO):
    current = prompt
    for attempt in range(1, ATTEMPTS + 1):
        result = run_isolated_review(adapter, current, timeout)
        if not result.ok:
            code = EXIT_EXHAUSTED if getattr(adapter, "exhausted", False) else EXIT_FAILED
            hint = " Rerun `baton audit` to continue." if code == EXIT_EXHAUSTED else ""
            raise _Stop(code, f"The agent failed: {result.error}.{hint}")
        try:
            return parse_audit_reply(result.text, paths)
        except AuditError as exc:
            say(out, f"Attempt {attempt}: the reply was rejected: {exc}")
            current = audit_retry_prompt(prompt, result.text, str(exc))
    raise _Stop(
        EXIT_FAILED,
        f"No valid reply after {ATTEMPTS} attempts; the batch stays open. Rerun to try again.",
    )


def run_audit(
    repo: Path,
    adapter: Adapter,
    out: TextIO,
    *,
    today: str | None = None,
    max_batches: int | None = None,
    timeout: float = DEFAULT_AGENT_TIMEOUT,
) -> int:
    """Scan every open batch of ``docs/AUDIT.md``, creating the file first if it is missing."""
    date = today or datetime.date.today().isoformat()
    try:
        _check_repo(repo)
        text = _load_or_create(repo, out, date)
        path = repo / AUDIT_FILE
        done = 0
        while batch := _open_batch(repo, text):
            if max_batches is not None and done >= max_batches:
                say(out, f"Stopped after {done} batches; rerun `baton audit` to continue.")
                return EXIT_OK
            paths = [f.path for f, _ in batch]
            say(out, f"Batch {done + 1}: {len(batch)} files ({paths[0]} ...)")
            findings = _ask(
                adapter, audit_prompt([(f.path, c) for f, c in batch]), paths, timeout, out
            )
            text = record_batch(text, [f for f, _ in batch], findings)
            _write(path, text)
            done += 1
        total = len(parse_findings(text))
        say(out, f"Scan complete: {len(parse_inventory(text))} files, {total} findings.")
        return EXIT_OK
    except _Stop as stop:
        say(out, str(stop))
        return stop.code
    except _ERRORS as exc:
        say(out, f"Error: {exc}")
        return EXIT_FAILED


def run_audit_command(
    repo: Path,
    *,
    config_path: str | None = None,
    agent_name: str | None = None,
    tier: str | None = None,
    timeout: float = DEFAULT_AGENT_TIMEOUT,
    max_batches: int | None = None,
    dry_run: bool = False,
    out: TextIO | None = None,
) -> int:
    """CLI entry: route ``review`` (tier M unless ``tier``) and scan the repository."""
    out = out or sys.stdout

    def select_adapter() -> RoutedAdapter:
        config = load_config(find_config_path(config_path))
        route = resolve_route(config, "review", tier=tier, agent_name=agent_name)
        return RoutedAdapter(route, LimitStore(), out, review=True)

    if not dry_run:
        try:
            adapter = select_adapter()
        except _ERRORS as exc:
            say(out, f"Error: {exc}")
            return EXIT_FAILED
        return run_audit(repo, adapter, out, max_batches=max_batches, timeout=timeout)

    try:
        _check_repo(repo)
        text = _load_or_create(repo, out, datetime.date.today().isoformat(), write=False)
        files = parse_inventory(text)
        batches = _batches(repo, text)
        skipped = sum(f.status == "skipped" for f in files)
        say(out, f"{len(files)} files, {skipped} skipped, {len(batches)} batches still open.")
        adapter = select_adapter()
        say(out, "Command (cwd = empty temp dir):")
        say(out, "  " + " ".join(shlex.quote(a) for a in adapter.argv("<prompt>")))
        if getattr(adapter, "transport", "argv") == "stdin":
            say(out, "  (prompt via stdin)")
        if batches:
            say(out, "Prompt of the first batch:")
            say(out, audit_prompt([(f.path, c) for f, c in batches[0]]))
        return EXIT_OK
    except _Stop as stop:
        say(out, str(stop))
        return stop.code
    except _ERRORS as exc:
        say(out, f"Error: {exc}")
        return EXIT_FAILED
