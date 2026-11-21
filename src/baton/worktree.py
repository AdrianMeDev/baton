"""A dedicated git worktree per milestone branch, so the user's checkout stays untouched."""

from __future__ import annotations

import shutil
from pathlib import Path

from baton.git import (
    PLAN_FILENAME,
    GitError,
    branch_exists,
    milestone_branch,
    worktree_add,
    worktree_path_for,
    worktree_paths,
)
from baton.plan import Plan


def find_worktree(repo: Path, plan: Plan) -> Path | None:
    """The existing worktree of the plan's milestone branch, or ``None``."""
    path = worktree_path_for(repo, milestone_branch(plan))
    try:
        registered = {p.resolve() for p in worktree_paths(repo)}
    except GitError:
        return None
    return path if path.resolve() in registered and path.is_dir() else None


def ensure_worktree(repo: Path, plan: Plan) -> tuple[Path, bool]:
    """Locate or create the worktree for the plan's milestone branch.

    Returns ``(path, created)``. On creation the plan file is copied in; on reuse the
    worktree's copy stays authoritative. Raises ``GitError`` when git refuses, for example
    because the branch is checked out elsewhere.
    """
    branch = milestone_branch(plan)
    existing = find_worktree(repo, plan)
    if existing is not None:
        return existing, False
    path = worktree_path_for(repo, branch)
    if path.exists():
        raise GitError(f"{path} exists but is not a worktree of {repo}; remove it and retry")
    worktree_add(repo, path, branch, create=not branch_exists(repo, branch))
    shutil.copyfile(repo / PLAN_FILENAME, path / PLAN_FILENAME)
    return path, True


def mirror_plan(worktree: Path, repo: Path) -> None:
    """Copy the worktree's plan file back so ``baton status`` in ``repo`` stays accurate."""
    source = worktree / PLAN_FILENAME
    target = repo / PLAN_FILENAME
    if source.is_file() and (not target.is_file() or source.read_bytes() != target.read_bytes()):
        shutil.copyfile(source, target)
