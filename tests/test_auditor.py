import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from baton.adapter import AgentResult
from baton.audit import MAX_FILE_CHARS, parse_findings, parse_inventory, validate_audit_plan
from baton.auditor import run_audit, run_audit_fix
from baton.cli import main
from baton.next import EXIT_EXHAUSTED, EXIT_FAILED, EXIT_OK, EXIT_STOPPED
from baton.roadmap import next_milestone, parse_roadmap


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class Scanner:
    """Replies per call; ``cwd`` records where the agent ran."""

    def __init__(self, *replies: AgentResult | str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.cwds: list[Path] = []
        self.exhausted = False

    def argv(self, prompt: str) -> list[str]:
        return ["fake-agent", prompt]

    def run(self, prompt: str, cwd: Path, timeout: float) -> AgentResult:
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        assert not (cwd / "README.md").exists()
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply if isinstance(reply, AgentResult) else AgentResult(ok=True, text=reply)


def finding(path: str, **over: object) -> dict:
    return {
        "severity": "warning",
        "category": "smell",
        "message": "too long",
        "path": path,
        **over,
    }


def reply(*items: dict) -> str:
    return json.dumps({"findings": list(items)})


CLEAN = reply()


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # commit_all runs plain `git commit`, which needs an identity on CI runners.
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@example.com")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    files = {
        "README.md": "# demo\n",
        "src/a.py": "a = 1\n",
        "src/b.py": "b = 2\n",
        "docs/guide.md": "guide\n",
        ".env": "SECRET=hunter2\n",
        "uv.lock": "locked\n",
        "big.txt": "x" * (MAX_FILE_CHARS + 1),
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (tmp_path / "logo.png").write_bytes(b"\0\1\2")
    os.symlink("README.md", tmp_path / "link")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


class FixPlanner:
    """Plans one milestone that covers every critical and warning finding of the audit file."""

    def __init__(self, *replies: str | None, action=None) -> None:
        self.replies = list(replies)  # None: plan every finding; the last reply repeats
        self.prompts: list[str] = []
        self.action = action
        self.exhausted = False

    def argv(self, prompt: str) -> list[str]:
        return ["fake-planner", prompt]

    def run(self, prompt: str, cwd: Path, timeout: float) -> AgentResult:
        self.prompts.append(prompt)
        if self.action:
            self.action(cwd)
        reply = (
            (self.replies.pop(0) if len(self.replies) > 1 else self.replies[0])
            if self.replies
            else None
        )
        if reply is not None:
            return AgentResult(ok=True, text=reply)
        found = parse_findings((cwd / "docs" / "AUDIT.md").read_text(encoding="utf-8"))
        ids = ", ".join(f"F{f.id}" for f in found if f.severity != "informational")
        milestones = (
            "## Milestones\n\n### M1 [ ] Audit: Fix findings\n- **Goal:** Fix them.\n"
            f"- **Includes:** {ids}\n- **Done when:** All fixed.\n- **Depends on:** -\n"
            "- **Size:** M, ~4 commits\n\n"
            if ids
            else ""
        )
        return AgentResult(ok=True, text=f"{milestones}## Suggestions\n- Add CI.\n")


def run(repo: Path, scanner: Scanner, planner: FixPlanner | None = None, **kw) -> tuple[int, str]:
    out = io.StringIO()
    planner = planner or FixPlanner()
    code = run_audit(repo, scanner, out, planner=lambda: planner, today="2026-11-20", **kw)
    return code, out.getvalue()


def audit_text(repo: Path) -> str:
    return (repo / "docs" / "AUDIT.md").read_text(encoding="utf-8")


def test_full_run_marks_every_reviewable_file_and_records_findings(repo: Path) -> None:
    scanner = Scanner(
        reply(finding("README.md")),
        reply(finding("docs/guide.md", severity="critical", category="security")),
        reply(finding("src/a.py", line=1), finding("src/b.py")),
    )
    code, out = run(repo, scanner)
    assert code == EXIT_OK
    assert "Scan complete" in out
    files = {f.path: f for f in parse_inventory(audit_text(repo))}
    reviewable = {"README.md", "docs/guide.md", "src/a.py", "src/b.py"}
    assert {p for p, f in files.items() if f.status == "done"} == reviewable
    assert {p for p, f in files.items() if f.status == "skipped"} == {
        ".env",
        "uv.lock",
        "big.txt",
        "logo.png",
        "link",
    }
    assert [f.id for f in parse_findings(audit_text(repo))] == [1, 2, 3, 4]
    assert len(scanner.prompts) == 3  # one batch per folder
    assert validate_audit_plan(audit_text(repo)) == []


def test_second_run_makes_no_scan_or_plan_call(repo: Path) -> None:
    run(repo, Scanner(CLEAN))
    again, replan = Scanner(CLEAN), FixPlanner()
    before = audit_text(repo)
    code, out = run(repo, again, replan)
    assert code == EXIT_OK
    assert again.prompts == [] and replan.prompts == []
    assert "already in place" in out
    assert audit_text(repo) == before


def test_skipped_files_never_reach_a_prompt(repo: Path) -> None:
    scanner = Scanner(CLEAN)
    run(repo, scanner)
    everything = "\n".join(scanner.prompts)
    for secret in ("hunter2", "locked", "xxxxxxxx"):
        assert secret not in everything
    assert "# demo" in everything and "guide" in everything


def test_repo_is_untouched_apart_from_the_audit_file(repo: Path) -> None:
    scanner = Scanner(reply(finding("README.md")))
    run(repo, scanner)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == "?? docs/AUDIT.md"
    assert all(cwd != repo for cwd in scanner.cwds)


def test_resume_after_a_stop_continues_at_the_next_batch(repo: Path) -> None:
    first = Scanner(reply(finding("README.md")))
    code, out = run(repo, first, max_batches=1)
    assert code == EXIT_OK and "Stopped after 1 batches" in out
    done = [f.path for f in parse_inventory(audit_text(repo)) if f.status == "done"]
    assert len(done) == 1
    second = Scanner(CLEAN)
    assert run(repo, second)[0] == EXIT_OK
    assert len(second.prompts) == 2
    assert "README.md" not in "".join(second.prompts)
    assert [f.id for f in parse_findings(audit_text(repo))] == [1]


def test_invalid_reply_is_retried_once_with_the_error(repo: Path) -> None:
    scanner = Scanner("not json", CLEAN)
    code, out = run(repo, scanner, max_batches=1)
    assert code == EXIT_OK
    assert "Attempt 1: the reply was rejected" in out
    assert len(scanner.prompts) == 2
    assert "Your previous reply was rejected" in scanner.prompts[1]
    assert "not json" in scanner.prompts[1]


def test_two_invalid_replies_stop_and_leave_the_batch_open(repo: Path) -> None:
    scanner = Scanner(reply(finding("elsewhere.py")))
    code, out = run(repo, scanner)
    assert code == EXIT_FAILED
    assert "No valid reply after 2 attempts" in out
    assert len(scanner.prompts) == 2
    assert all(f.status != "done" for f in parse_inventory(audit_text(repo)))


def test_failed_agent_stops_with_its_error(repo: Path) -> None:
    planner = FixPlanner()
    code, out = run(repo, Scanner(AgentResult(ok=False, error="boom")), planner)
    assert code == EXIT_FAILED
    assert "The agent failed: boom" in out
    assert planner.prompts == []


def test_exhausted_route_exits_3_and_a_rerun_continues(repo: Path) -> None:
    scanner = Scanner(CLEAN, AgentResult(ok=False, error="no agents left"))
    original_run = scanner.run

    def run_and_exhaust(prompt: str, cwd: Path, timeout: float) -> AgentResult:
        result = original_run(prompt, cwd, timeout)
        scanner.exhausted = not result.ok
        return result

    scanner.run = run_and_exhaust  # type: ignore[method-assign]
    code, out = run(repo, scanner)
    assert code == EXIT_EXHAUSTED
    assert "Rerun `baton audit` to continue" in out
    assert sum(f.status == "done" for f in parse_inventory(audit_text(repo))) == 1
    planner = FixPlanner()
    assert run(repo, Scanner(CLEAN), planner)[0] == EXIT_OK
    assert not any(f.status == "open" for f in parse_inventory(audit_text(repo)))
    assert len(planner.prompts) == 1


def test_warns_when_head_moved_and_still_reads_the_base(repo: Path) -> None:
    run(repo, Scanner(CLEAN), max_batches=1)
    (repo / "src" / "a.py").write_text("changed = True\n")
    git(repo, "commit", "-qam", "more")
    scanner = Scanner(CLEAN)
    code, out = run(repo, scanner)
    assert code == EXIT_OK
    assert "Warning: HEAD moved" in out
    assert "changed = True" not in "".join(scanner.prompts)


def test_outside_a_repository_and_without_commits(tmp_path: Path) -> None:
    code, out = run(tmp_path, Scanner(CLEAN))
    assert code == EXIT_STOPPED and "not a git repository" in out
    git(tmp_path, "init", "-q")
    code, out = run(tmp_path, Scanner(CLEAN))
    assert code == EXIT_STOPPED and "no commits" in out
    assert not (tmp_path / "docs").exists()


def test_damaged_audit_file_is_reported(repo: Path) -> None:
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "AUDIT.md").write_text("# nothing\n")
    code, out = run(repo, Scanner(CLEAN))
    assert code == EXIT_FAILED and "Base" in out


