"""Durable state of `baton auto` runs, shared with `baton stop`."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


class AutoStateError(Exception):
    """Auto state cannot safely be read or updated."""


def auto_state_path() -> Path:
    """Auto state lives next to the shared cooldowns, one entry per repository."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "baton" / "auto.json"


@dataclass(frozen=True)
class AutoState:
    """What an auto run needs to continue after a crash; empty values mean "none"."""

    milestone: str = ""
    step: int = 0
    branch: str = ""
    pr_url: str = ""
    steps_done: int = 0
    started_at: str = ""
    stop_requested: bool = False


_TYPES = {field.name: field.default for field in fields(AutoState)}


def _parse(value: Any) -> AutoState:
    if not isinstance(value, dict) or set(value) - set(_TYPES):
        raise ValueError("expected an object with known auto state fields")
    for name, item in value.items():
        # bool is an int subclass; reject it for the numeric fields explicitly.
        if type(item) is not type(_TYPES[name]):
            raise ValueError(f"field {name!r} has the wrong type")
    return AutoState(**value)


class AutoStore:
    """Per-repository state with serialized read/modify/write and atomic replacement."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = auto_state_path() if path is None else path

    @staticmethod
    def _key(repo: Path) -> str:
        return str(repo.resolve())

    def _read_all(self) -> dict[str, AutoState]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as exc:
            raise AutoStateError(
                f"Cannot read {self.path}: {exc}. Check file permissions."
            ) from exc
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("expected an object mapping repository paths to states")
            return {key: _parse(value) for key, value in data.items()}
        except (ValueError, TypeError) as exc:
            raise AutoStateError(
                f"Invalid auto state in {self.path}: {exc}. "
                "Repair or move this file before retrying; it was not overwritten."
            ) from exc

    def read(self, repo: Path) -> AutoState:
        return self._read_all().get(self._key(repo), AutoState())

    def update(self, repo: Path, **changes: Any) -> AutoState:
        """Merge field changes into the repository's state under the writer lock."""
        unknown = set(changes) - set(_TYPES)
        if unknown:
            raise ValueError(f"unknown auto state fields: {', '.join(sorted(unknown))}")
        temp_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                states = self._read_all()
                key = self._key(repo)
                new = _parse({**asdict(states.get(key, AutoState())), **changes})
                states[key] = new
                payload = {name: asdict(state) for name, state in states.items()}
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=".auto-",
                    suffix=".tmp",
                    delete=False,
                ) as temp:
                    temp_path = Path(temp.name)
                    json.dump(payload, temp, indent=2, sort_keys=True)
                    temp.write("\n")
                    temp.flush()
                    os.fsync(temp.fileno())
                os.replace(temp_path, self.path)
                return new
        except OSError as exc:
            raise AutoStateError(
                f"Cannot update {self.path}: {exc}. "
                "Check state directory permissions and free space."
            ) from exc
        finally:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)

    @contextmanager
    def run_lock(self, repo: Path) -> Iterator[None]:
        """Hold the per-repository auto run lock; raise ``AutoStateError`` if it is taken.

        The lock is a non-blocking ``flock`` on a file in the state directory, so it is
        released by the OS when the run ends or the process dies. ``baton stop`` never
        takes it.
        """
        digest = hashlib.sha256(self._key(repo).encode("utf-8")).hexdigest()[:16]
        lock_path = self.path.parent / f"auto-{digest}.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a")
        except OSError as exc:
            raise AutoStateError(
                f"Cannot open {lock_path}: {exc}. Check state directory permissions."
            ) from exc
        with handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AutoStateError(
                    f"another baton auto run is already active for {repo}. "
                    "Wait for it to finish or run `baton stop` first."
                ) from exc
            except OSError as exc:
                raise AutoStateError(f"Cannot lock {lock_path}: {exc}.") from exc
            yield

    def reset(self, repo: Path) -> None:
        """Forget a repository's state, e.g. when the roadmap is finished."""
        self.update(repo, **asdict(AutoState()))


def run_stop_command(path: Path) -> int:
    """Ask a running or future `baton auto` for this repository to stop at the next boundary."""
    try:
        AutoStore().update(path, stop_requested=True)
    except (AutoStateError, ValueError) as exc:
        print(f"Cannot request stop: {exc}")
        return 1
    print("Stop requested; baton auto stops after the current agent run.")
    return 0
