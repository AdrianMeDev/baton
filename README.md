# baton

Command-line tool that distributes the steps of a commit plan (`.agent-plan.md`) across several
coding agent CLIs (Claude Code, Codex, Antigravity, pi). See `docs/ROADMAP.md` for the plan.

Status: milestone M3 (first end-to-end run with Claude). Currently available: `baton --version`,
`baton doctor`, `baton probe`, `baton status` and `baton next`.

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

### `baton next`

```
baton next [--path DIR] [--timeout S] [--verify-timeout S] [--dry-run]
```

Implements exactly one plan step and stops for your commit, like the `/next` skill. M3 uses one
fixed agent: the first configured agent with `adapter = "claude"` (routing and `--agent`/`--tier`
come in M4). Flow:

1. **Gate.** If a step is `[~]` and committed (HEAD is newer than the noted hash), it is marked
   `[x]` and the run continues. If it is not committed yet, baton prints the changed files and the
   `git add -A` / `git commit -m "<message>"` hint and stops. If the changes were discarded
   (clean tree, HEAD unchanged), it stops as well. A dirty tree without a `[~]` step also refuses
   to start, so the agent never mixes its work with yours.
2. **Implement.** `claude -p` runs in the repo root with the prompt for the first open step only
   (no git, do not touch `.agent-plan.md`, include tests). Flags: `docs/adapters.md`.
3. **Check HEAD.** If the agent moved HEAD, baton reports an error and does not mark the step.
4. **Verify.** baton runs the step's **Verify** command itself (`bash -c` in the repo root); its
   result, not the agent's, decides.
5. **Mark and report.** The step becomes `[~] (on <HEAD>)`, a Log line is added, and the report
   lists the changed files, the checks and the commit commands. baton never commits or pushes.

Exit codes: 0 step done and verified; 1 agent or Verify failed, or the plan/config is invalid (the
changes stay in the tree, the step stays `[ ]`, a Log line records the failure; there is no
automatic fix loop yet); 2 nothing was started (gate blocked, no open step, dirty tree, no plan).
`--timeout` (default 1800 s) limits the agent, `--verify-timeout` (default 900 s) the Verify
command. `--dry-run` prints the step, the command and the prompt and changes nothing.

Safety: the agent runs with `--permission-mode acceptEdits`, Bash allowed but `Bash(git:*)`
disallowed. It edits files in the working tree and uses your login's quota; review the diff
before committing.

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
