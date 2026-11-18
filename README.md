# baton

Command-line tool that distributes the steps of a commit plan (`.agent-plan.md`) across several
coding agent CLIs (Claude Code, Codex, Antigravity, pi). See `docs/ROADMAP.md` for the plan.

Status: milestone M0 (walking skeleton). Currently available: `baton --version` and `baton doctor`.

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```
uv tool install .
```

## Configuration

baton looks for its configuration in this order:

1. `--config <path>`
2. `$BATON_CONFIG`
3. `$XDG_CONFIG_HOME/baton/config.toml`
4. `~/.config/baton/config.toml`

If none exists, the bundled example (`src/baton/config.example.toml`) is used. To start, copy it:

```
mkdir -p ~/.config/baton
cp src/baton/config.example.toml ~/.config/baton/config.toml
```

Each `[agents.<name>]` table needs `adapter` (`claude`, `codex`, `agy` or `pi`) and `command`;
`model` is optional.

## Usage

```
baton --version
baton doctor    # shows the configured agents and whether their CLIs are in PATH
```

`doctor` exits with 0 for a valid config (missing CLIs are marked, not treated as errors) and with
1 for an invalid config.

## Development

```
uv sync
uv run pytest -q
uv run ruff check
uv run ruff format --check
```

The same commands are available as Zed tasks in `.zed/tasks.json`. CI (`.github/workflows/ci.yml`)
runs them on Python 3.12 and 3.14.
