import io
import subprocess
from pathlib import Path

import pytest
from test_roadmap import VALID_ROADMAP

from baton.adapter import AgentResult
from baton.cli import main
from baton.git import head
from baton.limits import UsageLimit
from baton.next import EXIT_EXHAUSTED, EXIT_FAILED, EXIT_OK, EXIT_STOPPED
from baton.plan import parse_plan
from baton.planner import run_plan
from baton.roadmap import Question, find_roadmap, load_roadmap, next_milestone
from baton.roadmapper import run_roadmap, run_roadmap_command, stdin_asker

QUESTIONS = (
    "BATON-QUESTION: Who uses it? | default: just me\nBATON-QUESTION: Stack? | default: python"
)


class Planner:
    """Answers the question phase first, then the roadmap; ``action`` may misbehave."""

    def __init__(self, *replies: str, action=None) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.action = action

    def argv(self, prompt: str) -> list[str]:
        return ["fake-agent", prompt]

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        self.prompts.append(prompt)
        if self.action:
            self.action(repo)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return AgentResult(ok=True, text=reply)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("# demo\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def run(repo: Path, planner: Planner, **kw) -> tuple[int, str]:
    out = io.StringIO()
    code = run_roadmap(repo, planner, out, idea="a todo app", today="2026-11-19", **kw)
    return code, out.getvalue()


def test_questions_are_asked_and_answers_reach_the_roadmap_prompt(repo):
    asked: list[str] = []

    def ask(question: Question) -> str:
        asked.append(question.text)
        return "my team" if question.text.startswith("Who") else ""

    planner = Planner(QUESTIONS, VALID_ROADMAP)

    code, out = run(repo, planner, ask=ask)

    assert code == EXIT_OK, out
    assert asked == ["Who uses it?", "Stack?"]
    assert "- Who uses it? -> my team" in planner.prompts[1]
    assert "- Stack? -> python" in planner.prompts[1]
    assert "a todo app" in planner.prompts[0] and "README.md" in planner.prompts[0]
    roadmap = load_roadmap(repo / "docs" / "ROADMAP.md")
    assert [m.id for m in roadmap.milestones] == ["M0", "M1"]
    assert "Updated: 2026-11-19" in (repo / "docs" / "ROADMAP.md").read_text()


def test_without_ask_the_defaults_are_taken_and_shown(repo):
    planner = Planner(QUESTIONS, VALID_ROADMAP)

    code, out = run(repo, planner)

    assert code == EXIT_OK
    assert "-> just me (default)" in out
    assert "- Who uses it? -> just me" in planner.prompts[1]


def test_no_questions_goes_straight_to_the_roadmap(repo):
    code, _ = run(repo, Planner("none", VALID_ROADMAP))

    assert code == EXIT_OK


def test_invalid_roadmap_is_retried_once_with_the_errors(repo):
    bad = VALID_ROADMAP.replace("- **Goal:** One page works.\n", "")
    planner = Planner("none", bad, VALID_ROADMAP)

    code, out = run(repo, planner)

    assert code == EXIT_OK
    assert "Attempt 1: the roadmap was rejected" in out
    assert "M0: missing '**Goal:**'" in planner.prompts[2]
    assert len(planner.prompts) == 3


def test_two_invalid_roadmaps_write_nothing(repo):
    code, out = run(repo, Planner("none", "no roadmap here"))

    assert code == EXIT_FAILED
    assert "No valid roadmap after 2 attempts" in out
    assert not (repo / "docs").exists()


def test_existing_roadmap_is_never_overwritten(repo):
    (repo / "ROADMAP.md").write_text("# mine\n")
    planner = Planner(QUESTIONS)

    code, out = run(repo, planner)

    assert code == EXIT_STOPPED
    assert "ROADMAP.md already exists" in out
    assert planner.prompts == []
    assert (repo / "ROADMAP.md").read_text() == "# mine\n"


def test_commit_flag_commits_the_roadmap_and_leaves_a_clean_tree(repo):
    code, out = run(repo, Planner("none", VALID_ROADMAP), commit=True)

    assert code == EXIT_OK, out
    assert git(repo, "log", "-1", "--format=%s").strip() == "docs: add roadmap"
    assert git(repo, "status", "--porcelain") == ""
    assert "baton auto" in out


def test_without_commit_the_output_explains_how(repo):
    code, out = run(repo, Planner("none", VALID_ROADMAP))

    assert code == EXIT_OK
    assert "git commit -m 'docs: add roadmap'" in out
    assert git(repo, "status", "--porcelain").strip() == "?? docs/"


def test_commit_flag_refuses_a_dirty_tree(repo):
    (repo / "wip.txt").write_text("x")
    planner = Planner("none", VALID_ROADMAP)

    code, out = run(repo, planner, commit=True)

    assert code == EXIT_STOPPED
    assert "wip.txt" in out
    assert planner.prompts == []


def test_greenfield_repository_without_commits(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")

    code, out = run(tmp_path, Planner("none", VALID_ROADMAP), commit=True)

    assert code == EXIT_OK, out
    assert git(tmp_path, "log", "-1", "--format=%s").strip() == "docs: add roadmap"


@pytest.mark.parametrize("phase", [0, 1])
def test_agent_that_edits_files_is_rejected(repo, phase):
    def meddle(r: Path) -> None:
        (r / "evil.txt").write_text("x")

    class Late(Planner):
        def run(self, prompt, repo, timeout):
            if len(self.prompts) == phase:
                self.action = meddle
            else:
                self.action = None
            return super().run(prompt, repo, timeout)

    code, out = run(repo, Late("none", VALID_ROADMAP))

    assert code == EXIT_FAILED
    assert "changed the working tree" in out and "evil.txt" in out
    assert not (repo / "docs").exists()


def test_agent_that_commits_is_rejected(repo):
    def commit(r: Path) -> None:
        (r / "x.txt").write_text("x")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "sneaky")

    before = head(repo)

    code, out = run(repo, Planner("none", VALID_ROADMAP, action=commit))

    assert code == EXIT_FAILED
    assert "HEAD moved" in out and before in out


def test_failed_agent_stops(repo):
    class Failing:
        exhausted = True

        def run(self, prompt, repo, timeout):
            return AgentResult(ok=False, error="all limited")

    out = io.StringIO()
    code = run_roadmap(repo, Failing(), out, idea="x")

    assert code == EXIT_EXHAUSTED
    assert "all limited" in out.getvalue()


def test_empty_idea_is_refused(repo):
    out = io.StringIO()

    assert run_roadmap(repo, Planner("none"), out, idea="  ") == EXIT_STOPPED
    assert "No idea given" in out.getvalue()


def test_stdin_asker_prints_the_default_and_reads_a_line():
    out, stdin = io.StringIO(), io.StringIO("my answer\n\n")
    ask = stdin_asker(out, stdin)

    assert ask(Question("Who?", "me")).strip() == "my answer"
    assert ask(Question("Where?", "here")).strip() == ""
    assert "Who?" in out.getvalue() and "[me]" in out.getvalue()


CONFIG = """[agents.first]
adapter="claude"
command="first"
[agents.second]
adapter="codex"
command="second"
[routes.plan_roadmap]
S=["first"]
M=["first"]
L=["first", "second"]
"""


@pytest.fixture
def configured(repo, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))
    config = tmp_path_factory.mktemp("config") / "config.toml"
    config.write_text(CONFIG)
    agents = {
        "first": Planner(QUESTIONS, VALID_ROADMAP),
        "second": Planner(QUESTIONS, VALID_ROADMAP),
    }
    made: list[tuple[str, bool]] = []

    def make(agent, read_only=False):
        made.append((agent.name, read_only))
        return agents[agent.name]

    monkeypatch.setattr("baton.routed.make_adapter", make)
    return config, agents, made


