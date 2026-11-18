"""Thin wrappers around the ``git`` CLI (no dependencies)."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from baton.plan import Plan

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


def handoff_context(repo: Path) -> str:
    """Read staged and unstaged patches plus untracked paths without changing git state."""
    staged = _git(repo, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary")
    unstaged = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--binary")
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    # repr keeps paths with newlines or other unusual characters unambiguous.
    paths = "\n".join(repr(path) for path in untracked.split("\0") if path)
    return (
        f"Staged diff:\n{staged or '(none)'}\n"
        f"Unstaged diff:\n{unstaged or '(none)'}\n"
        f"Untracked paths (read these files in the working tree):\n{paths or '(none)'}\n"
    )


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def branch_slug(text: str) -> str:
    """Lowercase ``text``; umlauts become ``ae/oe/ue/ss``, other non-alphanumerics ``-``."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(_UMLAUTS))
    return slug.strip("-")


def milestone_branch(plan: Plan) -> str:
    """``baton/m<n>-<slug>`` for a milestone plan, ``baton/<slug of title>`` otherwise."""
    ref = plan.milestone
    if ref.id and ref.name:
        return f"baton/{ref.id.lower()}-{branch_slug(ref.name)}"
    return f"baton/{branch_slug(plan.title)}"


def current_branch(repo: Path) -> str | None:
    """The checked-out branch name, or ``None`` for a detached HEAD."""
    if not is_repo(repo):
        raise GitError(f"not a git repository: {repo}")
    return _git(repo, "branch", "--show-current").strip() or None


def branch_exists(repo: Path, branch: str) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    except GitError:
        return False
    return True


def switch_branch(repo: Path, branch: str, *, create: bool = False) -> None:
    """Switch to ``branch``; ``create`` branches it off the current HEAD first."""
    if create:
        _git(repo, "switch", "-c", branch)
    else:
        _git(repo, "switch", branch)


def commit_all(repo: Path, message: str, ignore: tuple[str, ...] = (PLAN_FILENAME,)) -> str:
    """Stage everything except ``ignore``, commit with ``message``, return the short hash.

    Hooks run normally. Raises ``GitError`` when there is nothing to commit or git rejects it.
    """
    if not is_repo(repo):
        raise GitError(f"not a git repository: {repo}")
    # No ":(exclude)" pathspec: git rejects it for a path that .gitignore lists.
    _git(repo, "add", "-A", "--", ".")
    # Unstage the ignored paths again, including ones the user staged earlier.
    for path in ignore:
        if path in _git(repo, "diff", "--cached", "--name-only", "-z", "--", path).split("\0"):
            _git(repo, "reset", "-q", "--", path)
    if not _git(repo, "diff", "--cached", "--name-only").strip():
        raise GitError(f"nothing to commit in {repo}")
    _git(repo, "commit", "-q", "-m", message)
    return head(repo)


def push(repo: Path, branch: str, remote: str = "origin") -> None:
    """``git push -u <remote> <branch>``."""
    _git(repo, "push", "-q", "-u", remote, branch)


def worktree_path_for(repo: Path, branch: str) -> Path:
    """``$XDG_STATE_HOME/baton/worktrees/<repo>-<hash>/<branch-slug>`` for ``repo``'s ``branch``."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    resolved = repo.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return base / "baton" / "worktrees" / f"{resolved.name}-{digest}" / branch_slug(branch)


def worktree_add(repo: Path, path: Path, branch: str, *, create: bool = False) -> None:
    """``git worktree add`` ``path`` on ``branch``; ``create`` branches it off the current HEAD."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if create:
        _git(repo, "worktree", "add", "-q", "-b", branch, str(path))
    else:
        _git(repo, "worktree", "add", "-q", str(path), branch)


def worktree_paths(repo: Path) -> list[Path]:
    """Paths of all worktrees registered for ``repo`` (including the main one)."""
    out = _git(repo, "worktree", "list", "--porcelain", "-z")
    return [Path(f[len("worktree ") :]) for f in out.split("\0") if f.startswith("worktree ")]
