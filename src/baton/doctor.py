"""``baton doctor``: report which configured agents are ready to use."""

from __future__ import annotations

import shutil
import sys
from typing import TextIO

from baton import __version__
from baton.config import ConfigError, find_config_path, load_config

OK = "✓"
MISSING = "✗"
ORIGINS = {
    "flag": "--config",
    "env": "$BATON_CONFIG",
    "xdg": "$XDG_CONFIG_HOME",
    "home": "~/.config",
    "bundled": "bundled example",
}
MIN_PYTHON = (3, 12)
TOOLS = ("git", "gh")


def run_doctor(config_path: str | None = None, out: TextIO | None = None) -> int:
    """Print the environment report. Exit 0 for a valid config, 1 otherwise.

    Missing CLIs are flagged but not treated as errors.
    """
    out = sys.stdout if out is None else out

    py = sys.version_info
    py_ok = (py.major, py.minor) >= MIN_PYTHON
    print(f"baton {__version__}", file=out)
    print(
        f"{OK if py_ok else MISSING} python {py.major}.{py.minor}.{py.micro}"
        f" (requires >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})",
        file=out,
    )

    location = find_config_path(config_path)
    try:
        config = load_config(location)
    except ConfigError as exc:
        print(f"{MISSING} config: {exc}", file=out)
        return 1

    note = " - no user config found, showing example agents" if location.is_bundled else ""
    print(f"{OK} config: {location.path} ({ORIGINS[location.origin]}){note}", file=out)

    print("\nAgents:", file=out)
    if not config.agents:
        print("  (none configured)", file=out)
    else:
        rows = [
            (
                agent.name,
                agent.adapter,
                agent.model or "-",
                agent.command,
                shutil.which(agent.command),
            )
            for agent in config.agents.values()
        ]
        widths = [max(len(row[i]) for row in rows) for i in range(4)]
        for name, adapter, model, command, found in rows:
            mark = OK if found else MISSING
            where = found or "not found in PATH"
            print(
                f"  {mark} {name:<{widths[0]}}  {adapter:<{widths[1]}}  {model:<{widths[2]}}"
                f"  {command:<{widths[3]}}  {where}",
                file=out,
            )

    print("\nTools:", file=out)
    for tool in TOOLS:
        found = shutil.which(tool)
        print(f"  {OK if found else MISSING} {tool}  {found or 'not found in PATH'}", file=out)
    return 0
