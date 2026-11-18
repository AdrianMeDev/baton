import json
from dataclasses import replace
from pathlib import Path

import pytest

from baton.adapter import ClaudeAdapter, CodexAdapter
from baton.recording import load
from baton.runner import RunResult

FIXTURES = Path(__file__).parent / "fixtures"


def _run_result(name: str, adapter: str = "claude") -> RunResult:
    rec = load(FIXTURES / adapter / name)
    return RunResult(rec.stdout, rec.stderr, rec.exit_code, rec.timed_out, rec.duration_s)


def test_argv_without_model():
    argv = ClaudeAdapter("claude").argv("do it")
    assert argv[:4] == ["claude", "-p", "do it", "--output-format"]
    assert "--model" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_argv_with_model():
    argv = ClaudeAdapter("claude", "haiku").argv("x")
    assert argv[argv.index("--model") + 1] == "haiku"


def test_argv_blocks_git_and_has_no_plan_mode():
    argv = ClaudeAdapter().argv("x")
    assert "plan" not in argv
    assert argv[argv.index("--disallowedTools") + 1] == "Bash(git:*)"
    assert argv[argv.index("--allowedTools") + 1] == "Bash"


def test_parse_ok_fixture():
    res = ClaudeAdapter().parse(_run_result("ok.json"))
    assert res.ok
    assert res.text == "OK"
    assert res.cost is not None and res.cost > 0
    assert res.error == ""


def test_parse_error_fixture_uses_is_error_not_subtype():
    raw = json.loads(load(FIXTURES / "claude" / "error.json").stdout)
    assert raw["subtype"] == "success"
    res = ClaudeAdapter().parse(_run_result("error.json"))
    assert not res.ok
    assert "does-not-exist-xyz" in res.error


def test_parse_garbage():
    res = ClaudeAdapter().parse(RunResult("not json", "", 0, False, 1.0))
    assert not res.ok
    assert "JSON" in res.error


def test_parse_empty_output_includes_stderr():
    res = ClaudeAdapter().parse(RunResult("", "boom", 1, False, 1.0))
    assert not res.ok
    assert "boom" in res.error


def test_parse_timeout():
    res = ClaudeAdapter().parse(RunResult('{"is_error": false}', "", None, True, 30.0))
    assert not res.ok
    assert "timed out" in res.error


def test_parse_non_object_json():
    assert not ClaudeAdapter().parse(RunResult("[1]", "", 0, False, 1.0)).ok


def test_parse_nonzero_exit_with_ok_payload_fails():
    res = ClaudeAdapter().parse(RunResult('{"is_error": false, "result": "x"}', "", 2, False, 1.0))
    assert not res.ok
    assert "exit code 2" in res.error


def test_codex_argv():
    assert CodexAdapter().argv("do it") == [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--",
        "do it",
    ]
    assert CodexAdapter("my-codex", "my-model").argv("--literal prompt") == [
        "my-codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "-m",
        "my-model",
        "--",
        "--literal prompt",
    ]


def test_codex_run_passes_cwd_and_timeout(monkeypatch, tmp_path):
    adapter = CodexAdapter()

    def fake_run(argv, cwd, timeout):
        assert argv == adapter.argv("do it")
        assert cwd == tmp_path
        assert timeout == 17
        return _run_result("ok.json", "codex")

    monkeypatch.setattr("baton.adapter.run_command", fake_run)
    assert adapter.run("do it", tmp_path, 17).text == "OK"


def test_codex_parse_ok_fixture():
    run = _run_result("ok.json", "codex")
    res = CodexAdapter().parse(run)
    assert res.ok
    assert res.text == "OK"
    assert res.raw == run.stdout


def test_codex_returns_last_completed_message():
    stdout = (
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"Working"}}\n'
        '{"type":"item.completed","item":{"type":"command_execution","exit_code":1}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"All\u2028done"}}\n'
        '{"type":"item.updated","item":{"type":"agent_message","text":"Ignore"}}\n'
        '{"type":"turn.completed"}\n'
    )
    res = CodexAdapter().parse(RunResult(stdout, "", 0, False, 1))
    assert res.ok
    assert res.text == "All\u2028done"


def test_codex_parse_error_fixture():
    res = CodexAdapter().parse(_run_result("error.json", "codex"))
    assert not res.ok
    assert "Codex turn failed" in res.error
    assert "model is not supported" in res.error


@pytest.mark.parametrize(
    ("suffix", "diagnostic"),
    [
        ('{"type":"turn.failed","error":{"message":"request failed"}}\n', "request failed"),
        ('{"type":"turn.completed"', "invalid JSON on output line 2"),
        ("[]\n", "invalid Codex event on output line 2"),
        ("", "missing turn.completed"),
        ('{"type":"turn.completed"}\n{"type":"turn.started"}\n', "missing turn.completed"),
    ],
)
def test_codex_message_without_successful_turn_fails(suffix, diagnostic):
    stdout = '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n' + suffix
    res = CodexAdapter().parse(RunResult(stdout, "", 0, False, 1))
    assert not res.ok
    assert diagnostic in res.error


def test_codex_completed_turn_requires_message():
    res = CodexAdapter().parse(RunResult('{"type":"turn.completed"}\n', "", 0, False, 1))
    assert not res.ok
    assert "no agent message" in res.error


def test_codex_nonzero_exit_with_completed_turn_fails():
    run = replace(_run_result("ok.json", "codex"), exit_code=2, stderr="exit detail")
    res = CodexAdapter().parse(run)
    assert not res.ok
    assert res.error == "exit code 2: exit detail"


def test_codex_timeout_takes_precedence_over_output():
    run = replace(_run_result("ok.json", "codex"), timed_out=True, duration_s=30)
    res = CodexAdapter().parse(run)
    assert not res.ok
    assert res.error == "timed out after 30s"
