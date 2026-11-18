import io
from datetime import UTC, datetime, timedelta

from baton.autowait import CHUNK_SECONDS, wait_for_reset
from baton.limits import LimitStore

START = datetime(2026, 11, 18, 12, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.t = START
        self.sleeps = []
        self.hook = None

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)
        if self.hook:
            self.hook(len(self.sleeps))


def wait(tmp_path, clock, agents=("a", "b"), stop=lambda: False):
    out = io.StringIO()
    result = wait_for_reset(
        agents,
        LimitStore(tmp_path / "limits.json"),
        now=clock.now,
        sleep=clock.sleep,
        stop_requested=stop,
        out=out,
    )
    return result, out.getvalue()


def test_ready_immediately_when_an_agent_is_free(tmp_path):
    LimitStore(tmp_path / "limits.json").set("a", START + timedelta(hours=1))
    clock = Clock()
    assert wait(tmp_path, clock)[0] == "ready"
    assert clock.sleeps == []


def test_waits_until_earliest_deadline_in_chunks(tmp_path):
    store = LimitStore(tmp_path / "limits.json")
    store.set("a", START + timedelta(minutes=12))
    store.set("b", START + timedelta(hours=3))
    clock = Clock()
    result, out = wait(tmp_path, clock)
    assert result == "ready"
    assert clock.sleeps == [CHUNK_SECONDS, CHUNK_SECONDS, 120.0]
    assert clock.t == START + timedelta(minutes=12)
    assert out.count("a in cooldown, sign in with another account or wait until") == 1


def test_recheck_clearing_ends_wait_early(tmp_path):
    store = LimitStore(tmp_path / "limits.json")
    store.set("a", START + timedelta(hours=2))
    store.set("b", START + timedelta(hours=3))
    clock = Clock()
    clock.hook = lambda n: store.clear_if("a", START + timedelta(hours=2)) if n == 2 else None
    assert wait(tmp_path, clock)[0] == "ready"
    assert len(clock.sleeps) == 2


def test_stop_request_ends_wait(tmp_path):
    store = LimitStore(tmp_path / "limits.json")
    store.set("a", START + timedelta(hours=2))
    store.set("b", START + timedelta(hours=3))
    clock = Clock()
    result, _ = wait(tmp_path, clock, stop=lambda: len(clock.sleeps) >= 1)
    assert result == "stopped"
    assert len(clock.sleeps) == 1


def test_other_agents_cooldowns_are_ignored(tmp_path):
    LimitStore(tmp_path / "limits.json").set("other", START + timedelta(hours=1))
    assert wait(tmp_path, Clock(), agents=("a",))[0] == "ready"


def test_run_deadline_caps_cooldown_sleep(tmp_path):
    store = LimitStore(tmp_path / "limits.json")
    store.set("a", START + timedelta(hours=2))
    clock = Clock()
    result = wait_for_reset(
        ["a"],
        store,
        now=clock.now,
        sleep=clock.sleep,
        stop_requested=lambda: False,
        out=io.StringIO(),
        deadline=START + timedelta(seconds=17),
    )
    assert "budget reached" in result
    assert clock.sleeps == [17]
    assert clock.now() == START + timedelta(seconds=17)
