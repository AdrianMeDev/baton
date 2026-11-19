import io
import subprocess
from pathlib import Path

import pytest

from baton.adapter import AgentResult, ClaudeAdapter
from baton.config import AgentConfig
from baton.git import head
from baton.limits import LimitStore, UsageLimit
from baton.next import (
    EXIT_EXHAUSTED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_ROADMAP_PROBLEM,
    EXIT_STOPPED,
)
from baton.plan import parse_plan
from baton.planner import RoutedPlanner, run_plan, run_plan_command
from baton.roadmap import parse_roadmap
from baton.router import Route

FIXTURES = Path(__file__).parent / "fixtures"
ROADMAP = (FIXTURES / "roadmaps" / "planning.md").read_text(encoding="utf-8")
VALID = (
    (FIXTURES / "plans" / "generated_valid.md")
    .read_text(encoding="utf-8")
    .replace("M2 – Widgets", "M1 – Widgets")
)
INVALID = VALID.replace("- **Verify:** `pytest -q`\n- **Size:** M\n", "- **Size:** M\n")


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


class FakePlanner:
    def __init__(self, *replies: str, action=None) -> None:
        self.replies = list(replies)
        self.action = action
        self.prompts: list[str] = []

    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        self.prompts.append(prompt)
        if self.action:
            self.action(repo)
        return AgentResult(ok=True, text=self.replies.pop(0))


@pytest.fixture(autouse=True)
def isolated_limit_state(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("limits-state")))


