import json
import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from baton.limits import LimitStateError, LimitStore, limit_state_path, parse_duration

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)


def test_state_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert limit_state_path() == tmp_path / "state/baton/limits.json"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert limit_state_path() == tmp_path / ".local/state/baton/limits.json"


def test_persistence_expiration_and_preservation(tmp_path):
    path = tmp_path / "state/limits.json"
    store = LimitStore(path)
    assert store.active(now=NOW) == {}
    assert not path.parent.exists()
    store.set("a", NOW + parse_duration("2h"))
    store.set("b", NOW + parse_duration("3h"))
    fresh = LimitStore(path)
    assert fresh.active(now=NOW) == {
        "a": NOW + timedelta(hours=2),
        "b": NOW + timedelta(hours=3),
    }
    assert fresh.active(now=NOW + timedelta(hours=2)) == {"b": NOW + timedelta(hours=3)}
    assert fresh.active(now=NOW + timedelta(hours=3)) == {}
    assert json.loads(path.read_text())["a"].endswith("+00:00")


@pytest.mark.parametrize("value", ["0h", "-1h", "2", "2d", "1.5h", "", "infh", "9" * 5000 + "h"])
def test_bad_duration(value):
    with pytest.raises(ValueError, match="duration"):
        parse_duration(value)


@pytest.mark.parametrize("value, seconds", [("2h", 7200), ("30m", 1800), ("1s", 1)])
def test_duration(value, seconds):
    assert parse_duration(value).total_seconds() == seconds


@pytest.mark.parametrize(
    "raw", ["{", "[]", '{"a": 1}', '{"a": "tomorrow"}', '{"a": "2026-11-18T12:00:00"}', '{"": "x"}']
)
def test_bad_state_preserved(tmp_path, raw):
    path = tmp_path / "limits.json"
    path.write_text(raw)
    with pytest.raises(LimitStateError, match="Repair or move"):
        LimitStore(path).set("b", NOW)
    assert path.read_text() == raw
    assert not list(tmp_path.glob("*.tmp"))


def test_unreadable_state_preserved(tmp_path, monkeypatch):
    path = tmp_path / "limits.json"
    path.write_bytes(b"\xff")
    with pytest.raises(LimitStateError, match="Cannot read"):
        LimitStore(path).set("b", NOW)
    assert path.read_bytes() == b"\xff"
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(LimitStateError, match="permissions"):
        LimitStore(path).set("b", NOW)
    assert path.read_bytes() == b"\xff"


def test_atomic_write_failure_preserves_previous_state(tmp_path, monkeypatch):
    path = tmp_path / "limits.json"
    store = LimitStore(path)
    store.set("a", NOW)
    before = path.read_bytes()

    def fail_replace(source, target):
        # The old complete version remains available while the new one is written.
        assert path.read_bytes() == before
        assert json.loads(Path(source).read_text())["b"] == NOW.isoformat()
        raise OSError("disk error")

    monkeypatch.setattr("baton.limits.os.replace", fail_replace)
    with pytest.raises(LimitStateError, match="free space"):
        store.set("b", NOW)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def _concurrent_writer(path, number, ready):
    ready.wait(10)
    LimitStore(path).set(str(number), NOW)


def test_concurrent_updates_preserve_all_agents(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    path = tmp_path / "limits.json"
    processes = [ctx.Process(target=_concurrent_writer, args=(path, i, ready)) for i in range(8)]
    for process in processes:
        process.start()
    ready.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert LimitStore(path).read() == {str(i): NOW for i in range(8)}


def test_clear_compares_observed_deadline_and_preserves_other_agents(tmp_path):
    store = LimitStore(tmp_path / "limits.json")
    store.set("a", NOW)
    store.set("b", NOW)
    newer = NOW + timedelta(hours=1)
    store.set("a", newer)
    assert not store.clear_if("a", NOW)
    assert store.read() == {"a": newer, "b": NOW}
    assert store.clear_if("a", newer)
    assert store.read() == {"b": NOW}
