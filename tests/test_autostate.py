import json
import multiprocessing
from pathlib import Path

import pytest

from baton.autostate import AutoState, AutoStateError, AutoStore, auto_state_path


def test_state_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert auto_state_path() == tmp_path / "state/baton/auto.json"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert auto_state_path() == tmp_path / ".local/state/baton/auto.json"


def test_round_trip_is_keyed_by_repo(tmp_path):
    store = AutoStore(tmp_path / "state/auto.json")
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    assert store.read(repo_a) == AutoState()
    assert not store.path.parent.exists()
    store.update(repo_a, milestone="M1", step=2, branch="feat/x", steps_done=1)
    store.update(repo_a, pr_url="https://example.test/pr/1")
    fresh = AutoStore(store.path)
    assert fresh.read(repo_a) == AutoState(
        milestone="M1", step=2, branch="feat/x", pr_url="https://example.test/pr/1", steps_done=1
    )
    assert fresh.read(repo_b) == AutoState()
    assert fresh.read(repo_a / "..") == AutoState()
    fresh.reset(repo_a)
    assert fresh.read(repo_a) == AutoState()


def test_unknown_field_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown"):
        AutoStore(tmp_path / "auto.json").update(tmp_path, nope=1)


@pytest.mark.parametrize(
    "raw",
    ["{", "[]", '{"r": 1}', '{"r": {"x": 1}}', '{"r": {"step": true}}', '{"r": {"step": "1"}}'],
)
def test_corrupt_state_preserved(tmp_path, raw):
    path = tmp_path / "auto.json"
    path.write_text(raw)
    store = AutoStore(path)
    with pytest.raises(AutoStateError, match="Repair or move"):
        store.read(tmp_path)
    with pytest.raises(AutoStateError, match="Repair or move"):
        store.update(tmp_path, step=1)
    assert path.read_text() == raw
    assert not list(tmp_path.glob("*.tmp"))


def _bump(path, repo):
    for _ in range(20):
        AutoStore(path).update(repo, milestone="M1")
    store = AutoStore(path)
    store.update(repo, stop_requested=True)


def test_concurrent_updates_keep_every_repo(tmp_path):
    path = tmp_path / "auto.json"
    repos = [tmp_path / f"r{i}" for i in range(4)]
    for repo in repos:
        repo.mkdir()
    procs = [multiprocessing.Process(target=_bump, args=(path, repo)) for repo in repos]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
        assert proc.exitcode == 0
    data = json.loads(path.read_text())
    assert len(data) == 4
    assert all(state["stop_requested"] and state["milestone"] == "M1" for state in data.values())


def test_baton_stop_sets_request(tmp_path, monkeypatch, capsys):
    from baton.cli import main

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    assert main(["stop", "--path", str(repo)]) == 0
    assert "Stop requested" in capsys.readouterr().out
    assert AutoStore().read(repo).stop_requested is True


def test_baton_stop_corrupt_state_fails(tmp_path, monkeypatch, capsys):
    from baton.cli import main

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state/baton").mkdir(parents=True)
    (tmp_path / "state/baton/auto.json").write_text("{")
    assert main(["stop", "--path", str(tmp_path)]) == 1
    assert "Cannot request stop" in capsys.readouterr().out
