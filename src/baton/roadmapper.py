"""``baton roadmap``: turn an idea into ``docs/ROADMAP.md`` with a headless planner.

Question phase (the agent asks up to six questions with defaults, baton asks them in the terminal)
-> roadmap phase -> validate with one retry -> write the file, optionally commit it.
The agent only reads: any change it makes to HEAD or the working tree is rejected.
"""

from __future__ import annotations

import contextlib
import datetime
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from baton.adapter import Adapter, AgentResult
from baton.config import ConfigError, find_config_path, load_config
from baton.git import GitError, commit_all, dirty_paths, handoff_context, head, is_repo
from baton.git import _git as git_output
from baton.limits import LimitStateError, LimitStore
from baton.next import (
    DEFAULT_AGENT_TIMEOUT,
    EXIT_EXHAUSTED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_STOPPED,
)
from baton.prompt import roadmap_prompt, roadmap_questions_prompt, roadmap_retry_prompt
from baton.roadmap import (
    Question,
    RoadmapError,
    assemble_roadmap,
    find_roadmap,
    parse_questions,
    parse_roadmap,
    validate_roadmap,
)
from baton.routed import RoutedPlanner, say
from baton.router import resolve_route

ATTEMPTS = 2  # the first roadmap plus one retry with the validation errors
ROADMAP_FILE = "docs/ROADMAP.md"
COMMIT_MESSAGE = "docs: add roadmap"
CONTEXT_CHARS = 12000
_FILE_CHARS = 3000
_MANIFESTS = (
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "composer.json",
)
_ERRORS = (ConfigError, GitError, LimitStateError, RoadmapError, OSError)


