import stat
from pathlib import Path

import pytest

from baton.cli import main

CONFIG = """
[agents.alpha]
adapter = "claude"
command = "fake-alpha"
model = "opus"

[agents.beta]
adapter = "codex"
command = "fake-beta"
"""


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", str(path))
    monkeypatch.delenv("BATON_CONFIG", raising=False)
    return path


def make_executable(directory: Path, name: str) -> Path:
    exe = directory / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def write_config(tmp_path: Path, text: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(text)
    return cfg


def test_doctor_marks_found_and_missing(
    tmp_path: Path, bin_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    alpha = make_executable(bin_dir, "fake-alpha")
    make_executable(bin_dir, "git")
    cfg = write_config(tmp_path, CONFIG)

    assert main(["--config", str(cfg), "doctor"]) == 0

    lines = capsys.readouterr().out.splitlines()
    alpha_line = next(line for line in lines if "alpha" in line)
    beta_line = next(line for line in lines if "beta" in line)
    assert "✓" in alpha_line and str(alpha) in alpha_line and "opus" in alpha_line
    assert "✗" in beta_line and "not found in PATH" in beta_line
    assert next(line for line in lines if line.strip().endswith(str(bin_dir / "git")))
    assert any("✗ gh" in line for line in lines)


def test_doctor_invalid_config_exits_1(
    tmp_path: Path, bin_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_config(tmp_path, '[agents.x]\nadapter = "nope"\ncommand = "x"\n')

    assert main(["--config", str(cfg), "doctor"]) == 1

    assert "unknown adapter" in capsys.readouterr().out


def test_doctor_missing_config_file_exits_1(
    tmp_path: Path, bin_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(tmp_path / "absent.toml"), "doctor"]) == 1
    assert "config file not found" in capsys.readouterr().out


def test_doctor_uses_bundled_example_without_user_config(
    tmp_path: Path,
    bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert main(["doctor"]) == 0

    out = capsys.readouterr().out
    assert "bundled example" in out
    assert "claude-opus" in out
