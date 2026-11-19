import json
import sys
from pathlib import Path

import pytest

from baton.adapter import AGY_READ_ONLY_SETTINGS
from baton.cli import main
from baton.config import AgentConfig
from baton.probe import DEFAULT_PROMPT, probe_argv
from baton.recording import load

FAKE_AGENT = f"""#!{sys.executable}
import os, sys
if "--version" in sys.argv:
    print("fake 1.2.3")
    sys.exit(0)
print("cwd=" + os.getcwd() + " home=" + os.path.expanduser("~") + " mail=me@example.com")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def agent(adapter: str, model: str | None = None, command: str = "tool") -> AgentConfig:
    return AgentConfig(name=f"{adapter}-x", adapter=adapter, command=command, model=model)


def test_probe_argv_per_adapter() -> None:
    assert probe_argv(agent("claude", "sonnet"), "hi") == [
        "tool", "-p", "hi", "--output-format", "json", "--permission-mode", "plan",
        "--model", "sonnet",
    ]  # fmt: skip
    assert probe_argv(agent("codex", "gpt-x"), "hi") == [
        "tool", "exec", "hi", "--json", "--sandbox", "read-only", "--skip-git-repo-check",
        "-m", "gpt-x",
    ]  # fmt: skip
    assert probe_argv(agent("agy"), "hi") == [
        "tool", "-p", "hi", "--output-format", "json", "--mode", "plan",
        "--settings", AGY_READ_ONLY_SETTINGS,
    ]  # fmt: skip
    assert probe_argv(agent("pi", "ollama/q"), "hi") == [
        "tool", "-p", "hi", "--mode", "json", "--model", "ollama/q",
    ]  # fmt: skip


def write_config(tmp_path: Path, command: str = "fake-agent") -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[agents.fake]\nadapter = "claude"\ncommand = "{command}"\nmodel = "opus"\n')
    return cfg


def test_dry_run_runs_nothing_and_model_overrides_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran"
    script = tmp_path / "agent"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    cfg = write_config(tmp_path, str(script))

    code = main(["--config", str(cfg), "probe", "fake", "--dry-run", "--model", "haiku"])

    out = capsys.readouterr().out
    assert code == 0
    assert not marker.exists()
    assert "--model haiku" in out
    assert "opus" not in out
    assert DEFAULT_PROMPT.split()[0] in out


def test_unknown_agent_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_config(tmp_path)
    assert main(["--config", str(cfg), "probe", "nope"]) == 1
    err = capsys.readouterr().err
    assert "unknown agent 'nope'" in err
    assert "fake" in err


@pytest.fixture
def fake_agent(tmp_path: Path) -> Path:
    script = tmp_path / "fake-agent"
    script.write_text(FAKE_AGENT)
    script.chmod(0o755)
    return script


def test_probe_records_redacted_output(
    tmp_path: Path, fake_agent: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_config(tmp_path, str(fake_agent))
    rec_path = tmp_path / "out" / "rec.json"

    code = main(["--config", str(cfg), "probe", "fake", "--record", str(rec_path)])

    assert code == 0
    assert "exit_code=0" in capsys.readouterr().out
    raw = rec_path.read_text()
    assert "me@example.com" not in raw
    assert str(Path.home()) not in raw
    data = json.loads(raw)
    assert data["cli_version"] == "fake 1.2.3"
    assert data["agent"] == "fake"
    assert "<workdir>" in data["stdout"]
    assert "<email>" in data["stdout"]
    assert load(rec_path).exit_code == 0


def test_exit_code_follows_agent(
    tmp_path: Path, fake_agent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_path, str(fake_agent))
    monkeypatch.setenv("FAKE_EXIT", "3")
    assert main(["--config", str(cfg), "probe", "fake"]) == 1


def test_missing_command_exits_1(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, str(tmp_path / "does-not-exist"))
    assert main(["--config", str(cfg), "probe", "fake"]) == 1


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.mark.parametrize("adapter", ["claude", "codex", "agy", "pi"])
@pytest.mark.parametrize("outcome", ["success", "ordinary_error", "limit"])
def test_recheck_parses_each_adapter_and_updates_only_requested_agent(
    tmp_path,
    monkeypatch,
    capsys,
    adapter,
    outcome,
):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore
    from baton.runner import RunResult

    now = datetime(2026, 11, 18, 12, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("baton.probe.datetime", Clock)
    store = LimitStore()
    store.set("target", now + timedelta(hours=2))
    store.set("other", now + timedelta(hours=4))
    config = tmp_path / "recheck.toml"
    config.write_text(
        f'[agents.target]\nadapter="{adapter}"\ncommand="target-cli"\n'
        '[agents.other]\nadapter="pi"\ncommand="must-not-run"\n'
    )
    recording = load(
        Path(__file__).parent
        / "fixtures"
        / adapter
        / ("ok.json" if outcome == "success" else "error.json")
    )
    run = RunResult(recording.stdout, recording.stderr, recording.exit_code, False, 1)
    if outcome == "limit":
        # Simulated diagnostics; pi intentionally exits zero on provider failure.
        payload = {
            "claude": {"is_error": True, "result": "Usage limit reached"},
            "codex": {"type": "turn.failed", "error": {"code": "insufficient_quota"}},
            "agy": {"status": "ERROR", "error": "Quota exhausted"},
            "pi": {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "Usage limit reached",
                },
            },
        }[adapter]
        run = replace(run, stdout=json.dumps(payload), stderr="", exit_code=0)
    calls = []

    def fake_command(argv, cwd, timeout, **kwargs):
        calls.append(argv)
        assert argv[0] == "target-cli"
        assert Path(cwd).is_dir() and not list(Path(cwd).iterdir())
        return RunResult("v1", "", 0, False, 1) if "--version" in argv else run

    monkeypatch.setattr("baton.probe.run_command", fake_command)
    code = main(["--config", str(config), "recheck", "target", "--timeout", "17"])
    assert code == (0 if outcome == "success" else 1)
    assert len(calls) == 2
    assert (
        store.read().get("target")
        == {
            "success": None,
            "ordinary_error": now + timedelta(hours=2),
            "limit": now + timedelta(hours=1),
        }[outcome]
    )
    assert store.read()["other"] == now + timedelta(hours=4)
    out = capsys.readouterr().out
    assert {
        "success": "probe succeeded",
        "ordinary_error": "Cooldown unchanged",
        "limit": "1h fallback",
    }[outcome] in out


@pytest.mark.parametrize("existing", [False, True])
def test_stale_successful_recheck_preserves_newer_cooldown(tmp_path, monkeypatch, capsys, existing):
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore

    now = datetime.now(UTC)
    store = LimitStore()
    if existing:
        store.set("fake", now + timedelta(hours=1))
    newer = now + timedelta(hours=3)

    def probe(*args, **kwargs):
        store.set("fake", newer)
        return load(Path(__file__).parent / "fixtures/claude/ok.json")

    monkeypatch.setattr("baton.probe.run_probe", probe)
    cfg = write_config(tmp_path)
    assert main(["--config", str(cfg), "recheck", "fake"]) == 0
    assert store.read()["fake"] == newer
    assert "newer cooldown was preserved" in capsys.readouterr().out


def test_recheck_unknown_agent_and_bad_state_never_probe(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from baton.limits import LimitStore

    probe = Mock()
    monkeypatch.setattr("baton.probe.run_probe", probe)
    cfg = write_config(tmp_path)
    assert main(["--config", str(cfg), "recheck", "missing"]) == 1
    store = LimitStore()
    store.path.parent.mkdir(parents=True)
    store.path.write_text("broken")
    assert main(["--config", str(cfg), "recheck", "fake"]) == 1
    probe.assert_not_called()
    assert store.path.read_text() == "broken"


def test_recheck_uses_provider_reset(tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore

    deadline = datetime.now(UTC) + timedelta(hours=3)
    rec = load(Path(__file__).parent / "fixtures/claude/error.json")
    rec = replace(
        rec,
        stdout=json.dumps(
            {
                "is_error": True,
                "result": "Usage limit reached",
                "error": {"reset_at": deadline.isoformat()},
            }
        ),
        exit_code=0,
    )
    monkeypatch.setattr("baton.probe.run_probe", lambda *a, **kw: rec)
    cfg = write_config(tmp_path)
    assert main(["--config", str(cfg), "recheck", "fake"]) == 1
    assert LimitStore().read()["fake"] == deadline


def test_recheck_timeout_preserves_cooldown(tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from baton.limits import LimitStore

    deadline = datetime.now(UTC) + timedelta(hours=2)
    LimitStore().set("fake", deadline)
    rec = load(Path(__file__).parent / "fixtures/claude/ok.json")
    monkeypatch.setattr("baton.probe.run_probe", lambda *a, **kw: replace(rec, timed_out=True))
    cfg = write_config(tmp_path)
    assert main(["--config", str(cfg), "recheck", "fake"]) == 1
    assert LimitStore().read()["fake"] == deadline


def test_recheck_success_without_cooldown_does_not_create_state(tmp_path, monkeypatch):
    from baton.limits import LimitStore

    rec = load(Path(__file__).parent / "fixtures/claude/ok.json")
    monkeypatch.setattr("baton.probe.run_probe", lambda *a, **kw: rec)
    cfg = write_config(tmp_path)
    assert main(["--config", str(cfg), "recheck", "fake"]) == 0
    assert not LimitStore().path.parent.exists()
