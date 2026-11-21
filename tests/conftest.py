import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path_factory, monkeypatch):
    """Keep tests from writing cooldowns and the run journal into the real state directory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))
