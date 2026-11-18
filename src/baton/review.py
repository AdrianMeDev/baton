"""Independent review input and a strict, machine-readable findings format."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from baton.adapter import Adapter, AgentResult
from baton.config import AgentConfig
from baton.git import PLAN_FILENAME, _git

Severity = Literal["critical", "warning", "informational"]
SEVERITIES = ("critical", "warning", "informational")


class ReviewError(ValueError):
    """Review output is unreadable or review input cannot be collected."""


@dataclass(frozen=True)
class Finding:
    severity: Severity
    message: str
    path: str | None = None
    line: int | None = None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewError(f"Duplicate review field: {key}")
        result[key] = value
    return result


def parse_review(text: str) -> tuple[Finding, ...]:
    """Accept only a JSON object with a findings array; an empty array is a clean review."""
    try:
        data = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise ReviewError(f"Invalid review JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"findings"}:
        raise ReviewError("Review must be an object containing only 'findings'")
    if not isinstance(data["findings"], list):
        raise ReviewError("Review 'findings' must be an array")
    findings = []
    for item in data["findings"]:
        if not isinstance(item, dict) or set(item) - {"severity", "message", "path", "line"}:
            raise ReviewError("Each finding must contain severity, message and optional path/line")
        severity, message = item.get("severity"), item.get("message")
        path, line = item.get("path"), item.get("line")
        if severity not in SEVERITIES:
            raise ReviewError("Finding severity must be critical, warning or informational")
        if not isinstance(message, str) or not message.strip():
            raise ReviewError("Finding message must be a non-empty string")
        if path is not None and (not isinstance(path, str) or not path.strip()):
            raise ReviewError("Finding path must be a non-empty string or null")
        if line is not None and (type(line) is not int or line < 1 or path is None):
            raise ReviewError("Finding line must be a positive integer with a path, or null")
        findings.append(Finding(severity, message, path, line))
    return tuple(findings)


def review_context(repo: Path) -> str:
    """Collect the final HEAD-to-tree patch and complete untracked contents, read-only.

    Symlinks are represented by their target text, never followed. Binary new files
    are base64 encoded. The orchestration plan is excluded from candidate changes.
    """
    patch = _git(
        repo,
        "diff",
        "HEAD",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        "--",
        ".",
        f":(exclude){PLAN_FILENAME}",
    )
    paths = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    untracked = []
    for name in paths.split("\0"):
        if not name or name == PLAN_FILENAME:
            continue
        path = repo / name
        if path.is_symlink():
            untracked.append({"path": name, "symlink": os.readlink(path)})
            continue
        if not path.is_file():
            raise ReviewError(f"Cannot review non-regular file: {name!r}")
        raw = path.read_bytes()
        try:
            content, encoding = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            content, encoding = base64.b64encode(raw).decode("ascii"), "base64"
        untracked.append({"path": name, "content": content, "encoding": encoding})
    return json.dumps({"diff": patch, "untracked": untracked}, ensure_ascii=True)


@dataclass(frozen=True)
class ReviewResult:
    reviewer: AgentConfig
    findings: tuple[Finding, ...]

    def summary(self) -> str:
        counts = ", ".join(
            f"{severity}={sum(f.severity == severity for f in self.findings)}"
            for severity in SEVERITIES
        )
        return f"review {self.reviewer.name}: {counts}"


def run_isolated_review(adapter: Adapter, prompt: str, timeout: float) -> AgentResult:
    """Supply changes via the prompt and give the reviewer an empty disposable cwd."""
    with TemporaryDirectory(prefix="baton-review-") as directory:
        return adapter.run(prompt, Path(directory), timeout)


def candidate_tree(repo: Path) -> str:
    """Snapshot candidate contents using a disposable index, preserving the user's index."""
    import subprocess

    with TemporaryDirectory(prefix="baton-index-") as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))

        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True
            )
            if result.returncode:
                raise ReviewError(result.stderr.strip())
            return result.stdout.strip()

        git("read-tree", "HEAD")
        git("add", "-A", "--", ".")
        git("rm", "--cached", "--ignore-unmatch", "--", PLAN_FILENAME)
        return git("write-tree")


def evidence_entry(step: int, tree: str, parent: str, verify, review: ReviewResult | None) -> str:
    """One JSON log line; embedded newlines cannot become plan headings."""
    return "baton-evidence: " + json.dumps(
        {
            "version": 1,
            "step": step,
            "tree": tree,
            "parent": parent,
            "verify": asdict(verify),
            "reviewer": asdict(review.reviewer) if review else None,
            "findings": [asdict(f) for f in review.findings] if review else None,
        },
        ensure_ascii=True,
    )
