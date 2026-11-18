from pathlib import Path

import pytest

from baton import __version__
from baton.cli import main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"baton {__version__}"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: baton" in capsys.readouterr().out


def test_status_command_without_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--path", str(tmp_path)]) == 0
    assert "No .agent-plan.md" in capsys.readouterr().out
