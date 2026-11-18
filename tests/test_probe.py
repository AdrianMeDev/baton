import json
import sys
from pathlib import Path

import pytest

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
