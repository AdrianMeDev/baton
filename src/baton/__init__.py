"""baton: orchestrates coding agent CLIs through a commit plan."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("baton")
except PackageNotFoundError:  # pragma: no cover - only when not installed
    __version__ = "0+unknown"
