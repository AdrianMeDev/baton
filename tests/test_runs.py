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
