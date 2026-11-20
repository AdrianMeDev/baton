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
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from baton.adapter import Adapter
from baton.audit import (
    BATCH_CHARS,
    AuditError,
    InventoryFile,
    assemble_audit_plan,
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
    validate_audit_plan,
)
from baton.config import ConfigError, find_config_path, load_config
from baton.git import GitError, commit_all, dirty_paths, head, is_repo, is_tracked
from baton.git import _git as git_output
from baton.limits import LimitStateError, LimitStore
from baton.next import (
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_VERIFY_TIMEOUT,
    EXIT_EXHAUSTED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_STOPPED,
)
from baton.prompt import (
    audit_plan_prompt,
    audit_plan_retry_prompt,
    audit_prompt,
    audit_retry_prompt,
)
from baton.review import run_isolated_review
from baton.roadmap import section_text
from baton.roadmapper import _guarded, _Stop, gather_context
from baton.routed import RoutedAdapter, RoutedPlanner, say
from baton.router import resolve_route

AUDIT_FILE = "docs/AUDIT.md"
ATTEMPTS = 2  # the first reply plus one retry with the validation error
COMMIT_MESSAGE = "docs: add code audit"
_ERRORS = (AuditError, ConfigError, GitError, LimitStateError, OSError)


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


def _check_clean(repo: Path) -> None:
    """``--commit`` stages everything, so it needs a tree where only the report is new."""
    others = [p for p in dirty_paths(repo) if p != AUDIT_FILE]
    if others:
        listing = "".join(f"\n  {p}" for p in others)
        raise _Stop(
            EXIT_STOPPED,
            f"--commit needs a clean working tree, but there are uncommitted changes:{listing}",
        )


def _plan(repo: Path, text: str, adapter: Adapter, out: TextIO, date: str, timeout: float) -> str:
    """Fix milestones and suggestions for the findings; ``text`` is returned unchanged on a stop."""
    findings = parse_findings(text)
    say(out, f"Planning fixes for {len(findings)} findings ...")
    prompt = audit_plan_prompt(findings, gather_context(repo))
    current, errors = prompt, []
    for attempt in range(1, ATTEMPTS + 1):
        result = _guarded(adapter, current, repo, timeout)
        candidate = assemble_audit_plan(text, result.text, date)
        errors = validate_audit_plan(candidate)
        if not errors:
            return candidate
        say(out, f"Attempt {attempt}: the plan was rejected:")
        for error in errors:
            say(out, f"  {error}")
        current = audit_plan_retry_prompt(prompt, result.text, errors)
    raise _Stop(
        EXIT_FAILED,
        f"No valid fix plan after {ATTEMPTS} attempts. The scan is saved; rerun to plan again.",
    )


def _finish(repo: Path, out: TextIO, *, commit: bool) -> None:
    uncommitted = AUDIT_FILE in dirty_paths(repo)
    if commit and uncommitted:
        _check_clean(repo)
        say(out, f"Committed: {commit_all(repo, COMMIT_MESSAGE)} {COMMIT_MESSAGE}")
    elif uncommitted:
        say(out, "Review it, then commit it (`baton audit --fix` needs it committed):")
        say(out, f"  git add {AUDIT_FILE}")
        say(out, f"  git commit -m {shlex.quote(COMMIT_MESSAGE)}")
    say(out, "Then `baton audit --fix` works through the fix milestones.")