def test_dry_run_prints_counts_command_and_first_prompt_and_writes_nothing(
    repo: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    elsewhere = tmp_path_factory.mktemp("config")
    monkeypatch.setenv("XDG_STATE_HOME", str(elsewhere / "state"))
    config = elsewhere / "config.toml"
    config.write_text(
        '[agents.rev]\nadapter = "claude"\ncommand = "rev"\n[routes.review]\nM = ["rev"]\n',
        encoding="utf-8",
    )
    code = main(["--config", str(config), "audit", "--path", str(repo), "--dry-run"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "9 files, 5 skipped, 3 batches still open." in out
    assert "Tier M, agent rev (claude)" in out
    assert "Prompt of the first batch" in out and "# demo" in out
    assert "hunter2" not in out
    assert not (repo / "docs" / "AUDIT.md").exists()
    assert git(repo, "status", "--porcelain") == ""


def _scan_with_findings() -> Scanner:
    return Scanner(
        reply(finding("README.md", severity="critical", category="security")),
        reply(finding("docs/guide.md")),
        reply(finding("src/a.py", severity="informational")),
    )


def test_planning_writes_milestones_that_parse_roadmap_accepts(repo: Path) -> None:
    planner = FixPlanner()
    code, out = run(repo, _scan_with_findings(), planner)
    assert code == EXIT_OK
    text = audit_text(repo)
    assert validate_audit_plan(text) == []
    milestone = next_milestone(parse_roadmap(text))
    assert milestone is not None and milestone.id == "M1" and "F1, F2" in milestone.includes
    assert "## Suggestions\n\n- Add CI." in text
    assert "F1 · critical · security · README.md – too long" in planner.prompts[0]
    assert "Planning fixes for 3 findings" in out
    assert "git commit -m 'docs: add code audit'" in out
    assert "baton audit --fix" in out
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == "?? docs/AUDIT.md"


def test_invalid_plan_is_retried_once_with_the_errors(repo: Path) -> None:
    planner = FixPlanner("## Suggestions\n- only ideas\n", None)
    code, out = run(repo, _scan_with_findings(), planner)
    assert code == EXIT_OK
    assert "Attempt 1: the plan was rejected" in out
    assert "finding F1 is not covered by any milestone" in planner.prompts[1]
    assert validate_audit_plan(audit_text(repo)) == []


def test_two_rejected_plans_leave_the_scan_saved_and_no_plan(repo: Path) -> None:
    planner = FixPlanner("## Suggestions\n- only ideas\n")
    code, out = run(repo, _scan_with_findings(), planner)
    assert code == EXIT_FAILED
    assert "No valid fix plan after 2 attempts" in out
    assert len(planner.prompts) == 2
    text = audit_text(repo)
    assert "## Milestones" not in text and "## Suggestions" not in text
    assert len(parse_findings(text)) == 3
    fixed = FixPlanner()
    assert run(repo, Scanner(CLEAN), fixed)[0] == EXIT_OK  # rerun plans without scanning
    assert validate_audit_plan(audit_text(repo)) == []


def test_planner_that_edits_the_tree_is_rejected(repo: Path) -> None:
    planner = FixPlanner(action=lambda cwd: (cwd / "src" / "a.py").write_text("hacked\n"))
    code, out = run(repo, _scan_with_findings(), planner)
    assert code == EXIT_FAILED
    assert "changed the working tree" in out
    assert "## Milestones" not in audit_text(repo)


def test_planner_that_commits_is_rejected(repo: Path) -> None:
    def commit(cwd: Path) -> None:
        (cwd / "x.txt").write_text("x\n")
        git(cwd, "add", "-A")
        git(cwd, "commit", "-qm", "sneaky")

    code, out = run(repo, _scan_with_findings(), FixPlanner(action=commit))
    assert code == EXIT_FAILED and "the agent committed" in out


def test_exhausted_planner_exits_3(repo: Path) -> None:
    planner = FixPlanner()
    planner.run = lambda *a: (  # type: ignore[method-assign]
        setattr(planner, "exhausted", True) or AgentResult(ok=False, error="no agents")
    )
    code, out = run(repo, _scan_with_findings(), planner)
    assert code == EXIT_EXHAUSTED and "no agents" in out


def test_no_critical_or_warning_findings_plan_suggestions_only(repo: Path) -> None:
    planner = FixPlanner()
    scanner = Scanner(CLEAN, CLEAN, reply(finding("src/a.py", severity="informational")))
    code, _ = run(repo, scanner, planner)
    assert code == EXIT_OK
    text = audit_text(repo)
    assert "## Milestones" not in text and "## Suggestions" in text
    assert validate_audit_plan(text) == []


def test_commit_flag_commits_the_finished_audit(repo: Path) -> None:
    code, out = run(repo, _scan_with_findings(), commit=True)
    assert code == EXIT_OK
    assert git(repo, "log", "--format=%s", "-1") == "docs: add code audit"
    assert git(repo, "status", "--porcelain") == ""
    assert "Committed:" in out
    again, replan = Scanner(CLEAN), FixPlanner()
    code, out = run(repo, again, replan, commit=True)
    assert code == EXIT_OK and again.prompts == [] and replan.prompts == []
    assert "Committed" not in out


def test_commit_flag_refuses_a_dirty_tree_before_scanning(repo: Path) -> None:
    (repo / "notes.txt").write_text("wip\n")
    scanner = Scanner(CLEAN)
    code, out = run(repo, scanner, commit=True)
    assert code == EXIT_STOPPED and "notes.txt" in out
    assert scanner.prompts == [] and not (repo / "docs" / "AUDIT.md").exists()


class AutoSpy:
    def __init__(self, code: int = EXIT_OK) -> None:
        self.code = code
        self.calls: list[tuple[Path, dict]] = []

    def __call__(self, repo: Path, **kw) -> int:
        self.calls.append((repo, kw))
        return self.code


def fix(repo: Path, spy: AutoSpy, **kw) -> tuple[int, str]:
    out = io.StringIO()
    return run_audit_fix(repo, out=out, auto=spy, **kw), out.getvalue()


def _committed_audit(repo: Path, scanner: Scanner | None = None) -> None:
    assert run(repo, scanner or _scan_with_findings(), commit=True)[0] == EXIT_OK


def test_fix_delegates_to_auto_over_the_audit_file(repo: Path) -> None:
    _committed_audit(repo)
    spy = AutoSpy(1)
    code, _ = fix(
        repo,
        spy,
        config_path="c.toml",
        max_steps=4,
        max_hours=1.5,
        merge=False,
        agent_timeout=60.0,
        verify_timeout=30.0,
    )
    assert code == 1  # auto's exit code is passed through
    assert len(spy.calls) == 1
    called_repo, kw = spy.calls[0]
    assert called_repo == repo
    assert kw["roadmap"] == "docs/AUDIT.md"
    assert (kw["config_path"], kw["max_steps"], kw["max_hours"], kw["merge"]) == (
        "c.toml",
        4,
        1.5,
        False,
    )
    assert (kw["agent_timeout"], kw["verify_timeout"]) == (60.0, 30.0)


def test_fix_needs_the_audit_file(repo: Path) -> None:
    spy = AutoSpy()
    code, out = fix(repo, spy)
    assert code == EXIT_STOPPED and "run `baton audit` first" in out and spy.calls == []


def test_fix_refuses_an_incomplete_scan(repo: Path) -> None:
    run(repo, Scanner(CLEAN), max_batches=1)
    spy = AutoSpy()
    code, out = fix(repo, spy)
    assert code == EXIT_STOPPED
    assert "scan is incomplete" in out and "Run `baton audit`" in out and spy.calls == []


def test_fix_refuses_a_scan_without_a_plan(repo: Path) -> None:
    planner = FixPlanner("## Suggestions\n- only ideas\n")
    assert run(repo, _scan_with_findings(), planner)[0] == EXIT_FAILED
    spy = AutoSpy()
    code, out = fix(repo, spy)
    assert code == EXIT_STOPPED and "fix plan is missing or invalid" in out
    assert "finding F1 is not covered" in out and spy.calls == []


def test_fix_with_nothing_to_fix_is_a_success_without_auto(repo: Path) -> None:
    scanner = Scanner(CLEAN, CLEAN, reply(finding("src/a.py", severity="informational")))
    _committed_audit(repo, scanner)
    spy = AutoSpy()
    code, out = fix(repo, spy)
    assert code == EXIT_OK and "Nothing to fix" in out and spy.calls == []


def test_fix_refuses_an_uncommitted_audit(repo: Path) -> None:
    assert run(repo, _scan_with_findings())[0] == EXIT_OK  # written, not committed
    spy = AutoSpy()
    code, out = fix(repo, spy)
    assert code == EXIT_STOPPED and "not committed yet" in out and spy.calls == []


def test_fix_refuses_a_modified_audit(repo: Path) -> None:
    _committed_audit(repo)
    path = repo / "docs" / "AUDIT.md"
    path.write_text(path.read_text() + "\n- edited\n")
    spy = AutoSpy()
    code, out = fix(repo, spy)
    assert code == EXIT_STOPPED and "uncommitted changes" in out and spy.calls == []


def test_fix_outside_a_repository(tmp_path: Path) -> None:
    spy = AutoSpy()
    code, out = fix(tmp_path, spy)
    assert code == EXIT_STOPPED and "not a git repository" in out and spy.calls == []


def test_cli_fix_and_scan_flags_do_not_mix(repo: Path, capsys: pytest.CaptureFixture) -> None:
    for argv in (["--fix", "--dry-run"], ["--fix", "--tier", "S"], ["--fix", "--commit"]):
        with pytest.raises(SystemExit) as exc:
            main(["audit", "--path", str(repo), *argv])
        assert exc.value.code == 2
    assert "--fix cannot be combined with --dry-run" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["audit", "--path", str(repo), "--max-steps", "3"])
    assert "--max-steps only apply with --fix" in capsys.readouterr().err


def test_cli_fix_reaches_the_precondition_checks(repo: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["audit", "--path", str(repo), "--fix", "--max-steps", "2"]) == EXIT_STOPPED
    assert "run `baton audit` first" in capsys.readouterr().out
