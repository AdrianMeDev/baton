import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from baton.adapter import AgyAdapter, ClaudeAdapter, CodexAdapter, PiAdapter
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
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--",
        "do it",
    ]
    assert CodexAdapter("my-codex", "my-model").argv("--literal prompt") == [
        "my-codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
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


def test_agy_argv_requests_headless_edits():
    assert AgyAdapter().argv("do it") == [
        "agy",
        "-p",
        "do it",
        "--output-format",
        "json",
        "--mode",
        "accept-edits",
    ]
    argv = AgyAdapter("my-agy", "Gemini 4 Argon").argv("do it")
    assert argv[0] == "my-agy"
    assert argv[-2:] == ["--model", "Gemini 4 Argon"]


def test_agy_empty_output_runs_once(monkeypatch, tmp_path):
    adapter = AgyAdapter()
    runner = Mock(return_value=RunResult("", "", 0, False, 1))
    monkeypatch.setattr("baton.adapter.run_command", runner)
    res = adapter.run("do it", tmp_path, 17)
    assert not res.ok
    assert res.error == "no output (exit 0)"
    runner.assert_called_once_with(adapter.argv("do it"), tmp_path, 17)


def test_agy_parse_success_fixture():
    run = _run_result("ok-notty.json", "agy")
    res = AgyAdapter().parse(run)
    assert res.ok
    assert res.text == "OK\n"
    assert res.raw == run.stdout


@pytest.mark.parametrize("exit_code", [0, 1])
def test_agy_parse_error_fixture(exit_code):
    run = replace(_run_result("error.json", "agy"), exit_code=exit_code, stderr="")
    res = AgyAdapter().parse(run)
    assert not res.ok
    assert "invalid model selection" in res.error


@pytest.mark.parametrize(
    ("stdout", "diagnostic"),
    [
        ("{", "output is not valid JSON"),
        ("[]", "output is not a JSON object"),
        ('{"response":"OK"}', "unexpected agy status None"),
        ('{"status":"SUCCESS","response":1}', "agy SUCCESS output has no string response"),
        ('{"status":"ERROR"}', "agy reported an error"),
    ],
)
def test_agy_parse_invalid_result(stdout, diagnostic):
    res = AgyAdapter().parse(RunResult(stdout, "", 0, False, 1))
    assert not res.ok
    assert res.error == diagnostic


def test_agy_nonzero_exit_with_success_payload_fails():
    run = replace(_run_result("ok.json", "agy"), exit_code=2, stderr="exit detail")
    res = AgyAdapter().parse(run)
    assert not res.ok
    assert res.error == "exit code 2: exit detail"


def test_agy_timeout_takes_precedence_over_output():
    run = replace(_run_result("ok.json", "agy"), timed_out=True, duration_s=30)
    res = AgyAdapter().parse(run)
    assert not res.ok
    assert res.error == "timed out after 30s"


@pytest.mark.parametrize("model", ["ollama/qwen2.5-coder", "openrouter/deepseek/deepseek-chat"])
def test_pi_argv_keeps_provider_qualified_model(model):
    assert PiAdapter("my-pi", model).argv("do it") == [
        "my-pi",
        "-p",
        "do it",
        "--mode",
        "json",
        "--model",
        model,
    ]


def test_pi_run_uses_default_model_and_shared_runner(monkeypatch, tmp_path):
    runner = Mock(return_value=_run_result("ok.json", "pi"))
    monkeypatch.setattr("baton.adapter.run_command", runner)
    assert PiAdapter().run("do it", tmp_path, 17).ok
    runner.assert_called_once_with(["pi", "-p", "do it", "--mode", "json"], tmp_path, 17)


def test_pi_parse_success_fixture():
    run = _run_result("ok.json", "pi")
    res = PiAdapter().parse(run)
    assert res.ok
    assert res.text == "OK"
    assert res.cost == pytest.approx(0.00048135)
    assert res.raw == run.stdout


def test_pi_parse_exit_zero_error_fixture():
    run = _run_result("error.json", "pi")
    assert run.exit_code == 0
    res = PiAdapter().parse(run)
    assert not res.ok
    assert "stopped (error)" in res.error
    assert "Unknown Model" in res.error


def test_pi_extracts_final_text_and_counts_each_assistant_message_once():
    earlier = {
        "role": "assistant",
        "stopReason": "toolUse",
        "content": [{"type": "text", "text": "Working"}],
        "usage": {"cost": {"total": 0.01}},
    }
    final = {
        "role": "assistant",
        "stopReason": "stop",
        "content": [
            {"type": "thinking", "thinking": "private"},
            {"type": "text", "text": "Done"},
            {"type": "text", "text": "Verified"},
        ],
        "usage": {"cost": {"total": 0.02}},
    }
    events = [
        {"type": "message_end", "message": earlier},
        {"type": "message_end", "message": {"role": "toolResult", "content": []}},
        {"type": "message_end", "message": final},
        {"type": "agent_end", "messages": [earlier, final]},
        {"type": "agent_settled", "aborted": False},
    ]
    res = PiAdapter().parse(RunResult("\n".join(map(json.dumps, events)), "", 0, False, 1))
    assert res.ok
    assert res.text == "Done\nVerified"
    assert res.cost == pytest.approx(0.03)


@pytest.mark.parametrize(
    ("stdout", "diagnostic"),
    [
        ("", "missing agent_settled"),
        ("[]", "invalid pi event"),
        ('{"type":"message_end"}', "invalid pi message"),
        ('{"type":"agent_settled","aborted":true}', "pi run aborted"),
        ('{"type":"agent_settled"}', "no final assistant message"),
    ],
)
def test_pi_invalid_or_incomplete_result(stdout, diagnostic):
    res = PiAdapter().parse(RunResult(stdout, "", 0, False, 1))
    assert not res.ok
    assert diagnostic in res.error


def test_pi_truncated_run_with_assistant_text_fails():
    run = _run_result("ok.json", "pi")
    incomplete = "\n".join(run.stdout.splitlines()[:-1])
    res = PiAdapter().parse(replace(run, stdout=incomplete))
    assert not res.ok
    assert "missing agent_settled" in res.error
    res = PiAdapter().parse(replace(run, stdout=incomplete + '\n{"type":'))
    assert not res.ok
    assert "invalid JSON" in res.error


@pytest.mark.parametrize(
    ("reason", "error", "diagnostic"),
    [("length", "", "stopped (length)"), ("stop", "provider error", "provider error")],
)
def test_pi_partial_assistant_response_fails(reason, error, diagnostic):
    message = {
        "role": "assistant",
        "stopReason": reason,
        "errorMessage": error,
        "content": [{"type": "text", "text": "Partial answer"}],
    }
    stdout = json.dumps({"type": "message_end", "message": message})
    stdout += '\n{"type":"agent_settled","aborted":false}\n'
    res = PiAdapter().parse(RunResult(stdout, "", 0, False, 1))
    assert not res.ok
    assert diagnostic in res.error


def test_pi_nonzero_exit_with_completed_run_fails():
    run = replace(_run_result("ok.json", "pi"), exit_code=2, stderr="exit detail")
    res = PiAdapter().parse(run)
    assert not res.ok
    assert res.error == "exit code 2: exit detail"


def test_pi_timeout_takes_precedence_over_output():
    run = replace(_run_result("ok.json", "pi"), timed_out=True, duration_s=30)
    res = PiAdapter().parse(run)
    assert not res.ok
    assert res.error == "timed out after 30s"


# Simulated quota failures: M1 has no real quota recording yet.
@pytest.mark.parametrize(
    ("adapter", "payload"),
    [
        (ClaudeAdapter, {"is_error": True, "result": "You've hit your usage limit"}),
        (CodexAdapter, {"type": "turn.failed", "error": {"code": "insufficient_quota"}}),
        (AgyAdapter, {"status": "ERROR", "error": "Quota exhausted"}),
        (
            PiAdapter,
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "Usage limit reached",
                },
            },
        ),
    ],
)
def test_simulated_usage_limit_at_exit_zero(adapter, payload):
    result = adapter().parse(RunResult(json.dumps(payload), "", 0, False, 1))
    assert not result.ok
    assert result.limit is not None
    assert result.limit.reset_at is None
    assert result.raw == json.dumps(payload)


