import io
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from baton.auto import Budget, MilestoneHooks, ff_base, run_milestone
from baton.autostate import AutoStore
from baton.next import EXIT_EXHAUSTED, EXIT_FAILED, EXIT_OK, EXIT_ROADMAP_PROBLEM, EXIT_STOPPED
from baton.plan import parse_plan

ROADMAP = (Path(__file__).parent / "fixtures" / "roadmaps" / "planning.md").read_text("utf-8")
NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)

PLAN = """# Plan: Widgets
Created: 2026-11-18 · Branch: main · Base: abc1234
Milestone: M1 – Widgets (docs/ROADMAP.md, tracked)

## Steps

### 1. {s1} feat(widget): first
- **Why:** w
- **Verify:** `true`

### 2. {s2} feat(widget): second
- **Why:** w
- **Verify:** `true`

## Log
"""


class World:
    """A fake repository: commands edit the plan file the way the real ones would."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        self.repo = tmp_path / "repo"
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "docs" / "ROADMAP.md").write_text(ROADMAP)
        self.store = AutoStore()
        self.out = io.StringIO()
        self.now = NOW
        self.plan_codes = [EXIT_OK]
        self.next_codes: list[int] = []
        self.next_calls: list[bool] = []
        self.plan_calls = 0
        self.pr_result = (EXIT_OK, "https://example.test/pr/1\nMerged: https://example.test/pr/1\n")
        self.pr_calls: list[bool] = []
        self.waits: list[str] = []
        self.wait_result = "ready"
        self.after_merge_result: str | None = None
        self.on_next = None

    @property
    def plan_path(self) -> Path:
        return self.repo / ".agent-plan.md"

    def write_plan(self, s1="[ ]", s2="[ ]") -> None:
        self.plan_path.write_text(PLAN.format(s1=s1, s2=s2))

    def complete_first_open(self) -> None:
        text = self.plan_path.read_text()
        self.plan_path.write_text(text.replace("[ ]", "[x] – abc1234", 1))

    def plan(self) -> int:
        self.plan_calls += 1
        code = self.plan_codes.pop(0)
        if code == EXIT_OK:
            self.write_plan()
        return code

    def next(self, resume: bool) -> int:
        self.next_calls.append(resume)
        code = self.next_codes.pop(0) if self.next_codes else EXIT_OK
        if self.on_next:
            self.on_next(len(self.next_calls))
        if code == EXIT_OK:
            self.complete_first_open()
        return code

    def pr(self, merge: bool):
        self.pr_calls.append(merge)
        return self.pr_result

    def wait(self, role, size):
        self.waits.append(role)
        return self.wait_result

    def run(self, *, merge=True, budget=None):
        hooks = MilestoneHooks(
            plan=self.plan,
            next=self.next,
            pr=self.pr,
            wait=self.wait,
            now=lambda: self.now,
            after_merge=lambda repo, plan: self.after_merge_result,
        )
        return run_milestone(
            self.repo, hooks, self.store, budget or Budget(NOW), merge=merge, out=self.out
        )


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_plans_runs_every_step_publishes_and_merges(world):
    result = world.run()
    assert (result.outcome, result.milestone, result.pr_url) == (
        "merged",
        "M1",
        "https://example.test/pr/1",
    )
    assert world.plan_calls == 1
    assert world.next_calls == [False, False]
    assert world.pr_calls == [True]
    state = world.store.read(world.repo)
    assert state.milestone == "" and state.steps_done == 2 and state.step == 0


def test_adopts_an_open_plan_without_planning(world):
    world.write_plan(s1="[x] – abc1234")
    result = world.run()
    assert result.outcome == "merged"
    assert world.plan_calls == 0 and len(world.next_calls) == 1


def test_finished_plan_not_done_in_base_roadmap_goes_straight_to_pr(world):
    world.write_plan(s1="[x] – abc1234", s2="[x] – def5678")
    assert world.run().outcome == "merged"  # lost auto.json: derived from plan + roadmap
    assert world.plan_calls == 0 and world.next_calls == [] and world.pr_calls == [True]


def test_finished_plan_of_a_done_milestone_is_a_leftover(world):
    world.write_plan(s1="[x] – abc1234", s2="[x] – def5678")
    done = ROADMAP.replace("### M1 [ ]", "### M1 [x]")
    (world.repo / "docs" / "ROADMAP.md").write_text(done)
    assert world.run().outcome == "merged"
    assert world.plan_calls == 1 and world.pr_calls == [True]


def test_local_roadmap_milestone_is_marked_done_after_merge(world):
    text = PLAN.replace("tracked)", "local)").format(s1="[ ]", s2="[ ]")
    world.plan_path.write_text(text)
    assert world.run().outcome == "merged"
    roadmap = (world.repo / "docs" / "ROADMAP.md").read_text()
    assert "### M1 [x] Widgets" in roadmap


def test_no_merge_stops_after_publication(world):
    result = world.run(merge=False)
    assert result.outcome == "published" and result.pr_url == "https://example.test/pr/1"
    assert world.pr_calls == [False]
    assert world.store.read(world.repo).milestone == "M1"


def test_roadmap_finished(world):
    done = ROADMAP.replace("### M1 [ ]", "### M1 [x]").replace("### M2 [ ]", "### M2 [x]")
    (world.repo / "docs" / "ROADMAP.md").write_text(done)
    assert world.run().outcome == "done"
    assert world.plan_calls == 0


def test_blocked_dependencies_are_a_stop(world):
    blocked = ROADMAP.replace("### M0 [x]", "### M0 [~]")
    (world.repo / "docs" / "ROADMAP.md").write_text(blocked)
    result = world.run()
    assert result.outcome == "stopped" and "dependencies" in result.reason
    assert world.plan_calls == 0


def test_stop_escalation_exhausted(world):
    world.next_codes = [EXIT_OK, EXIT_FAILED]
    result = world.run()
    assert result.outcome == "stopped"
    assert "step 2 failed after escalation" in result.reason
    assert world.pr_calls == []


def test_stop_merge_refused(world):
    world.pr_result = (1, "https://example.test/pr/1\nStopped: critical finding\n")
    result = world.run()
    assert result.outcome == "stopped"
    assert "pull request not merged: Stopped: critical finding" in result.reason
    assert world.store.read(world.repo).milestone == "M1"  # resumable


def test_stop_roadmap_problem_from_implementer(world):
    world.next_codes = [EXIT_ROADMAP_PROBLEM]
    result = world.run()
    assert result.outcome == "stopped" and "roadmap problem" in result.reason


def test_stop_roadmap_problem_from_planner(world):
    world.plan_codes = [EXIT_ROADMAP_PROBLEM]
    result = world.run()
    assert result.outcome == "stopped" and "roadmap problem" in result.reason
    assert world.next_calls == []


def test_planning_failure_is_a_stop(world):
    world.plan_codes = [EXIT_STOPPED]
    assert "planning failed" in world.run().reason


def test_stop_max_steps(world):
    result = world.run(budget=Budget(NOW, max_steps=1))
    assert result.outcome == "stopped" and "budget reached: 1 steps" in result.reason
    assert world.pr_calls == [] and len(world.next_calls) == 1


def test_stop_max_hours(world):
    def later(call):
        world.now += timedelta(hours=2)

    world.on_next = later
    result = world.run(budget=Budget(NOW, max_hours=1.5))
    assert result.outcome == "stopped" and "budget reached: 1.5 hours" in result.reason
    assert len(world.next_calls) == 1


def test_stop_request_between_steps(world):
    world.on_next = lambda call: world.store.update(world.repo, stop_requested=True)
    result = world.run()
    assert result.outcome == "stopped" and result.reason == "stop requested"
    assert len(world.next_calls) == 1


def test_stop_request_during_wait(world):
    world.next_codes = [EXIT_EXHAUSTED]
    world.wait_result = "stopped"
    result = world.run()
    assert result.outcome == "stopped" and "while waiting" in result.reason


def test_exhaustion_waits_then_resumes_the_same_step(world):
    world.next_codes = [EXIT_EXHAUSTED]
    result = world.run()
    assert result.outcome == "merged"
    assert world.waits == ["implement"]
    assert world.next_calls == [False, True, False]  # retry of step 1 resumes; step 2 is fresh


def test_planner_exhaustion_waits_then_replans(world):
    world.plan_codes = [EXIT_EXHAUSTED, EXIT_OK]
    assert world.run().outcome == "merged"
    assert world.plan_calls == 2 and world.waits == ["plan_commits"]


def test_recorded_step_in_progress_resumes_after_a_crash(world):
    world.write_plan()
    world.store.update(world.repo, milestone="M1", step=1)
    assert world.run().outcome == "merged"
    assert world.next_calls[0] is True


def test_step_without_progress_is_a_stop(world):
    world.next_codes = [EXIT_OK]
    world.complete_first_open = lambda: None
    result = world.run()
    assert "made no progress" in result.reason


def test_step_not_started_is_a_stop(world):
    world.next_codes = [EXIT_STOPPED]
    world.complete_first_open = lambda: None
    result = world.run()
    assert result.outcome == "stopped" and "was not started (exit 2)" in result.reason


def test_committed_pending_step_is_settled_by_the_gate(world):
    world.write_plan(s1="[x] – abc1234", s2="[~] (on abc1234)")
    world.next_codes = [EXIT_STOPPED]  # the gate marks it done, then no open step is left
    world.on_next = lambda call: world.plan_path.write_text(
        world.plan_path.read_text().replace("[~] (on abc1234)", "[x] – def5678")
    )
    assert world.run().outcome == "merged"
    assert len(world.next_calls) == 1


def test_dirty_base_after_merge_is_a_stop(world):
    world.after_merge_result = "the base checkout has uncommitted changes: x"
    result = world.run()
    assert result.outcome == "stopped" and "uncommitted changes" in result.reason
    assert world.store.read(world.repo).milestone == "M1"


# --- ff_base with real repositories -------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def clone(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "a").write_text("1")
    git(work, "add", ".")
    git(work, "commit", "-qm", "base")
    git(work, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(origin), str(other))
    return work, other


def advance_origin(other: Path, text: str = "2") -> None:
    (other / "a").write_text(text)
    git(other, "commit", "-qam", "merge")
    git(other, "push", "-q", "origin", "main")


def plan_for(base="main"):
    return parse_plan(
        PLAN.format(s1="[x] – a", s2="[x] – b").replace("Branch: main", f"Branch: {base}")
    )


def test_ff_base_fast_forwards_and_removes_the_worktree(clone):
    from baton.git import milestone_branch, worktree_add, worktree_path_for

    work, other = clone
    plan = plan_for()
    path = worktree_path_for(work, milestone_branch(plan))
    worktree_add(work, path, milestone_branch(plan), create=True)
    advance_origin(other)
    assert ff_base(work, plan) is None
    assert (work / "a").read_text() == "2"
    assert not path.exists()


def test_ff_base_refuses_dirty_checkout(clone):
    work, other = clone
    advance_origin(other)
    (work / "stray").write_text("x")
    assert "uncommitted changes: stray" in ff_base(work, plan_for())
    assert (work / "a").read_text() == "1"


def test_ff_base_refuses_diverged_base(clone):
    work, other = clone
    (work / "a").write_text("local")
    git(work, "commit", "-qam", "local")
    advance_origin(other)
    assert "cannot fast-forward main" in ff_base(work, plan_for())
    assert (work / "a").read_text() == "local"


def test_ff_base_requires_base_branch_checked_out(clone):
    work, _ = clone
    git(work, "switch", "-qc", "other")
    assert "not on the base branch main" in ff_base(work, plan_for())


# --- the loop: baton auto -------------------------------------------------------------------

CONFIG = '[agents.a]\nadapter = "claude"\ncommand = "claude"\n'


class Loop(World):
    """World whose planner follows the roadmap and whose merge updates it, like origin would."""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.config = tmp_path / "config.toml"
        self.config.write_text(CONFIG)
        self.notes: list[tuple[str, str]] = []
        self.merged: list[str] = []

    def plan(self) -> int:
        from baton.roadmap import next_milestone, parse_roadmap

        self.plan_calls += 1
        path = self.repo / "docs" / "ROADMAP.md"
        milestone = next_milestone(parse_roadmap(path.read_text()))
        text = PLAN.format(s1="[ ]", s2="[ ]").replace("M1 – Widgets", f"{milestone.id} – Widgets")
        self.plan_path.write_text(text)
        return EXIT_OK

    def finish(self, repo, plan):
        from baton.roadmap import save_roadmap, set_milestone_status

        path = repo / "docs" / "ROADMAP.md"
        save_roadmap(path, set_milestone_status(path.read_text(), plan.milestone.id, "done"))
        self.merged.append(plan.milestone.id)
        return self.after_merge_result

    def auto(self, **kw):
        from baton.auto import run_auto_command

        hooks = MilestoneHooks(
            plan=self.plan,
            next=self.next,
            pr=self.pr,
            wait=self.wait,
            now=lambda: self.now,
            after_merge=self.finish,
        )
        return run_auto_command(
            self.repo,
            config_path=str(self.config),
            out=self.out,
            hooks=hooks,
            notifier=lambda title, message: self.notes.append((title, message)),
            now=lambda: self.now,
            **kw,
        )


@pytest.fixture
def loop(tmp_path, monkeypatch):
    return Loop(tmp_path, monkeypatch)


def test_auto_runs_the_whole_roadmap_and_notifies(loop):
    assert loop.auto() == 0
    assert loop.merged == ["M1", "M2"]
    assert [title for title, _ in loop.notes] == [
        "baton: M1 merged",
        "baton: M2 merged",
        "baton: roadmap complete",
    ]
    assert loop.store.read(loop.repo).steps_done == 4


def test_auto_notifies_every_stop(loop):
    loop.next_codes = [EXIT_FAILED]
    assert loop.auto() == 1
    assert loop.notes == [
        ("baton: stopped", "M1: step 1 failed after escalation (exit code 1); see the plan log")
    ]


def test_auto_budget_spans_milestones(loop):
    assert loop.auto(max_steps=3) == 1
    assert loop.merged == ["M1"]
    assert "budget reached: 3 steps" in loop.notes[-1][1]


def test_auto_no_merge_stops_with_a_notification(loop):
    assert loop.auto(merge=False) == 0
    assert loop.merged == []
    assert loop.notes == [("baton: M1 PR ready", "PR ready for review. https://example.test/pr/1")]


def test_auto_clears_a_stale_stop_request(loop):
    loop.store.update(loop.repo, stop_requested=True)
    assert loop.auto() == 0


@pytest.mark.parametrize("bad", [{"max_steps": 0}, {"max_hours": 0}])
def test_auto_rejects_bad_budget(loop, bad):
    assert loop.auto(**bad) == 2
    assert "Cannot start" in loop.out.getvalue()


def test_auto_requires_a_git_repository(tmp_path, monkeypatch):
    from baton.auto import run_auto_command

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    out = io.StringIO()
    assert run_auto_command(tmp_path / "nowhere", out=out) == 2
    assert "Cannot start" in out.getvalue()


def test_resume_mid_step_with_partial_diff(loop):
    loop.write_plan()
    loop.store.update(loop.repo, milestone="M1", step=1)
    assert loop.auto() == 0
    assert loop.next_calls[0] is True and loop.plan_calls == 1  # M2 is planned afresh


def test_resume_with_next_step_pending(loop):
    loop.write_plan(s1="[x] – abc1234")
    assert loop.auto() == 0
    assert loop.next_calls[0] is False and loop.merged[0] == "M1"


def test_resume_plan_done_not_published(loop):
    loop.write_plan(s1="[x] – abc1234", s2="[x] – def5678")
    assert loop.auto() == 0
    assert loop.next_calls[:2] == [False, False] and loop.merged[0] == "M1"
    assert loop.pr_calls[0] is True


def test_resume_with_open_pr_merges_it(loop):
    loop.write_plan(s1="[x] – abc1234", s2="[x] – def5678")
    loop.store.update(loop.repo, milestone="M1", pr_url="https://example.test/pr/1")
    assert loop.auto() == 0
    assert loop.merged[0] == "M1" and loop.pr_calls[0] is True


def test_resume_after_merge_before_fast_forward(loop):
    loop.write_plan(s1="[x] – abc1234", s2="[x] – def5678")
    loop.pr_result = (EXIT_OK, "Already merged: https://example.test/pr/1\n")
    assert loop.auto() == 0
    assert loop.merged[0] == "M1"


@pytest.mark.parametrize("planning", [False, True])
def test_daily_budget_stops_auto_with_resume_diagnostic(world, planning):
    diagnostic = (
        "Daily run budget exhausted; resets at 2026-11-19T00:00:00+00:00 (UTC). Resume after reset."
    )
    world.wait_result = diagnostic
    if planning:
        world.plan_codes = [EXIT_EXHAUSTED]
    else:
        world.write_plan()
        world.next_codes = [EXIT_EXHAUSTED]
    result = world.run()
    assert result.outcome == "stopped"
    assert result.reason == diagnostic
    assert not world.pr_calls


def test_real_wait_detects_mixed_budget_and_cooldown(world):
    from baton.auto import default_hooks
    from baton.config import AgentConfig, Config, ConfigLocation
    from baton.limits import LimitStore
    from baton.runs import RunStore

    a = AgentConfig("a", "claude", "fake", daily_run_budget=1)
    b = AgentConfig("b", "codex", "fake")
    config = Config(
        ConfigLocation(world.repo / "config.toml", "flag"),
        agents={"a": a, "b": b},
        routes={"implement": {"M": ["a", "b"]}},
    )
    RunStore().dispatch("a", now=NOW)
    LimitStore().set("b", NOW + timedelta(hours=1))

    def no_sleep(_):
        pytest.fail("budget exhaustion should stop without waiting")

    hooks = default_hooks(
        world.repo,
        config,
        None,
        agent_timeout=10,
        verify_timeout=10,
        now=lambda: NOW,
        sleep=no_sleep,
        store=world.store,
        out=world.out,
    )
    assert "Daily run budget exhausted" in hooks.wait("implement", "M")


@pytest.mark.parametrize("merge", [True, False])
def test_stop_in_final_step_prevents_publication_and_can_resume(world, merge):
    world.write_plan(s1="[x] – abc1234")
    world.on_next = lambda _: world.store.update(world.repo, stop_requested=True)
    result = world.run(merge=merge)
    assert result.outcome == "stopped"
    assert result.reason == "stop requested"
    assert not world.pr_calls
    world.store.update(world.repo, stop_requested=False)
    result = world.run(merge=merge)
    assert result.outcome == ("merged" if merge else "published")
    assert len(world.next_calls) == 1


def test_exhausted_budget_prevents_new_planning(world):
    result = world.run(budget=Budget(NOW, max_steps=1, steps=1))
    assert result.outcome == "stopped"
    assert "budget reached" in result.reason
    assert world.plan_calls == 0


def test_planner_retry_checks_elapsed_budget(world):
    world.plan_codes = [EXIT_EXHAUSTED]

    def wait(role, size):
        world.now += timedelta(hours=1)
        return "ready"

    world.wait = wait
    result = world.run(budget=Budget(NOW, max_hours=1))
    assert "budget reached" in result.reason
    assert world.plan_calls == 1


def test_last_allowed_step_finalizes_before_next_planning(world):
    world.write_plan(s1="[x] – abc1234")
    budget = Budget(NOW, max_steps=1)
    assert world.run(budget=budget).outcome == "merged"
    assert world.pr_calls == [True]
    world.plan_path.unlink()
    result = world.run(budget=budget)
    assert "budget reached" in result.reason
    assert world.plan_calls == 0


@pytest.mark.parametrize("role", ["implement", "plan_commits"])
def test_periodic_recheck_uses_active_agent_config(world, monkeypatch, role):
    from baton.auto import default_hooks
    from baton.config import AgentConfig, AutoConfig, Config, ConfigLocation
    from baton.limits import LimitStore
    from baton.recording import Recording
    from baton.runs import RunStore

    agent = AgentConfig("custom", "claude", "custom-command", model="custom-model")
    config = Config(
        ConfigLocation(world.repo / "config.toml", "flag"),
        agents={"custom": agent},
        routes={role: {"M": ["custom"]}},
        auto=AutoConfig(recheck_interval_seconds=30),
    )
    reset = NOW + timedelta(hours=2)
    LimitStore().set("custom", reset)
    probes = []

    def probe(selected, **kwargs):
        probes.append(selected)
        return Recording(
            adapter="claude",
            agent="custom",
            argv=[],
            tty=False,
            exit_code=0,
            timed_out=False,
            duration_s=0,
            stdout='{"type":"result","subtype":"success","result":"OK","is_error":false}',
            stderr="",
            recorded_at=NOW.isoformat(),
            cli_version="fake",
        )

    monkeypatch.setattr("baton.probe.run_probe", probe)

    def sleep(seconds):
        world.now += timedelta(seconds=seconds)

    hooks = default_hooks(
        world.repo,
        config,
        None,
        agent_timeout=10,
        verify_timeout=10,
        now=lambda: world.now,
        sleep=sleep,
        store=world.store,
        out=world.out,
        budget=Budget(NOW, max_hours=1),
    )
    assert hooks.wait(role, "M") == "ready"
    assert probes == [agent]
    assert world.now == NOW + timedelta(seconds=30)
    assert "custom" not in LimitStore().active(now=world.now)
    assert RunStore().read() == []


def test_periodic_recheck_skips_agent_capped_during_wait(world, monkeypatch):
    from baton.auto import default_hooks
    from baton.config import AgentConfig, AutoConfig, Config, ConfigLocation
    from baton.limits import LimitStore
    from baton.runs import RunStore

    agent = AgentConfig("a", "claude", "fake", daily_run_budget=1)
    config = Config(
        ConfigLocation(world.repo / "config.toml", "flag"),
        agents={"a": agent},
        routes={"implement": {"M": ["a"]}},
        auto=AutoConfig(recheck_interval_seconds=30),
    )
    LimitStore().set("a", NOW + timedelta(hours=1))
    probes = []
    monkeypatch.setattr("baton.probe.run_probe", lambda *a, **kw: probes.append(a))

    def sleep(seconds):
        world.now += timedelta(seconds=seconds)
        RunStore().dispatch("a", now=world.now)

    hooks = default_hooks(
        world.repo,
        config,
        None,
        agent_timeout=10,
        verify_timeout=10,
        now=lambda: world.now,
        sleep=sleep,
        store=world.store,
        out=world.out,
    )
    assert "Daily run budget exhausted" in hooks.wait("implement", "M")
    assert probes == []
