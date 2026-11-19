from baton.adapter import AgentResult
from baton.cli import main
from baton.runs import RunStore


def test_empty_stats_is_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "missing"))
    assert main(["stats"]) == 0
    assert "No recorded attempts" in capsys.readouterr().out
    assert not (tmp_path / "missing").exists()


def test_partial_and_removed_agents(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = RunStore()
    ident = store.dispatch("removed")
    store.complete(ident, AgentResult(ok=True, tokens=0, duration_s=1.25))
    store.dispatch("removed")
    store.dispatch("unknown")
    assert main(["stats"]) == 0
    output = capsys.readouterr().out
    assert "removed  2  1.250 (partial; 1 unknown)  0 (partial; 1 unknown)" in output
    assert "unknown  1  unknown  unknown" in output


def test_invalid_stats(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = RunStore()
    store.path.parent.mkdir()
    store.path.write_text("bad\n")
    assert main(["stats"]) == 1
    assert "runs.jsonl:1" in capsys.readouterr().err


def test_weighted_cli_sequence(tmp_path, monkeypatch, capsys):
    import io

    from baton.config import AgentConfig
    from baton.limits import LimitStore
    from baton.routed import RoutedAdapter
    from baton.router import Route

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr("baton.routed.head", lambda _: "same-head")

    class Fake:
        def run(self, *args):
            return AgentResult(ok=True, duration_s=2, tokens=10)

    monkeypatch.setattr("baton.routed.make_adapter", lambda _, read_only=False: Fake())
    a = AgentConfig("a", "claude", "fake", daily_run_budget=4, weight=2)
    b = AgentConfig("b", "codex", "fake", weight=1)
    route = Route("M", (a, a, b))
    for _ in range(6):
        runner = RoutedAdapter(route, LimitStore(), io.StringIO())
        assert runner.run("prompt", tmp_path, 10).ok
    assert main(["stats"]) == 0
    output = capsys.readouterr().out
    assert "a  4  8.000  40" in output
    assert "b  2  4.000  20" in output
    pinned = RoutedAdapter(Route("M", (a,)), LimitStore(), io.StringIO())
    assert not pinned.run("prompt", tmp_path, 10).ok
    assert "UTC" in pinned._exhaustion_message()
