"""Milestone reports and pull request publication."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from baton.git import _git
from baton.plan import Plan, PlanError, parse_plan


@dataclass(frozen=True)
class Report:
    title: str
    body: str
    blockers: tuple[str, ...]


def milestone_report(repo: Path, text: str) -> Report:
    """Resolve completed steps and bind recorded reviews to exact commit trees."""
    plan: Plan = parse_plan(text)
    if any(s.status in ("open", "pending") for s in plan.steps):
        raise PlanError("Complete or skip every step before publishing")
    if not plan.base:
        raise PlanError("Plan requires a Base commit")
    base = _git(repo, "rev-parse", "--verify", f"{plan.base}^{{commit}}").strip()
    _git(repo, "merge-base", "--is-ancestor", base, "HEAD")
    commits = _git(repo, "rev-list", "--reverse", f"{base}..HEAD").splitlines()
    evidence = {}
    for entry in plan.log:
        if entry.startswith("baton-evidence: "):
            try:
                item = json.loads(entry.removeprefix("baton-evidence: "))
                evidence[item["step"]] = item
            except (ValueError, KeyError, TypeError) as exc:
                raise PlanError("Invalid baton review evidence") from exc
    goal = re.search(r"^## Goal\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    lines = ["## Goal", goal[1].strip() if goal else plan.title, "", "## Steps"]
    blockers = []
    covered = []
    for step in plan.steps:
        lines += [f"\n### {step.number}. {step.message}", f"Status: {step.status}"]
        if step.status == "skipped":
            continue
        if not step.commit:
            blockers.append(f"Step {step.number}: missing commit")
            continue
        commit = _git(repo, "rev-parse", "--verify", f"{step.commit}^{{commit}}").strip()
        if commit not in commits:
            raise PlanError(f"Step {step.number} commit is outside the milestone")
        covered.append(commit)
        lines.append(f"Commit: {commit}")
        item = evidence.get(step.number)
        valid = False
        if isinstance(item, dict) and item.get("version") == 1:
            tree = _git(repo, "rev-parse", f"{commit}^{{tree}}").strip()
            parent = _git(repo, "rev-parse", f"{commit}^").strip()
            valid = item.get("tree") == tree and item.get("parent") == parent
        if not valid:
            lines.append("Checks and review: unknown (missing or stale evidence)")
            blockers.append(f"Step {step.number}: missing or stale review evidence")
            continue
        verify = item.get("verify", {})
        lines.append(
            f"Check: `{verify.get('command', '')}` — "
            + ("passed" if verify.get("passed") else "unknown or failed")
        )
        if not verify.get("verified") or not verify.get("passed"):
            blockers.append(f"Step {step.number}: checks unknown or failed")
        findings = item.get("findings")
        if not item.get("reviewer") or not isinstance(findings, list):
            blockers.append(f"Step {step.number}: review unknown")
            lines.append("Independent review: unknown")
            continue
        lines.append(f"Independent reviewer: {item['reviewer'].get('name', 'unknown')}")
        for finding in findings:
            lines.append(
                f"- {finding['severity']}: {finding['message']} "
                f"({finding.get('path') or ''}:{finding.get('line') or ''})"
            )
            if finding["severity"] == "critical":
                blockers.append(f"Step {step.number}: critical finding")
        if not findings:
            lines.append("No findings.")
    if len(covered) != len(set(covered)) or set(covered) != set(commits):
        blockers.append("Milestone contains commits without distinct step reviews")
    lines += ["", "## Automatic merge", "\n".join(f"- {b}" for b in blockers) or "Eligible"]
    return Report(plan.title, "\n".join(lines) + "\n", tuple(blockers))
