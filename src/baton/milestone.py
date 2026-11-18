"""Keep a tracked roadmap in sync with auto-commits: `[~]` first, `[x]` with the last step."""

from __future__ import annotations

from pathlib import Path

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
    path = repo / ref.roadmap
    if not path.is_file():
        raise RoadmapError(f"roadmap {ref.roadmap} not found in {repo}")
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