def test_command_routes_plan_roadmap_at_tier_l_read_only(repo, configured):
    config, agents, made = configured
    out = io.StringIO()

    code = run_roadmap_command(
        repo, idea="a todo app", config_path=str(config), yes=True, commit=True, out=out
    )

    assert code == EXIT_OK, out.getvalue()
    assert "Tier L, agent first" in out.getvalue()
    assert ("first", True) in made
    assert len(agents["first"].prompts) == 2


def test_command_limited_agent_falls_back_to_the_next(repo, configured):
    config, agents, _ = configured

    class Limited:
        def run(self, prompt, repo, timeout):
            return AgentResult(ok=False, error="quota", limit=UsageLimit())

    agents["first"] = Limited()
    out = io.StringIO()

    code = run_roadmap_command(repo, idea="x", config_path=str(config), yes=True, out=out)

    assert code == EXIT_OK, out.getvalue()
    assert "first reached its usage limit" in out.getvalue()
    assert len(agents["second"].prompts) == 2


def test_command_reads_the_idea_from_a_file(repo, configured, tmp_path):
    config, agents, _ = configured
    idea = tmp_path / "idea.txt"
    idea.write_text("an idea from a file")

    code = run_roadmap_command(
        repo, from_file=str(idea), config_path=str(config), yes=True, out=io.StringIO()
    )

    assert code == EXIT_OK
    assert "an idea from a file" in agents["first"].prompts[0]


def test_command_rejects_idea_and_file_together(repo, configured, tmp_path):
    config, _, _ = configured
    idea = tmp_path / "idea.txt"
    idea.write_text("x")
    out = io.StringIO()

    code = run_roadmap_command(
        repo, idea="y", from_file=str(idea), config_path=str(config), out=out
    )

    assert code == EXIT_STOPPED
    assert "not both" in out.getvalue()


def test_dry_run_prints_command_and_prompt_and_changes_nothing(repo, configured):
    config, agents, _ = configured
    out = io.StringIO()

    code = run_roadmap_command(
        repo, idea="a todo app", config_path=str(config), dry_run=True, out=out
    )

    assert code == EXIT_OK
    assert "Command (cwd = repo root):" in out.getvalue()
    assert "a todo app" in out.getvalue()
    assert agents["first"].prompts == []
    assert git(repo, "status", "--porcelain") == ""


def test_cli_registers_the_command(repo, configured, capsys):
    config, _, _ = configured

    code = main(["--config", str(config), "roadmap", "--path", str(repo), "--dry-run", "an idea"])

    assert code == EXIT_OK
    assert "an idea" in capsys.readouterr().out


def test_after_roadmap_baton_plan_selects_the_first_milestone(repo):
    code, _ = run(repo, Planner("none", VALID_ROADMAP), commit=True)
    assert code == EXIT_OK
    assert find_roadmap(repo) is not None
    milestone = next_milestone(load_roadmap(repo / "docs" / "ROADMAP.md"))
    assert milestone is not None and milestone.id == "M0"

    plan_reply = Path(__file__).parent / "fixtures" / "plans" / "generated_valid.md"
    text = plan_reply.read_text().replace("M2 – Widgets", "M0 – Walking skeleton")
    out = io.StringIO()

    assert run_plan(repo, fixed_planner(text), out, today="2026-11-19") == EXIT_OK, out.getvalue()
    assert parse_plan((repo / ".agent-plan.md").read_text()).milestone.id == "M0"


def fixed_planner(text: str):
    class Fixed:
        def run(self, prompt, repo, timeout):
            return AgentResult(ok=True, text=text)

    return Fixed()
