import json
import re
from pathlib import Path

import pytest

from baton.recording import Recording, load

FIXTURES = Path(__file__).parent / "fixtures"
RECORDINGS = sorted(FIXTURES.glob("*/*.json"))
PERSONAL = re.compile(r"/home/|@[a-z0-9-]+\.[a-z]", re.IGNORECASE)


def _ids(paths: list[Path]) -> list[str]:
    return [f"{p.parent.name}/{p.name}" for p in paths]


def _json_lines(text: str) -> list[object]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.parametrize("path", RECORDINGS, ids=_ids(RECORDINGS))
def test_recording_loads_and_is_redacted(path: Path) -> None:
    rec = load(path)
    assert isinstance(rec, Recording)
    assert rec.adapter == path.parent.name
    assert not PERSONAL.search(path.read_text())


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*/ok.json")), ids=lambda p: p.parent.name)
def test_ok_recordings_succeed(path: Path) -> None:
    rec = load(path)
    assert rec.exit_code == 0
    assert not rec.timed_out
    assert rec.stdout.strip()


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*/error.json")), ids=lambda p: p.parent.name)
def test_error_recordings_fail(path: Path) -> None:
    rec = load(path)
    assert rec.exit_code != 0
    assert rec.stdout.strip() or rec.stderr.strip()


def test_claude_json_result() -> None:
    ok = json.loads(load(FIXTURES / "claude/ok.json").stdout)
    assert ok["result"] == "OK"
    assert ok["is_error"] is False
    err = json.loads(load(FIXTURES / "claude/error.json").stdout)
    assert err["is_error"] is True


def test_codex_jsonl_events() -> None:
    ok = _json_lines(load(FIXTURES / "codex/ok.json").stdout)
    messages = [e["item"]["text"] for e in ok if e.get("type") == "item.completed"]
    assert messages == ["OK"]
    err = _json_lines(load(FIXTURES / "codex/error.json").stdout)
    assert err[-1]["type"] == "turn.failed"


def test_agy_json_result() -> None:
    ok = json.loads(load(FIXTURES / "agy/ok.json").stdout)
    assert ok["status"] == "SUCCESS"
    assert ok["response"].strip() == "OK"
    err = json.loads(load(FIXTURES / "agy/error.json").stdout)
    assert err["status"] == "ERROR"


@pytest.mark.parametrize("name", ["ok-tty.json", "ok-notty.json"])
def test_agy_works_with_and_without_terminal(name: str) -> None:
    rec = load(FIXTURES / "agy" / name)
    assert rec.exit_code == 0
    assert json.loads(rec.stdout)["response"].strip() == "OK"
