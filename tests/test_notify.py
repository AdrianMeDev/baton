import os
import urllib.error
import urllib.request

from baton.config import AutoConfig
from baton.notify import notify


def test_notify_send_receives_message(tmp_path, monkeypatch):
    log = tmp_path / "args.txt"
    exe = tmp_path / "notify-send"
    exe.write_text(f'#!/bin/sh\nprintf "%s|" "$@" > {log}\n')
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    notify(AutoConfig(notify="notify-send"), "baton: M1 done", "PR https://x/1")
    assert log.read_text() == "--|baton: M1 done|PR https://x/1|"


def test_missing_notify_send_only_logs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    notify(AutoConfig(notify="notify-send"), "t", "m")
    assert "Notification via notify-send failed" in capsys.readouterr().out


def test_ntfy_posts_message(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen["url"], seen["body"] = request.full_url, request.data
        seen["title"], seen["method"] = request.get_header("Title"), request.get_method()
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    notify(AutoConfig(notify="ntfy", ntfy_url="https://ntfy.sh/t"), "baton: stop", "reason")
    assert seen == {
        "url": "https://ntfy.sh/t",
        "body": b"reason",
        "title": "baton: stop",
        "method": "POST",
    }


def test_ntfy_http_error_only_logs(monkeypatch, capsys):
    def boom(request, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    notify(AutoConfig(notify="ntfy", ntfy_url="https://ntfy.sh/t"), "t", "m")
    assert "Notification via ntfy failed" in capsys.readouterr().out


def test_none_does_nothing(capsys):
    notify(AutoConfig(), "t", "m")
    assert capsys.readouterr().out == ""
