"""``baton auto --roadmap``: the loop works through another roadmap file, offline."""
# ruff: noqa: F811  (the imported `world` fixture is used as a test argument)

import json

from test_auto import PLAN, World
from test_auto_acceptance import FAKE_PLANNER, git, script, world  # noqa: F401

from baton.auto import MilestoneHooks, run_auto_command, run_milestone
from baton.autostate import AutoStore
from baton.next import EXIT_OK
from baton.roadmap import parse_roadmap

AUDIT = """# Audit: demo
Updated: 2026-11-20 · Base: abc1234

## Findings

F1 · critical · security · a.py:1 – eval
F2 · warning · smell · b.py – long

## Milestones

### M1 [ ] Audit: First
- **Goal:** Fix the eval.
- **Includes:** F1
- **Done when:** eval is gone
- **Depends on:** -
- **Size:** S, ~3 commits

### M2 [ ] Audit: Second
- **Goal:** Split the function.
- **Includes:** F2
- **Done when:** function is short
- **Depends on:** M1
- **Size:** S, ~3 commits

## Suggestions
- Add CI.

## Definition of done
- tests green

## Changelog
- 2026-11-20: audit planned
"""

AUDIT_PLANNER = FAKE_PLANNER.replace(
    "mid, name = m[1], m[2]",
    'mid, name = m[1], m[2]\npath = re.search(r"roadmap `([^`]+)`", prompt)[1]',
).replace("(docs/ROADMAP.md, tracked)", "({{path}}, tracked)")


def test_auto_works_through_the_audit_file_and_leaves_the_roadmap_alone(world, tmp_path):
    repo, bare, state, config = world
    roadmap_before = (repo / "docs" / "ROADMAP.md").read_text()
    (repo / "docs" / "AUDIT.md").write_text(AUDIT)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "audit")
    git(repo, "push", "-q", "origin", "main")
    script(tmp_path / "bin" / "fake-planner", AUDIT_PLANNER)

    out = tmp_path / "out.txt"
    with out.open("w") as stream:
        code = run_auto_command(
            repo,
            config_path=str(config),
            roadmap="docs/AUDIT.md",
            out=stream,
            notifier=lambda *_: None,
        )
    assert code == 0, out.read_text()

    audit = parse_roadmap(git(bare, "show", "main:docs/AUDIT.md"))
    assert [m.status for m in audit.milestones] == ["done", "done"]
    assert git(bare, "show", "main:docs/ROADMAP.md") == roadmap_before
    prs = json.loads(state.read_text())["prs"]
    assert [(p["head"], p["merged"]) for p in prs] == [
        ("baton/m1-audit-first", True),
        ("baton/m2-audit-second", True),
    ]
    assert git(repo, "status", "--porcelain").strip() == ""


def test_auto_rejects_a_missing_or_escaping_roadmap(world, capsys):
    repo, _, _, config = world
    for bad in ("docs/NOPE.md", "../x.md", "/etc/passwd"):
        code = run_auto_command(
            repo, config_path=str(config), roadmap=bad, notifier=lambda *_: None
        )
        assert code == 2
    out = capsys.readouterr().out
    assert "roadmap docs/NOPE.md not found" in out
    assert out.count("Cannot start") == 3


# -- run_milestone with injected hooks (the World of test_auto.py) -----------------------------


def _audit_world(tmp_path, monkeypatch) -> World:
    world = World(tmp_path, monkeypatch)
    (world.repo / "docs" / "AUDIT.md").write_text(AUDIT)
    return world


def _run(world: World, roadmap: str | None):
    hooks = MilestoneHooks(
        plan=world.plan,
        next=world.next,
        pr=world.pr,
        wait=world.wait,
        now=lambda: world.now,
        after_merge=lambda repo, plan: None,
    )
    from baton.auto import Budget

    return run_milestone(
        world.repo, hooks, world.store, Budget(world.now), roadmap=roadmap, out=world.out
    )


def test_open_plan_of_the_other_roadmap_is_not_continued(tmp_path, monkeypatch):
    world = _audit_world(tmp_path, monkeypatch)
    world.write_plan(s1="[x] – abc1234")  # belongs to docs/ROADMAP.md
    result = _run(world, "docs/AUDIT.md")
    assert result.outcome == "stopped"
    assert "belongs to docs/ROADMAP.md, not docs/AUDIT.md" in result.reason
    assert world.plan_calls == 0 and world.next_calls == [] and world.pr_calls == []


def test_finished_plan_of_the_other_roadmap_is_a_leftover(tmp_path, monkeypatch):
    world = _audit_world(tmp_path, monkeypatch)
    world.write_plan(s1="[x] – abc1234", s2="[x] – def5678")  # M1 of docs/ROADMAP.md, not done
    result = _run(world, "docs/AUDIT.md")
    assert world.plan_calls == 1  # planned afresh instead of going straight to the PR
    assert result.outcome == "merged"


def test_default_run_still_adopts_a_finished_plan(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.write_plan(s1="[x] – abc1234", s2="[x] – def5678")
    assert _run(world, None).outcome == "merged"
    assert world.plan_calls == 0 and world.pr_calls == [True]


def test_matching_plan_of_the_explicit_roadmap_is_adopted(tmp_path, monkeypatch):
    world = _audit_world(tmp_path, monkeypatch)
    world.plan_path.write_text(
        PLAN.format(s1="[x] – abc1234", s2="[ ]").replace("docs/ROADMAP.md", "docs/AUDIT.md")
    )
    assert _run(world, "docs/AUDIT.md").outcome == "merged"
    assert world.plan_calls == 0 and len(world.next_calls) == 1


def test_plan_hook_gets_the_roadmap_path(tmp_path, monkeypatch):
    from baton import auto

    seen: dict = {}
    monkeypatch.setattr(auto, "run_plan_command", lambda repo, **kw: seen.update(kw) or EXIT_OK)
    world = World(tmp_path, monkeypatch)
    config = type("C", (), {"auto": None})()
    hooks = auto.default_hooks(
        world.repo,
        config,
        None,
        agent_timeout=5,
        verify_timeout=5,
        now=lambda: world.now,
        sleep=lambda s: None,
        store=AutoStore(),
        out=world.out,
        roadmap="docs/AUDIT.md",
    )
    hooks.plan()
    assert seen["roadmap"] == "docs/AUDIT.md"
