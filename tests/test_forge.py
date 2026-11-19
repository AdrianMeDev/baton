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


@pytest.mark.parametrize(
    "mode", ["green", "red", "absent", "pending", "changed", "rejected", "unconfirmed"]
)
def test_merge_states(tmp_path, monkeypatch, mode):
    from baton.forge import merge_when_ready

    calls = []

    def gh(repo, *args):
        calls.append(args)
        merged = any(c[1] == "merge" for c in calls)
        if args[1] == "merge":
            assert args[-2:] == ("--match-head-commit", "sha")
            if mode == "rejected":
                raise ForgeError("rejected")
            return ""
        check = dict(__typename="CheckRun", status="COMPLETED", conclusion="SUCCESS")
        if mode == "red":
            check["conclusion"] = "FAILURE"
        if mode == "pending":
            check["status"] = "IN_PROGRESS"
        return json.dumps(
            dict(
                state="MERGED" if merged and mode != "unconfirmed" else "OPEN",
                headRefOid="changed" if mode == "changed" else "sha",
                statusCheckRollup=[] if mode == "absent" else [check],
            )
        )

    monkeypatch.setattr("baton.forge.gh", gh)
    if mode == "green":
        merge_when_ready(tmp_path, "a/b", "url", "sha", timeout=0)
    else:
        with pytest.raises(ForgeError):
            merge_when_ready(tmp_path, "a/b", "url", "sha", timeout=0)
    assert any(c[1] == "merge" for c in calls) == (mode in ("green", "rejected", "unconfirmed"))


@pytest.mark.parametrize("initial", [[], [{"__typename": "StatusContext", "state": "PENDING"}]])
def test_checks_can_appear_and_finish(tmp_path, monkeypatch, initial):
    from baton.forge import merge_when_ready

    responses = iter(
        [
            dict(state="OPEN", headRefOid="sha", statusCheckRollup=initial),
            dict(
                state="OPEN",
                headRefOid="sha",
                statusCheckRollup=[dict(__typename="StatusContext", state="SUCCESS")],
            ),
            dict(state="MERGED", headRefOid="sha"),
        ]
    )

    def gh(repo, *args):
        return "" if args[1] == "merge" else json.dumps(next(responses))

    monkeypatch.setattr("baton.forge.gh", gh)
    merge_when_ready(tmp_path, "a/b", "url", "sha", interval=0)


@pytest.mark.parametrize("green", [True, False])
def test_stop_during_ci_or_before_merge_leaves_pr_open(tmp_path, monkeypatch, green):
    from baton.forge import merge_when_ready

    calls = []
    stopped = False

    def gh(repo, *args):
        nonlocal stopped
        calls.append(args)
        stopped = True
        return json.dumps(
            dict(
                state="OPEN",
                headRefOid="sha",
                statusCheckRollup=[
                    dict(__typename="StatusContext", state="SUCCESS" if green else "PENDING")
                ],
            )
        )

    monkeypatch.setattr("baton.forge.gh", gh)
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(ForgeError, match="stop requested"):
        merge_when_ready(tmp_path, "a/b", "url", "sha", stop_requested=lambda: stopped)
    assert all(args[1] != "merge" for args in calls)