class _Stop(Exception):
    """End the run with ``code`` after ``message`` was reported."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


def gather_context(repo: Path) -> str:
    """Read-only project context for the prompts: README, docs, manifests, files, history."""
    parts: list[str] = []

    def add(title: str, body: str) -> None:
        if body.strip():
            parts.append(f"## {title}\n{body.strip()}")

    for name in ("README.md", "README", "CLAUDE.md", "AGENTS.md", *_MANIFESTS):
        path = repo / name
        if path.is_file():
            add(name, path.read_text(encoding="utf-8", errors="replace")[:_FILE_CHARS])
    docs = repo / "docs"
    if docs.is_dir():
        add("docs/", "\n".join(sorted(p.name for p in docs.iterdir() if p.is_file())[:40]))
    files = git_output(repo, "ls-files").splitlines()
    add("Files (first 80)", "\n".join(files[:80]))
    with contextlib.suppress(GitError):  # no commits yet
        add("git log", git_output(repo, "log", "--oneline", "-15"))
    return "\n\n".join(parts)[:CONTEXT_CHARS]


def _head(repo: Path) -> str:
    try:
        return head(repo)
    except GitError:
        return "(no commits)"  # a greenfield repository


def _guarded(adapter: Adapter, prompt: str, repo: Path, timeout: float) -> AgentResult:
    """Run the planner and reject any change it made to HEAD or the working tree."""
    before_head = _head(repo)
    before_tree = handoff_context(repo)
    result = adapter.run(prompt, repo, timeout)
    after = _head(repo)
    if after != before_head:
        raise _Stop(EXIT_FAILED, f"HEAD moved from {before_head} to {after}: the agent committed.")
    if handoff_context(repo) != before_tree:
        listing = "".join(f"\n  {p}" for p in dirty_paths(repo))
        raise _Stop(EXIT_FAILED, f"The agent changed the working tree; review it:{listing}")
    if not result.ok:
        code = EXIT_EXHAUSTED if getattr(adapter, "exhausted", False) else EXIT_FAILED
        raise _Stop(code, f"The agent failed: {result.error}")
    return result


def _ask_questions(
    questions: list[Question], ask: Callable[[Question], str] | None, out: TextIO
) -> list[tuple[Question, str]]:
    answers: list[tuple[Question, str]] = []
    for question in questions:
        answer = ask(question) if ask is not None else ""
        answer = answer.strip() or question.default
        if ask is None:
            say(out, f"Q: {question.text}\n   -> {answer} (default)")
        answers.append((question, answer))
    return answers


def stdin_asker(out: TextIO, stdin: TextIO) -> Callable[[Question], str]:
    def ask(question: Question) -> str:
        out.write(f"{question.text}\n  [{question.default}] > ")
        out.flush()
        return stdin.readline()

    return ask


def run_roadmap(
    repo: Path,
    adapter: Adapter,
    out: TextIO,
    *,
    idea: str,
    ask: Callable[[Question], str] | None = None,
    commit: bool = False,
    today: str | None = None,
    timeout: float = DEFAULT_AGENT_TIMEOUT,
) -> int:
    """Write ``docs/ROADMAP.md`` for ``idea``; ``ask`` None takes the suggested defaults."""
    return _run_roadmap(
        repo, lambda: adapter, out, idea=idea, ask=ask, commit=commit, today=today, timeout=timeout
    )


def _check_start(repo: Path, idea: str, commit: bool) -> None:
    if not idea.strip():
        raise _Stop(EXIT_STOPPED, "No idea given: pass it as text or with --from FILE.")
    if not is_repo(repo):
        raise _Stop(EXIT_STOPPED, f"{repo} is not a git repository.")
    existing = find_roadmap(repo)
    if existing is not None:
        rel = existing.relative_to(repo).as_posix()
        raise _Stop(
            EXIT_STOPPED,
            f"{rel} already exists; baton roadmap never overwrites it. "
            "Update it with the /plan-roadmap skill or delete it first.",
        )
    dirty = dirty_paths(repo)
    if commit and dirty:
        listing = "".join(f"\n  {p}" for p in dirty)
        raise _Stop(
            EXIT_STOPPED,
            f"--commit needs a clean working tree, but there are uncommitted changes:{listing}",
        )


def _run_roadmap(
    repo: Path,
    select_adapter: Callable[[], Adapter],
    out: TextIO,
    *,
    idea: str,
    ask: Callable[[Question], str] | None,
    commit: bool,
    today: str | None,
    timeout: float,
) -> int:
    date = today or datetime.date.today().isoformat()
    try:
        _check_start(repo, idea, commit)
        adapter = select_adapter()
        context = gather_context(repo)

        say(out, "Asking the planner which questions are open ...")
        reply = _guarded(adapter, roadmap_questions_prompt(idea, context), repo, timeout)
        answers = _ask_questions(parse_questions(reply.text), ask, out)

        say(out, "Writing the roadmap ...")
        prompt = roadmap_prompt(idea, answers, context)
        current, errors, text = prompt, [], ""
        for attempt in range(1, ATTEMPTS + 1):
            result = _guarded(adapter, current, repo, timeout)
            text = assemble_roadmap(result.text, date)
            errors = validate_roadmap(text)
            if not errors:
                break
            say(out, f"Attempt {attempt}: the roadmap was rejected:")
            for error in errors:
                say(out, f"  {error}")
            current = roadmap_retry_prompt(prompt, result.text, errors)
        if errors:
            raise _Stop(
                EXIT_FAILED, f"No valid roadmap after {ATTEMPTS} attempts. Nothing written."
            )

        target = repo / ROADMAP_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as f:
            f.write(text)
        roadmap = parse_roadmap(text)
        say(out, f"Wrote {ROADMAP_FILE}: {roadmap.title} ({len(roadmap.milestones)} milestones)")
        for milestone in roadmap.milestones:
            say(out, f"  {milestone.id} {milestone.name} [{milestone.size}]")
        if commit:
            say(out, f"Committed: {commit_all(repo, COMMIT_MESSAGE)} {COMMIT_MESSAGE}")
            say(out, "Next: `baton auto` works through the roadmap (or `baton plan` for M0/M1).")
        else:
            say(out, "Review it, then commit it (`baton plan` needs a clean working tree):")
            say(out, f"  git add {ROADMAP_FILE}")
            say(out, f"  git commit -m {shlex.quote(COMMIT_MESSAGE)}")
            say(
                out,
                "Then run `baton auto`. Or rerun with --commit to commit it yourself next time.",
            )
        return EXIT_OK
    except _Stop as stop:
        say(out, str(stop))
        return stop.code
    except FileExistsError:
        say(out, f"{ROADMAP_FILE} appeared while the agent was working; not overwriting it.")
        return EXIT_FAILED
    except _ERRORS as exc:
        say(out, f"Error: {exc}")
        return EXIT_FAILED


def run_roadmap_command(
    repo: Path,
    *,
    idea: str | None = None,
    from_file: str | None = None,
    config_path: str | None = None,
    agent_name: str | None = None,
    tier: str | None = None,
    timeout: float = DEFAULT_AGENT_TIMEOUT,
    yes: bool = False,
    commit: bool = False,
    dry_run: bool = False,
    out: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    """CLI entry: route ``plan_roadmap`` (tier L unless ``tier``) and write the roadmap."""
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    try:
        if from_file is not None:
            if idea:
                raise _Stop(EXIT_STOPPED, "Pass the idea as text or with --from FILE, not both.")
            idea = Path(from_file).read_text(encoding="utf-8")
    except _Stop as stop:
        say(out, str(stop))
        return stop.code
    except OSError as exc:
        say(out, f"Error: {exc}")
        return EXIT_FAILED
    idea = idea or ""

    def select_adapter() -> RoutedPlanner:
        config = load_config(find_config_path(config_path))
        route = resolve_route(config, "plan_roadmap", tier=tier or "L", agent_name=agent_name)
        return RoutedPlanner(route, LimitStore(), out)

    if dry_run:
        try:
            _check_start(repo, idea, commit=False)
            adapter = select_adapter()
            say(out, "Command (cwd = repo root):")
            say(out, "  " + " ".join(shlex.quote(a) for a in adapter.argv("<prompt>")))
            if getattr(adapter, "transport", "argv") == "stdin":
                say(out, "  (prompt via stdin)")
            say(out, "Prompt of the question phase (the roadmap prompt follows the answers):")
            say(out, roadmap_questions_prompt(idea, gather_context(repo)))
            return EXIT_OK
        except _Stop as stop:
            say(out, str(stop))
            return stop.code
        except _ERRORS as exc:
            say(out, f"Error: {exc}")
            return EXIT_FAILED

    ask = None if yes or not stdin.isatty() else stdin_asker(out, stdin)
    return _run_roadmap(
        repo,
        select_adapter,
        out,
        idea=idea,
        ask=ask,
        commit=commit,
        today=None,
        timeout=timeout,
    )