@pytest.mark.parametrize("adapter", [ClaudeAdapter, CodexAdapter, AgyAdapter, PiAdapter])
def test_plain_failure_diagnostic_and_timeout(adapter):
    run = RunResult("", "Usage limit reached; reset in 2 hours", 1, False, 1)
    assert adapter().parse(run).limit.reset_at is not None
    assert adapter().parse(replace(run, timed_out=True)).limit is None
    assert adapter().parse(replace(run, stderr="HTTP 429: too many requests")).limit is None


@pytest.mark.parametrize(
    ("adapter", "fixture"),
    [(ClaudeAdapter, "claude"), (CodexAdapter, "codex"), (AgyAdapter, "agy"), (PiAdapter, "pi")],
)
def test_real_m1_errors_are_not_limits_and_success_mentions_stay_successful(adapter, fixture):
    assert adapter().parse(_run_result("error.json", fixture)).limit is None
    run = _run_result("ok.json", fixture)
    run = replace(
        run, stdout=run.stdout.replace("OK", "Usage limit reached"), stderr="Quota exhausted"
    )
    parsed = adapter().parse(run)
    assert parsed.ok
    assert parsed.limit is None


def test_custom_limit_patterns_replace_defaults_and_reach_factory():
    from baton.adapter import make_adapter
    from baton.config import AgentConfig

    adapter = make_adapter(AgentConfig("local", "pi", "pi", limit_patterns=("NO TOKENS",)))
    assert adapter.parse(RunResult("", "NO TOKENS", 1, False, 1)).limit is not None
    assert adapter.parse(RunResult("", "Quota exhausted", 1, False, 1)).limit is None