def make_repo(path: Path, *, tracked: bool = True, roadmap: str = ROADMAP) -> Path:
    git(path, "init", "-q")
    (path / "docs").mkdir()
    (path / "docs" / "ROADMAP.md").write_text(roadmap, encoding="utf-8")
    (path / "a.txt").write_text("a\n")
    if not tracked:
        (path / ".gitignore").write_text("docs/\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


def plan_in(repo: Path, adapter, **kw) -> tuple[int, str]:
    out = io.StringIO()
    code = run_plan(repo, adapter, out, today="2026-11-19", **kw)
    return code, out.getvalue()


def roadmap_status(repo: Path, milestone: str) -> str:
    roadmap = parse_roadmap((repo / "docs" / "ROADMAP.md").read_text(encoding="utf-8"))
    found = roadmap.get(milestone)
    assert found is not None
    return found.status


def test_valid_plan_on_first_try(repo: Path):
    planner = FakePlanner(VALID)
    code, out = plan_in(repo, planner)
    assert code == EXIT_OK, out
    assert len(planner.prompts) == 1
    assert "### M1 [ ] Widgets" in planner.prompts[0]
    plan = parse_plan((repo / ".agent-plan.md").read_text(encoding="utf-8"))
    assert plan.created == "2026-11-19"
    assert plan.base == head(repo)
    assert plan.branch is not None and plan.branch != "(detached)"
    assert plan.milestone.id == "M1"
    assert plan.milestone.tracking == "tracked"
    assert len(plan.steps) == 3
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".agent-plan.md" in exclude.splitlines()
    assert roadmap_status(repo, "M1") == "open"  # tracked: the first step commits `[~]`
    assert "Wrote .agent-plan.md" in out


def test_invalid_then_valid_retries_with_errors(repo: Path):
    planner = FakePlanner(INVALID, VALID)
    code, out = plan_in(repo, planner)
    assert code == EXIT_OK, out
    assert len(planner.prompts) == 2
    assert "step 2: missing '**Verify:**'" in planner.prompts[1]
    assert "Attempt 1: the plan was rejected" in out
    assert (repo / ".agent-plan.md").is_file()


def test_invalid_twice_writes_nothing(repo: Path):
    planner = FakePlanner(INVALID, "Sorry, no plan.")
    code, out = plan_in(repo, planner)
    assert code == EXIT_FAILED
    assert len(planner.prompts) == 2
    assert not (repo / ".agent-plan.md").exists()
    assert "No valid plan after 2 attempts" in out


def test_invalid_twice_leaves_finished_plan_untouched(repo: Path):
    old = VALID.replace("### 1. [ ]", "### 1. [x] – abc1234").replace("### 2. [ ]", "### 2. [-]")
    old = old.replace("### 3. [ ]", "### 3. [x] – def5678")
    (repo / ".agent-plan.md").write_text(old, encoding="utf-8")
    code, _ = plan_in(repo, FakePlanner(INVALID, INVALID))
    assert code == EXIT_FAILED
    assert (repo / ".agent-plan.md").read_text(encoding="utf-8") == old


def test_finished_plan_is_replaced(repo: Path):
    old = VALID.replace("[ ]", "[-]")
    (repo / ".agent-plan.md").write_text(old, encoding="utf-8")
    code, out = plan_in(repo, FakePlanner(VALID))
    assert code == EXIT_OK, out
    plan = parse_plan((repo / ".agent-plan.md").read_text(encoding="utf-8"))
    assert all(s.status == "open" for s in plan.steps)


def test_agent_editing_the_tree_is_rejected(repo: Path):
    planner = FakePlanner(VALID, action=lambda r: (r / "a.txt").write_text("changed\n"))
    code, out = plan_in(repo, planner)
    assert code == EXIT_FAILED
    assert "a.txt" in out
    assert not (repo / ".agent-plan.md").exists()


def test_agent_committing_is_rejected(repo: Path):
    def commit(r: Path) -> None:
        (r / "b.txt").write_text("b\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "sneaky")

    code, out = plan_in(repo, FakePlanner(VALID, action=commit))
    assert code == EXIT_FAILED
    assert "HEAD moved" in out
    assert not (repo / ".agent-plan.md").exists()


def test_agent_writing_the_plan_file_is_rejected_and_restored(repo: Path):
    planner = FakePlanner(VALID, action=lambda r: (r / ".agent-plan.md").write_text(VALID))
    code, out = plan_in(repo, planner)
    assert code == EXIT_FAILED
    assert "changed .agent-plan.md" in out
    assert not (repo / ".agent-plan.md").exists()


def test_existing_open_plan_stops(repo: Path):
    (repo / ".agent-plan.md").write_text(VALID, encoding="utf-8")
    planner = FakePlanner(VALID)
    code, out = plan_in(repo, planner)
    assert code == EXIT_STOPPED
    assert "open or pending steps" in out
    assert planner.prompts == []


def test_dirty_tree_stops(repo: Path):
    (repo / "a.txt").write_text("dirty\n")
    planner = FakePlanner(VALID)
    code, out = plan_in(repo, planner)
    assert code == EXIT_STOPPED
    assert "a.txt" in out
    assert planner.prompts == []


def test_no_roadmap_stops(tmp_path: Path):
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    code, out = plan_in(tmp_path, FakePlanner(VALID))
    assert code == EXIT_STOPPED
    assert "ROADMAP.md" in out


def test_no_open_milestone_stops(tmp_path: Path):
    done = ROADMAP.replace("### M1 [ ]", "### M1 [x]").replace("### M2 [ ]", "### M2 [x]")
    repo = make_repo(tmp_path, roadmap=done)
    code, out = plan_in(repo, FakePlanner(VALID))
    assert code == EXIT_STOPPED
    assert "No open milestone" in out


def test_named_milestone_with_unmet_dependencies_stops(repo: Path):
    planner = FakePlanner(VALID)
    code, out = plan_in(repo, planner, milestone_id="M2")
    assert code == EXIT_STOPPED
    assert "M2 depends on M1" in out
    assert planner.prompts == []


@pytest.mark.parametrize(("milestone", "code"), [("M9", EXIT_FAILED), ("M0", EXIT_STOPPED)])
def test_named_milestone_unknown_or_done(repo: Path, milestone: str, code: int):
    assert plan_in(repo, FakePlanner(VALID), milestone_id=milestone)[0] == code


def test_named_milestone_is_planned(tmp_path: Path):
    repo = make_repo(tmp_path, roadmap=ROADMAP.replace("### M1 [ ]", "### M1 [x]"))
    reply = VALID.replace("M1 – Widgets", "M2 – Gadgets")
    planner = FakePlanner(reply)
    code, out = plan_in(repo, planner, milestone_id="M2")
    assert code == EXIT_OK, out
    assert "### M2 [ ] Gadgets" in planner.prompts[0]
    assert parse_plan((repo / ".agent-plan.md").read_text("utf-8")).milestone.id == "M2"


def test_untracked_roadmap_gets_pending_marker(tmp_path: Path):
    repo = make_repo(tmp_path, tracked=False)
    planner = FakePlanner(VALID.replace("tracked)", "local)"))
    code, out = plan_in(repo, planner)
    assert code == EXIT_OK, out
    assert "not tracked" in planner.prompts[0]
    assert roadmap_status(repo, "M1") == "pending"
    plan = parse_plan((repo / ".agent-plan.md").read_text(encoding="utf-8"))
    assert plan.milestone.tracking == "local"


class Limited(FakePlanner):
    def run(self, prompt: str, repo: Path, timeout: float) -> AgentResult:
        self.prompts.append(prompt)
        return AgentResult(ok=False, error="limit", limit=UsageLimit())


def routed(names: tuple[str, ...], out: io.StringIO, store: LimitStore) -> RoutedPlanner:
    return RoutedPlanner(Route("M", tuple(AgentConfig(n, "claude", n) for n in names)), store, out)


def test_limit_handoff_restarts_the_plan_on_the_next_agent(repo: Path, monkeypatch):
    agents = {"first": Limited(), "second": FakePlanner(VALID)}
    monkeypatch.setattr(
        "baton.routed.make_adapter", lambda agent, read_only=False: agents[agent.name]
    )
    store = LimitStore()
    out = io.StringIO()
    code = run_plan(repo, routed(("first", "second"), out, store), out, today="2026-11-19")
    assert code == EXIT_OK, out.getvalue()
    assert agents["second"].prompts == agents["first"].prompts  # fresh prompt, no handoff
    assert "first" in store.active()
    assert "usage limit" in out.getvalue()


def test_exhausted_route_writes_nothing(repo: Path, monkeypatch):
    monkeypatch.setattr("baton.routed.make_adapter", lambda agent, read_only=False: Limited())
    out = io.StringIO()
    code = run_plan(repo, routed(("only",), out, LimitStore()), out, today="2026-11-19")
    assert code == EXIT_EXHAUSTED
    assert "No plan was written" in out.getvalue()
    assert not (repo / ".agent-plan.md").exists()


PLAN_CONFIG = """[agents.small]
adapter = "claude"
command = "small-claude"
[agents.big]
adapter = "codex"
command = "big-codex"
[routes.plan_commits]
M = ["small"]
L = ["big"]
"""


def plan_command(repo: Path, config: Path, **kw) -> tuple[int, str]:
    out = io.StringIO()
    code = run_plan_command(repo, config_path=str(config), out=out, **kw)
    return code, out.getvalue()


@pytest.fixture
def config(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("config") / "config.toml"
    path.write_text(PLAN_CONFIG, encoding="utf-8")
    return path


def test_dry_run_prints_milestone_command_and_prompt(repo: Path, config: Path):
    code, out = plan_command(repo, config, dry_run=True)
    assert code == EXIT_OK, out
    assert "Milestone: M1 – Widgets (docs/ROADMAP.md, tracked)" in out
    assert "Tier M, agent small (claude)" in out  # tier from the milestone's Size
    assert "small-claude" in out
    assert "### M1 [ ] Widgets" in out
    assert not (repo / ".agent-plan.md").exists()
    assert "agent-plan" not in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")


def test_dry_run_tier_and_milestone_override(tmp_path: Path, config: Path):
    repo = make_repo(tmp_path, roadmap=ROADMAP.replace("### M1 [ ]", "### M1 [x]"))
    code, out = plan_command(repo, config, dry_run=True, milestone_id="M2")
    assert code == EXIT_OK, out
    assert "Tier L, agent big (codex)" in out  # M2 has Size L
    code, out = plan_command(repo, config, dry_run=True, milestone_id="M2", tier="M")
    assert "Tier M, agent small (claude)" in out


def test_missing_route_exits_with_clear_message(repo: Path, tmp_path_factory):
    config = tmp_path_factory.mktemp("legacy") / "config.toml"
    config.write_text('[agents.fake]\nadapter = "claude"\ncommand = "claude"\n')
    for dry_run in (True, False):
        code, out = plan_command(repo, config, dry_run=dry_run)
        assert code == EXIT_FAILED
        assert "configure [routes.plan_commits] M" in out
    assert not (repo / ".agent-plan.md").exists()


def test_unknown_agent_exits_with_clear_message(repo: Path, config: Path):
    code, out = plan_command(repo, config, agent_name="nope", dry_run=True)
    assert code == EXIT_FAILED
    assert "unknown agent 'nope'" in out


def test_command_routes_and_writes_the_plan(repo: Path, config: Path, monkeypatch):
    planner = FakePlanner(VALID)
    monkeypatch.setattr("baton.routed.make_adapter", lambda agent, read_only=False: planner)
    code, out = plan_command(repo, config)
    assert code == EXIT_OK, out
    assert "Tier M, agent small (claude)" in out
    assert parse_plan((repo / ".agent-plan.md").read_text("utf-8")).milestone.id == "M1"


def test_planner_roadmap_problem_stops_and_writes_nothing(repo: Path):
    planner = FakePlanner("BATON-ROADMAP-PROBLEM: M1 depends on a missing API")
    code, out = plan_in(repo, planner)
    assert code == EXIT_ROADMAP_PROBLEM
    assert "M1 depends on a missing API" in out
    assert len(planner.prompts) == 1  # no retry
    assert not (repo / ".agent-plan.md").exists()


def test_roles_request_read_only_adapters(repo: Path, monkeypatch):
    from baton.routed import RoutedAdapter

    seen: list[bool] = []

    def fake_make(agent, read_only=False):
        seen.append(read_only)
        return ClaudeAdapter(read_only=read_only)

    monkeypatch.setattr("baton.routed.make_adapter", fake_make)
    route = Route("M", (AgentConfig("a", "claude", "a"),))
    out = io.StringIO()
    RoutedPlanner(route, LimitStore(), out).argv("p")
    RoutedAdapter(route, LimitStore(), out, review=True).argv("p")
    RoutedAdapter(route, LimitStore(), out).argv("p")
    assert seen == [True, True, False]
