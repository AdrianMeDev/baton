import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from baton.adapter import AgentResult
from baton.audit import MAX_FILE_CHARS, parse_findings, parse_inventory
from baton.auditor import run_audit
from baton.cli import main
from baton.next import EXIT_EXHAUSTED, EXIT_FAILED, EXIT_OK, EXIT_STOPPED


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


def run(repo: Path, scanner: Scanner, **kw) -> tuple[int, str]:
    out = io.StringIO()
    code = run_audit(repo, scanner, out, today="2026-11-20", **kw)
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


def test_second_run_makes_no_scan_call(repo: Path) -> None:
    run(repo, Scanner(CLEAN))
    again = Scanner(CLEAN)
    code, out = run(repo, again)
    assert code == EXIT_OK
    assert again.prompts == []
    assert "Scan complete" in out


def test_skipped_files_never_reach_a_prompt(repo: Path) -> None:
    scanner = Scanner(CLEAN)
    run(repo, scanner)
    everything = "\n".join(scanner.prompts)
    for secret in ("hunter2", "locked", "xxxxxxxx"):
        assert secret not in everything
    assert "# demo" in everything and "guide" in everything


def test_repo_is_untouched_apart_from_the_audit_file(repo: Path) -> None:
    scanner = Scanner(CLEAN)
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
    code, out = run(repo, Scanner(AgentResult(ok=False, error="boom")))
    assert code == EXIT_FAILED
    assert "The agent failed: boom" in out


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
    assert run(repo, Scanner(CLEAN))[0] == EXIT_OK
    assert not any(f.status == "open" for f in parse_inventory(audit_text(repo)))


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