@pytest.mark.parametrize("adapter", [ClaudeAdapter, CodexAdapter, AgyAdapter, PiAdapter])
def test_structured_reset_fields(adapter):
    from datetime import UTC, datetime

    payload = {"error": {"code": "insufficient_quota", "reset_at": "2026-11-19T10:00:00Z"}}
    parsed = adapter().parse(RunResult(json.dumps(payload, indent=2), "", 1, False, 1))
    assert parsed.limit.reset_at == datetime(2026, 11, 19, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("fields", "diagnostic", "expected"),
    [
        ({"retry_after_seconds": 120}, "quota exhausted", "2026-11-18T12:02:00+00:00"),
        ({}, "Usage limit reached; reset in 2 hours", "2026-11-18T14:00:00+00:00"),
        ({}, "Quota exceeded; resets at 2026-11-19T10:00:00Z", "2026-11-19T10:00:00+00:00"),
        ({"reset_at": "2026-11-18T14:00:00+02:00"}, "quota exhausted", "2026-11-18T12:00:00+00:00"),
        ({"reset_at": "tomorrow"}, "quota exhausted", None),
        ({"reset_at": "2026-11-19T10:00:00"}, "quota exhausted", None),
        ({"retry_after": -1}, "quota exhausted", None),
        ({"retry_after": float("inf")}, "quota exhausted", None),
    ],
)
def test_limit_reset_hints_with_fixed_clock(fields, diagnostic, expected):
    from datetime import UTC, datetime

    from baton.limits import classify_limit

    limit = classify_limit(diagnostic, fields, now=datetime(2026, 11, 18, 12, tzinfo=UTC))
    assert limit is not None
    assert (limit.reset_at.isoformat() if limit.reset_at else None) == expected


@pytest.mark.parametrize(
    "path", sorted(FIXTURES.glob("*/*.json")), ids=lambda path: f"{path.parent.name}/{path.name}"
)
def test_replay_every_real_m1_recording(path):
    adapters = {"claude": ClaudeAdapter, "codex": CodexAdapter, "agy": AgyAdapter, "pi": PiAdapter}
    rec = load(path)
    parsed = adapters[rec.adapter]().parse(
        RunResult(rec.stdout, rec.stderr, rec.exit_code, rec.timed_out, rec.duration_s)
    )
    assert parsed.ok == path.name.startswith("ok")
    assert parsed.limit is None


@pytest.mark.parametrize(
    "name,total", [("claude", 22583), ("codex", 21454), ("agy", 10288), ("pi", 3202)]
)
def test_recorded_token_totals(name, total):
    import json
    from pathlib import Path

    from baton.adapter import _reported_tokens

    raw = json.loads((Path(__file__).parent / "fixtures" / name / "ok.json").read_text())["stdout"]
    assert _reported_tokens(name.title() + "Adapter", raw) == total


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({}, None),
        ({"input_tokens": True, "output_tokens": 1}, None),
        ({"input_tokens": 0, "output_tokens": 0}, 0),
    ],
)
def test_optional_usage(usage, expected):
    import json

    from baton.adapter import _reported_tokens

    event = json.dumps({"type": "turn.completed", "usage": usage})
    assert _reported_tokens("CodexAdapter", event) == expected
    if expected is not None:
        assert _reported_tokens("CodexAdapter", event + "\n" + event) == expected * 2
