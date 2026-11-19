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


def test_periodic_probe_cadence_failure_then_success(tmp_path):
    store = LimitStore(tmp_path / "limits.json")
    reset = START + timedelta(hours=2)
    store.set("a", reset)
    clock = Clock()
    probes = []

    def probe(name, timeout):
        probes.append((name, clock.now(), timeout))
        if len(probes) == 2:
            store.clear_if(name, reset)

    result = wait_for_reset(
        ["a"],
        store,
        now=clock.now,
        sleep=clock.sleep,
        stop_requested=lambda: False,
        out=io.StringIO(),
        recheck_interval_seconds=45,
        recheck=probe,
    )
    assert result == "ready"
    assert clock.sleeps == [45, 45]
    assert [p[1] for p in probes] == [START + timedelta(seconds=s) for s in (45, 90)]


def test_default_wait_issues_no_probes(tmp_path):
    store = LimitStore(tmp_path / "limits.json")
    store.set("a", START + timedelta(seconds=60))
    clock = Clock()
    probes = []
    assert (
        wait_for_reset(
            ["a"],
            store,
            now=clock.now,
            sleep=clock.sleep,
            stop_requested=lambda: False,
            out=io.StringIO(),
            recheck=lambda *args: probes.append(args),
        )
        == "ready"
    )
    assert probes == []


def test_periodic_probes_respect_remaining_deadline(tmp_path):
    store = LimitStore(tmp_path / "limits.json")
    store.set("a", START + timedelta(hours=2))
    store.set("b", START + timedelta(hours=2))
    clock = Clock()
    probes = []

    def probe(name, timeout):
        probes.append((name, timeout))
        clock.t += timedelta(seconds=timeout)

    result = wait_for_reset(
        ["a", "b"],
        store,
        now=clock.now,
        sleep=clock.sleep,
        stop_requested=lambda: False,
        out=io.StringIO(),
        deadline=START + timedelta(seconds=65),
        recheck_interval_seconds=60,
        recheck=probe,
    )
    assert "budget reached" in result
    assert probes == [("a", 5)]
    assert clock.now() == START + timedelta(seconds=65)


def test_stop_during_probe_prevents_further_probes(tmp_path):
    store = LimitStore(tmp_path / "limits.json")
    for name in ("a", "b"):
        store.set(name, START + timedelta(hours=2))
    clock = Clock()
    probes = []
    result = wait_for_reset(
        ["a", "b"],
        store,
        now=clock.now,
        sleep=clock.sleep,
        stop_requested=lambda: bool(probes),
        out=io.StringIO(),
        recheck_interval_seconds=60,
        recheck=lambda name, timeout: probes.append(name),
    )
    assert result == "stopped"
    assert probes == ["a"]
