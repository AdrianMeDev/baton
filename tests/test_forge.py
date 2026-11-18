import json

import pytest

from baton.forge import ForgeError, publish


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", "MERGED", None])
def test_publish_retries(tmp_path, monkeypatch, state):
    calls = []

    def gh(repo, *args):
        calls.append(args)
        if args[1] == "list":
            return json.dumps(
                [dict(number=1, url="https://github.com/a/b/pull/1", state=state)] if state else []
            )
        if "--body-file" in args:
            from pathlib import Path

            assert Path(args[args.index("--body-file") + 1]).read_text() == "report"
        return "https://github.com/a/b/pull/1"

    monkeypatch.setattr("baton.forge.gh", gh)
    if state == "CLOSED":
        with pytest.raises(ForgeError, match="Closed"):
            publish(tmp_path, "a/b", "main", "branch", "title", "report")
    else:
        assert publish(tmp_path, "a/b", "main", "branch", "title", "report")["url"].endswith("/1")
        assert all("--repo" in c for c in calls)
        assert len(calls) == (1 if state == "MERGED" else 2)