def run_audit(
    repo: Path,
    adapter: Adapter,
    out: TextIO,
    *,
    planner: Callable[[], Adapter],
    today: str | None = None,
    max_batches: int | None = None,
    timeout: float = DEFAULT_AGENT_TIMEOUT,
    commit: bool = False,
) -> int:
    """Scan every open batch of ``docs/AUDIT.md`` (created if missing), then plan the fixes.

    ``planner`` is only called when the scan is complete and the fix plan is missing or invalid.
    """
    date = today or datetime.date.today().isoformat()
    try:
        _check_repo(repo)
        if commit:
            _check_clean(repo)
        text = _load_or_create(repo, out, date)
        path = repo / AUDIT_FILE
        done = 0
        while batch := _open_batch(repo, text):
            if max_batches is not None and done >= max_batches:
                say(out, f"Stopped after {done} batches; rerun `baton audit` to continue.")
                return EXIT_OK
            paths = [f.path for f, _ in batch]
            say(out, f"Batch {done + 1}: {len(batch)} files ({paths[0]} ...)")
            prompt = audit_prompt([(f.path, c) for f, c in batch])
            findings = _ask(adapter, prompt, paths, timeout, out)
            text = record_batch(text, [f for f, _ in batch], findings)
            _write(path, text)
            done += 1
        files, found = len(parse_inventory(text)), len(parse_findings(text))
        say(out, f"Scan complete: {files} files, {found} findings.")
        if validate_audit_plan(text):
            text = _plan(repo, text, planner(), out, date, timeout)
            _write(path, text)
            say(out, f"Wrote the fix plan to {AUDIT_FILE}.")
        else:
            say(out, "The fix plan is already in place.")
        _finish(repo, out, commit=commit)
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
    commit: bool = False,
    dry_run: bool = False,
    out: TextIO | None = None,
) -> int:
    """CLI entry: scan with the ``review`` route (tier M unless ``tier``), plan with tier L."""
    out = out or sys.stdout

    def select_adapter() -> RoutedAdapter:
        config = load_config(find_config_path(config_path))
        route = resolve_route(config, "review", tier=tier, agent_name=agent_name)
        return RoutedAdapter(route, LimitStore(), out, review=True)

    def select_planner() -> RoutedPlanner:
        config = load_config(find_config_path(config_path))
        route = resolve_route(config, "plan_roadmap", tier="L")
        return RoutedPlanner(route, LimitStore(), out)

    if not dry_run:
        try:
            adapter = select_adapter()
        except _ERRORS as exc:
            say(out, f"Error: {exc}")
            return EXIT_FAILED
        return run_audit(
            repo,
            adapter,
            out,
            planner=select_planner,
            max_batches=max_batches,
            timeout=timeout,
            commit=commit,
        )

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


def run_audit_fix(
    repo: Path,
    *,
    config_path: str | None = None,
    max_steps: int | None = None,
    max_hours: float | None = None,
    merge: bool | None = None,
    agent_timeout: float = DEFAULT_AGENT_TIMEOUT,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
    out: TextIO | None = None,
    auto: Callable[..., int] | None = None,
) -> int:
    """``baton audit --fix``: work through the committed audit's milestones like ``baton auto``."""
    out = out or sys.stdout
    try:
        _check_repo(repo)
        path = repo / AUDIT_FILE
        if not path.is_file():
            raise _Stop(EXIT_STOPPED, f"No {AUDIT_FILE}; run `baton audit` first.")
        text = path.read_text(encoding="utf-8")
        open_files = sum(f.status == "open" for f in parse_inventory(text))
        if open_files:
            raise _Stop(
                EXIT_STOPPED,
                f"The scan is incomplete: {open_files} files are still open. "
                "Run `baton audit` to finish it.",
            )
        errors = validate_audit_plan(text)
        if errors:
            listing = "".join(f"\n  {e}" for e in errors)
            raise _Stop(
                EXIT_STOPPED,
                f"The fix plan is missing or invalid; run `baton audit` to plan it:{listing}",
            )
        if section_text(text, "Milestones") is None:
            say(
                out, f"Nothing to fix: no critical or warning findings. See {AUDIT_FILE} for ideas."
            )
            return EXIT_OK
        if not is_tracked(repo, AUDIT_FILE):
            raise _Stop(
                EXIT_STOPPED,
                f"{AUDIT_FILE} is not committed yet. Review and commit it first "
                f"(`git add {AUDIT_FILE} && git commit -m {shlex.quote(COMMIT_MESSAGE)}`).",
            )
        if AUDIT_FILE in dirty_paths(repo):
            raise _Stop(
                EXIT_STOPPED, f"{AUDIT_FILE} has uncommitted changes; commit or revert them first."
            )
    except _Stop as stop:
        say(out, str(stop))
        return stop.code
    except _ERRORS as exc:
        say(out, f"Error: {exc}")
        return EXIT_FAILED
    if auto is None:
        from baton.auto import run_auto_command

        auto = run_auto_command
    return auto(
        repo,
        config_path=config_path,
        max_steps=max_steps,
        max_hours=max_hours,
        merge=merge,
        agent_timeout=agent_timeout,
        verify_timeout=verify_timeout,
        roadmap=AUDIT_FILE,
        out=out,
    )
