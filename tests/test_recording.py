import json
from pathlib import Path

from baton.recording import Recording, load, redact, save


def make(**kw) -> Recording:
    base = dict(
        adapter="claude",
        agent="claude-sonnet",
        argv=["claude", "-p", "hi"],
        tty=False,
        exit_code=0,
        timed_out=False,
        duration_s=1.5,
        stdout="ok",
        stderr="",
        recorded_at="2026-11-18T10:00:00+00:00",
        cli_version="1.0.0",
    )
    base.update(kw)
    return Recording(**base)


def test_redact_home_workdir_email_uuid():
    home = Path("/home/someone")
    work = "/tmp/baton-probe-abc"
    text = f"{home}/proj {work}/x mail me@example.com id 123e4567-e89b-12d3-a456-426614174000"
    out = redact(text, home=home, workdir=work)
    assert out == "~/proj <workdir>/x mail <email> id <uuid>"


def test_workdir_below_home_is_workdir_token():
    out = redact("/home/u/tmp/w/file", home="/home/u", workdir="/home/u/tmp/w")
    assert out == "<workdir>/file"


def test_save_redacts_and_round_trips(tmp_path):
    rec = make(stdout="/home/u/a me@x.org", argv=["x", "/home/u/w"])
    path = tmp_path / "sub" / "rec.json"
    save(rec, path, home="/home/u", workdir="/home/u/w")
    raw = path.read_text()
    assert "/home/u" not in raw and "me@x.org" not in raw
    data = json.loads(raw)
    assert data["stdout"] == "~/a <email>"
    loaded = load(path)
    assert loaded.argv == ["x", "<workdir>"]
    assert loaded.exit_code == 0
    assert loaded.duration_s == 1.5


def test_timeout_recording_keeps_none_exit_code(tmp_path):
    path = tmp_path / "r.json"
    save(make(exit_code=None, timed_out=True), path)
    assert load(path).exit_code is None
