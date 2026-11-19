"""Keep a tracked roadmap in sync with auto-commits: `[~]` first, `[x]` with the last step."""

from __future__ import annotations

from pathlib import Path

from baton.git import GitError, _git
from baton.plan import Plan, Step
from baton.roadmap import (
    RoadmapError,
    append_changelog,
    parse_roadmap,
    save_roadmap,
    set_milestone_status,
)


def sync_milestone(repo: Path, plan: Plan, step: Step, date: str) -> Path | None:
    """Update the tracked roadmap before ``step`` is committed; return its path if it changed.

    The milestone becomes ``[~]`` unless it already is, and ``[x]`` plus a Changelog line when
    ``step`` is the last one still open. Idempotent; local and standalone plans are left alone.
    """
    ref = plan.milestone
    if ref.tracking != "tracked" or ref.id is None or ref.roadmap is None:
        return None
    relative = Path(ref.roadmap)
    if relative.is_absolute() or ".." in relative.parts:
        raise RoadmapError("roadmap must be a relative path within the checkout")
    path = repo / relative
    if not path.resolve().is_relative_to(repo.resolve()):
        raise RoadmapError("roadmap escapes the checkout")
    if not path.is_file():
        raise RoadmapError(f"roadmap {ref.roadmap} not found in {repo}")
    try:
        tracked = _git(repo, "ls-files", "--stage", "-z", "--", relative.as_posix())
    except GitError as exc:
        raise RoadmapError(f"cannot validate roadmap tracking: {exc}") from exc
    entries = [entry for entry in tracked.split("\0") if entry]
    if len(entries) != 1 or entries[0].split("\t", 1)[-1] != relative.as_posix():
        raise RoadmapError(f"roadmap {ref.roadmap} is not tracked")
    if not entries[0].startswith(("100644 ", "100755 ")):
        raise RoadmapError("tracked roadmap must be a regular file")
    original = path.read_text(encoding="utf-8")
    milestone = parse_roadmap(original).get(ref.id)
    if milestone is None:
        raise RoadmapError(f"no milestone {ref.id} in {ref.roadmap}")

    last = all(s.status in ("done", "skipped") for s in plan.steps if s.number != step.number)
    text = original
    if last:
        if milestone.status != "done":
            text = set_milestone_status(text, ref.id, "done")
            text = append_changelog(text, f"{date}: {ref.id} done ({plan.title})")
    elif milestone.status == "open":
        text = set_milestone_status(text, ref.id, "pending")
    if text == original:
        return None
    save_roadmap(path, text)
    return path
