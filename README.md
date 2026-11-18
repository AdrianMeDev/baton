# baton

Command-line tool that distributes the steps of a commit plan (`.agent-plan.md`) across several
coding agent CLIs (Claude Code, Codex, Antigravity, pi). See `docs/ROADMAP.md` for the plan.

Status: milestone M2 (plan engine and commit gate). Currently available: `baton --version`,
`baton doctor`, `baton probe` and `baton status`.

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

### `baton probe`

```
baton probe <agent> [--prompt TEXT] [--model NAME] [--tty] [--timeout S] [--record PATH] [--dry-run]
```

Starts the configured agent CLI headless with a tiny read-only request in an empty temporary
directory and prints the call, exit code, duration, stdout and stderr. `--record PATH` saves a
redacted JSON recording (home path, e-mail addresses and UUIDs removed), `--tty` runs the agent
through a pseudo-terminal, `--dry-run` only prints the call. The exit code is 0 only if the agent
exited with 0. A live probe uses your login's quota. Results per CLI: `docs/adapters.md`.

### `baton status`

```
baton status [--path DIR]
```

Read-only overview of the repository in `DIR` (default: current directory): the milestone from
`docs/ROADMAP.md`, all steps of `.agent-plan.md` with their markers, the current and next step, and
the commit gate. If the previous step is implemented (`[~]`) but not committed, the gate is blocked
and `status` prints the `git add -A` / `git commit -m "<message>"` hint. If it is already committed,
`status` reports that the step can be marked `[x]`; it never modifies any file. Exit code 0, or 1 for
an unreadable or malformed plan.

### Plan format

baton reads and writes `.agent-plan.md` and `docs/ROADMAP.md` in the format of the `/plan-commits`,
`/next` and `/plan-roadmap` skills. Step markers are `[ ]` open, `[~] (on <hash>)` implemented and
waiting for the commit, `[x] – <hash>` committed, `[-]` skipped. Writes are in place (markers, hash
suffixes, Log and Changelog lines only), so hand edits survive. Each step may carry an optional
`- **Size:** S|M|L` bullet; unknown bullets are ignored. Structural errors (no `## Steps`, unknown
marker, duplicate step numbers) are reported with the line number.

## Development

```
uv sync
uv run pytest -q
uv run ruff check
uv run ruff format --check
```

The same commands are available as Zed tasks in `.zed/tasks.json`. CI (`.github/workflows/ci.yml`)
runs them on Python 3.12 and 3.14.
