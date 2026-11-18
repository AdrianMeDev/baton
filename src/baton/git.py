"""Thin wrappers around the ``git`` CLI (no dependencies, read-only)."""

from __future__ import annotations

import subprocess
from pathlib import Path

PLAN_FILENAME = ".agent-plan.md"


class GitError(RuntimeError):
    """A git command failed or ``repo`` is not a git repository."""


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except NotADirectoryError as exc:
        raise GitError(f"not a directory: {repo}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise GitError(f"git {' '.join(args)} failed in {repo}: {detail}")
    return proc.stdout


def is_repo(repo: Path) -> bool:
    try:
        return _git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False


def head(repo: Path, *, short: bool = True) -> str:
    """The commit hash of HEAD (abbreviated by default)."""
    if not is_repo(repo):
        raise GitError(f"not a git repository: {repo}")
    args = ["rev-parse", "--verify", "--quiet"]
    if short:
        args.append("--short")
    try:
        return _git(repo, *args, "HEAD").strip()
    except GitError as exc:
        raise GitError(f"repository has no commits yet: {repo}") from exc


def dirty_paths(repo: Path, ignore: tuple[str, ...] = (PLAN_FILENAME,)) -> list[str]:
    """Paths with uncommitted changes (staged, unstaged, untracked), minus ``ignore``.

    Paths are relative to the repository root, as printed by ``git status``.
    """
    if not is_repo(repo):
        raise GitError(f"not a git repository: {repo}")
    out = _git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
    entries = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":  # the next entry is the rename/copy source
            i += 1
    return [p for p in paths if p not in ignore]
