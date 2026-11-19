"""GitHub CLI operations, always scoped to origin's repository."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from baton.git import _git


class ForgeError(RuntimeError):
    """GitHub publication or merge failed."""


def gh(repo: Path, *args: str):
    try:
        result = subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForgeError(f"GitHub CLI unavailable or timed out: {exc}") from exc
    if result.returncode:
        raise ForgeError(result.stderr.strip() or "GitHub CLI failed")
    return result.stdout.strip()


def origin_repository(repo: Path) -> str:
    remote = _git(repo, "remote", "get-url", "origin").strip()
    # gh accepts OWNER/REPO or a GitHub repository URL, including enterprise hosts.
    if remote.startswith("git@") and ":" in remote:
        host, path = remote[4:].split(":", 1)
        return f"https://{host}/{path.removesuffix('.git')}"
    if remote.startswith(("https://", "http://", "ssh://")):
        from urllib.parse import urlsplit

        parsed = urlsplit(remote)
        if not parsed.hostname:
            raise ForgeError("Origin must identify a GitHub repository")
        return f"https://{parsed.hostname}/{parsed.path.strip('/').removesuffix('.git')}"
    raise ForgeError("Origin must be a GitHub URL (HTTPS or SSH)")


def publish(repo: Path, target: str, base: str, branch: str, title: str, body: str) -> dict:
    raw = gh(
        repo,
        "pr",
        "list",
        "--repo",
        target,
        "--base",
        base,
        "--head",
        branch,
        "--state",
        "all",
        "--json",
        "number,url,state,headRefOid",
    )
    try:
        matches = json.loads(raw)
    except ValueError as exc:
        raise ForgeError("Invalid GitHub PR response") from exc
    if len(matches) > 1:
        raise ForgeError("Multiple matching PRs; resolve them on GitHub")
    if matches and matches[0]["state"] == "CLOSED":
        raise ForgeError(f"Closed unmerged PR: {matches[0]['url']}; reopen it before retrying")
    if matches and matches[0]["state"] == "MERGED":
        return matches[0]
    with TemporaryDirectory(prefix="baton-pr-") as directory:
        path = Path(directory) / "body.md"
        path.write_text(body, encoding="utf-8")
        if matches:
            gh(
                repo,
                "pr",
                "edit",
                str(matches[0]["number"]),
                "--repo",
                target,
                "--title",
                title,
                "--body-file",
                str(path),
            )
            return matches[0]
        url = gh(
            repo,
            "pr",
            "create",
            "--repo",
            target,
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            str(path),
        )
    return {"url": url, "state": "OPEN"}


def merge_when_ready(
    repo: Path,
    target: str,
    url: str,
    revision: str,
    *,
    timeout: float = 300,
    interval: float = 5,
    stop_requested: Callable[[], bool] | None = None,
) -> None:
    """Wait for checks and merge only the expected revision, then confirm the result."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        if stop_requested is not None and stop_requested():
            raise ForgeError("stop requested; PR remains open")
        data = json.loads(
            gh(
                repo,
                "pr",
                "view",
                url,
                "--repo",
                target,
                "--json",
                "state,headRefOid,statusCheckRollup",
            )
        )
        if data["headRefOid"] != revision:
            raise ForgeError("PR head changed; publish and review the new revision")
        if data["state"] == "MERGED":
            return
        if data["state"] != "OPEN":
            raise ForgeError("PR is no longer open")
        checks = data.get("statusCheckRollup") or []
        good = True
        for check in checks:
            if check.get("__typename") == "StatusContext":
                status = check.get("state")
                if status in ("ERROR", "FAILURE"):
                    raise ForgeError("CI failed")
                good = good and status == "SUCCESS"
            else:
                conclusion = check.get("conclusion")
                if conclusion in (
                    "FAILURE",
                    "TIMED_OUT",
                    "CANCELLED",
                    "ACTION_REQUIRED",
                    "STARTUP_FAILURE",
                    "STALE",
                ):
                    raise ForgeError("CI failed")
                good = (
                    good
                    and check.get("status") == "COMPLETED"
                    and conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED")
                )
        if checks and good:
            break
        if time.monotonic() >= deadline:
            raise ForgeError("CI wait timed out (pending or absent checks); PR remains open")
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    if stop_requested is not None and stop_requested():
        raise ForgeError("stop requested; PR remains open")
    gh(repo, "pr", "merge", url, "--repo", target, "--squash", "--match-head-commit", revision)
    confirmed = json.loads(
        gh(repo, "pr", "view", url, "--repo", target, "--json", "state,headRefOid")
    )
    if confirmed.get("state") != "MERGED" or confirmed.get("headRefOid") != revision:
        raise ForgeError("Merge was not confirmed for the expected revision")
