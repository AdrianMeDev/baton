import json
from pathlib import Path

from baton.adapter import ClaudeAdapter
from baton.recording import load
from baton.runner import RunResult

FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def _run_result(name: str) -> RunResult:
    rec = load(FIXTURES / name)
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
    raw = json.loads(load(FIXTURES / "error.json").stdout)
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
