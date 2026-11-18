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
