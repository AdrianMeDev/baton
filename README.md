# baton

Command-line tool that distributes the steps of a commit plan (`.agent-plan.md`) across several
coding agent CLIs (Claude Code, Codex, Antigravity, pi). See `docs/ROADMAP.md` for the plan.

Available commands include `baton doctor`, `baton probe`, `baton status` and `baton next`.
`baton next` can run Claude Code, Codex, Antigravity or pi.

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

Each `[agents.<name>]` table needs `adapter` (`claude`, `codex`, `agy` or `pi`) and `command`.
The optional `model` takes an ID supported by your CLI and account. pi accepts a provider-qualified
model like `openrouter/deepseek/deepseek-chat`. The optional `provider` identifies the model
provider for cross-provider review. Claude and Codex default to Anthropic and OpenAI, respectively.
Set `provider` explicitly for agy and pi when using cross-provider review.

Routes are ordered lists of configured agent names. The bundled example defines S, M and L for
each role (`plan_roadmap`, `plan_commits`, `implement`, `fix`, and `review`).

```toml
[routes.implement]
S = ["pi-ollama", "claude-haiku"]
M = ["claude-sonnet", "codex"]
L = ["claude-sonnet", "agy", "codex"]
```

`baton next` uses the first agent in the implementation chain. Planning, fixing, review and
automatic switching are planned for later milestones. Agents-only configs use the first
configured Claude agent by default.

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
baton next [--path DIR] [--agent NAME] [--tier {S,M,L}] [--timeout S] [--verify-timeout S] [--dry-run]
```

Implements exactly one plan step and stops for your commit. The tier comes from `--tier`, the
step’s `Size` field, or `M` in that order. `--agent` selects a named agent directly. For example,
`baton next --agent codex --dry-run` previews Codex, and `baton next --tier L` uses the large
implementation route.

1. **Gate.** If a step is `[~]` and committed (HEAD is newer than the noted hash), it is marked
   `[x]` and the run continues. If it is not committed yet, baton prints the changed files and the
   `git add -A` / `git commit -m "<message>"` hint and stops. If the changes were discarded
   (clean tree, HEAD unchanged), it stops as well. A dirty tree without a `[~]` step also refuses
   to start, so the agent never mixes its work with yours.
2. **Implement.** baton prints the effective tier and agent, then runs the selected CLI in the
   repo root with the prompt for the first open step. See the adapter notes for implementation
   modes and a smoke test. [Adapter notes](docs/adapters.md)
3. **Check HEAD.** If the agent moved HEAD, baton reports an error and does not mark the step.
4. **Verify.** baton runs the step's **Verify** command itself (`bash -c` in the repo root); its
   result, not the agent's, decides.
5. **Mark and report.** The step becomes `[~] (on <HEAD>)`, a Log line is added, and the report
   lists the changed files, the checks and the commit commands. baton never commits or pushes.

Exit code 0 means the step is ready to commit. Steps without Verify are reported as unverified.
Exit code 1 indicates an agent, Verify, plan or config error. Agent or Verify failures leave the
step open and record the failure in the Log. Exit code 2 means the run stopped before starting
an agent because of the gate, a dirty tree, a missing plan or no open steps.
`--timeout` (default 1800 s) limits the agent, `--verify-timeout` (default 900 s) the Verify
command. `--dry-run` prints the step, the command and the prompt and changes nothing.

The agent can edit your working tree and may use your account quota. Review the diff before
committing.

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
