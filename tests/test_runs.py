from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from baton.adapter import AgentResult
from baton.limits import LimitStateError
from baton.runs import RunStore


def test_missing_read_is_read_only(tmp_path):
    store = RunStore(tmp_path / "absent" / "runs.jsonl")
    assert store.read() == []
    assert not store.path.parent.exists()


def test_concurrent_dispatches_and_interruption(tmp_path):
    store = RunStore(tmp_path / "runs.jsonl")
    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(lambda _: store.dispatch("agent"), range(30)))
    store.complete(ids[0], AgentResult(ok=False, tokens=0, duration_s=1.5))
    rows = store.read()
    assert len(rows) == 31
    assert len(set(ids)) == 30
    assert all(datetime.fromisoformat(r["at"]).tzinfo == UTC for r in rows[:-1])
    assert rows[-1]["tokens"] == 0


def test_invalid_journal_has_line_diagnostic(tmp_path):
    store = RunStore(tmp_path / "runs.jsonl")
    store.path.write_text("{}\n")
    with pytest.raises(LimitStateError, match="runs.jsonl:1"):
        store.dispatch("agent")
    assert store.path.read_text() == "{}\n"


def test_atomic_budget_and_utc_rollover(tmp_path):
    from datetime import timedelta

    from baton.config import AgentConfig

    store = RunStore(tmp_path / "runs.jsonl")
    agent = AgentConfig("limited", "claude", "claude", daily_run_budget=1)
    now = datetime(2026, 11, 18, tzinfo=UTC)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: store.select([agent], claim=True, now=now), range(2)))
    assert sum(a is not None for a, _ in results) == 1
    assert store.select([agent], now=now) == (None, None)
    assert store.select([agent], now=now + timedelta(days=1))[0] == agent
    assert len(store.read()) == 1


def _journal(tmp_path, count=2):
    store = RunStore(tmp_path / "runs.jsonl")
    ids = [store.dispatch("agent") for _ in range(count)]
    return store, ids


def test_torn_tail_is_ignored_on_read(tmp_path):
    store, _ = _journal(tmp_path)
    with store.path.open("a") as stream:
        stream.write('{"event": "dispatch", "id": "x", "ag')
    assert len(store.read()) == 2


def test_append_after_torn_tail_repairs_the_journal(tmp_path):
    store, ids = _journal(tmp_path)
    with store.path.open("a") as stream:
        stream.write('{"event": "dispatch", "id": "x", "ag')
    agent, ident = store.select([_agent("agent")], claim=True)
    assert agent is not None and ident
    store.complete(ident, AgentResult(ok=True, tokens=1, duration_s=1.0))
    rows = store.read()
    assert [r["id"] for r in rows][:2] == ids and len(rows) == 4
    text = store.path.read_text()
    assert text.endswith("\n") and "ag\n" not in text
    assert all(line.startswith("{") and line.endswith("}") for line in text.splitlines())


def test_valid_last_line_without_newline_is_kept(tmp_path):
    store, ids = _journal(tmp_path)
    store.path.write_text(store.path.read_text().rstrip("\n"))
    assert len(store.read()) == 2
    store.dispatch("agent")
    assert len(store.read()) == 3 and store.path.read_text().count("\n") == 3


def test_corrupt_middle_line_still_fails_with_line_number(tmp_path):
    store, _ = _journal(tmp_path)
    lines = store.path.read_text().splitlines()
    store.path.write_text("\n".join([lines[0], "{broken", lines[1]]) + "\n")
    with pytest.raises(LimitStateError, match=r"runs.jsonl:2"):
        store.read()
    with pytest.raises(LimitStateError):
        store.dispatch("agent")


def test_corrupt_terminated_last_line_still_fails(tmp_path):
    store, _ = _journal(tmp_path)
    with store.path.open("a") as stream:
        stream.write("{broken\n")
    with pytest.raises(LimitStateError, match=r"runs.jsonl:3"):
        store.read()


def _agent(name):
    from baton.config import AgentConfig

    return AgentConfig(name, "claude", "claude")


def test_stop_events_are_journaled_and_ignored_by_readers(tmp_path):
    from baton.runs import summarize

    store = RunStore(tmp_path / "runs.jsonl")
    ident = store.dispatch("agent")
    store.complete(ident, AgentResult(ok=True, tokens=5, duration_s=1.0))
    before = summarize(store.read())
    store.record_stop("budget reached", "recoverable")
    assert '"event": "stop"' in store.path.read_text()
    assert summarize(store.read()) == before and len(store.read()) == 2
