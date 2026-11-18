# baton

Command-line tool that distributes the steps of a commit plan (`.agent-plan.md`) across several
coding agent CLIs (Claude Code, Codex, Antigravity, pi). See `docs/ROADMAP.md` for the plan.

Available commands include `baton doctor`, `baton probe`, `baton status`, `baton next`,
`baton cooldown` and `baton recheck`.
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

`baton next` tries the implementation chain in order, skipping active cooldowns and continuing
with the next available agent after a usage-limit failure. Each configured name runs at most once
per implementation or fix attempt. `--agent NAME` pins implementation only; fixes and review
still use their configured role routes. Agents-only configs use the first configured Claude
agent by default and retain verification without automatic repair or review. Planning commands
belong to later milestones.

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
an unreadable or malformed plan, configuration or cooldown state. Configured agents are shown
even without a plan: each has either an active cooldown with a local reset time (including zone
and UTC offset), or no active cooldown. This is local eligibility information, not a live account
check. `status` never starts an agent or writes state.

### `baton next`

```
baton next [--path DIR] [--agent NAME] [--tier {S,M,L}] [--timeout S] [--verify-timeout S] [--dry-run]
          [--commit] [--push] [--worktree]
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
3. **Check HEAD and hand off.** After every attempt, including failures, baton checks HEAD.
   A moved HEAD stops the run. A usage-limit failure records a cooldown and passes the same step
   to the next available agent in the same working tree, with staged and unstaged diffs and
   untracked paths. Ordinary errors and timeouts stop the run without switching.
4. **Verify.** baton runs the step's **Verify** command itself (`bash -c` in the repo root); its
   result, not the agent's, decides.
5. **Repair.** In routed configs, a failed Verify invokes the effective tier's `fix` route in
   the existing working tree with the failing command, exit/timeout and bounded output. baton
   reruns the original Verify command. A second failure invokes `routes.fix.L` once, including
   when the starting tier is already L. A third failure stops with the latest diagnostic and
   preserved edits. Agent errors, changed HEAD or exhausted routes stop immediately.
6. **Review.** After successful Verify, or explicit omission of Verify, routed steps require a
   valid independent review. baton excludes every provider that participated in implementation
   or fixes, including partial work before quota handoff. Each reviewer receives the final diff
   and complete new-file contents in its prompt and runs in a fresh temporary directory. Quota
   failures can try the next eligible reviewer; ordinary errors or invalid findings stop the run.
7. **Mark and report.** The step becomes `[~] (on <HEAD>)`, a Log line is added, and the report
   lists changed files, verification, reviewer and findings grouped as **CRITICAL**, **WARNING**
   and **INFORMATIONAL**, followed by commit commands. A valid review with critical findings
   still reaches `[~]`: you decide whether to commit. The Log records reviewer and severity
   counts. baton never commits or pushes.

Exit code 0 means the step is ready to commit. Steps without Verify are reported as unverified.
Exit code 1 indicates an agent, Verify, review, plan or config error. Failed verification or
review leaves the step open and records the failure in the Log. Exit code 2 means the run stopped
before starting an agent because of the gate, a dirty tree, a missing plan or no open steps.
`--timeout` (default 1800 s) limits each implementation, fix or review agent call;
`--verify-timeout` (default 900 s) limits the Verify command. `--dry-run` prints the step, the command and the prompt and changes nothing.

The agent can edit your working tree and may use your account quota. Review the diff before
committing.

### Auto-commit, branches and worktrees

By default baton stops for your commit. Three opt-in flags change that:

- `--commit` commits a step that passed Verify and review with the plan's commit message and
  marks it `[x] – <hash>`. `.agent-plan.md` is never staged, git hooks run normally, and the
  commit uses your git identity. Before the first step baton creates (or switches to) the branch
  `baton/m<n>-<milestone-slug>` from the current HEAD; plans without a milestone use
  `baton/<plan-title-slug>`. Switching needs a clean tree (exit 2 otherwise). With a tracked
  roadmap the milestone becomes `[~]` inside the first commit and `[x]` plus a Changelog line
  inside the last one, so the roadmap changes only in those two commits.
- `--push` implies `--commit` and runs `git push -u origin <branch>` after each commit. A failed
  push exits 1, but the commit and the `[x]` status stay.
- `--worktree` implies `--commit` and works in a dedicated worktree under
  `$XDG_STATE_HOME/baton/worktrees/`, so your checkout (branch, HEAD, files) stays untouched.
  The plan is copied in on creation and mirrored back after every run, so `baton status` in your
  checkout shows the progress.

A `critical` review finding blocks the auto-commit: the step stays `[~]` with the changes in the
tree and baton exits 1 so you decide. `warning` and `informational` findings are reported and
logged only. A failing commit hook has the same effect. `--dry-run` prints the target branch.

### Fix and independent review routes

Configure each tier you use. Fixes use the selected implementation tier first and L on escalation;
review uses the selected implementation tier. For example, with the bundled agent definitions:

```toml
[routes.fix]
S = ["claude-haiku", "codex"]
M = ["claude-sonnet", "agy", "codex"]
L = ["claude-opus", "codex-astra", "agy"]

[routes.review]
S = ["claude-opus", "codex-astra", "agy"]
M = ["claude-opus", "codex-astra", "agy"]
L = ["claude-opus", "codex-astra", "agy"]
```

Opus and Astra are example L fix agents. Routes resolve only when needed: a passing Verify
requires no fix route, while review is required even when Verify is absent. Missing required
routes produce a diagnostic. Cooldowns and quota handoffs apply within each attempt; a handoff
does not consume another repair attempt. The maximum is one implementation attempt and two
repair attempts, with up to three Verify runs; each attempt may try multiple configured agents.

Provider identity is compared after trimming whitespace and ignoring case. Names sharing a
provider, such as Claude aliases and pi configured with `provider = "anthropic"`, cannot review
one another. Claude defaults to `anthropic`, Codex to `openai`; set the actual model provider for
agy and pi, including models accessed through OpenRouter. An unknown author's provider or no
eligible reviewer stops the run. Include a third independent provider if fixes may use both
Anthropic and OpenAI. `--agent` does not override either fix or review selection.

Reviewers must return JSON such as:

```json
{"findings": [{"severity": "warning", "message": "Missing boundary test", "path": "tests/test_widget.py", "line": 12}]}
```

`{"findings": []}` is a valid clean review. Empty text, prose, malformed JSON or invalid severity
values are review failures. Review inputs contain complete untracked files and a final patch;
symlinks are represented by their targets and binary new files by base64. The temporary working
directory prevents accidental edits to the candidate through relative paths; it is not a security
sandbox. A changed candidate or HEAD during review invalidates the result. `--dry-run` starts no
implementation, fix or review subprocess and does not resolve review routes.

To replay the full offline quality loop, including partial quota work and provider exclusion:

```bash
uv run pytest -q tests/test_next.py -k cli_quality_loop
```

### Cooldowns and recovery

```bash
baton cooldown claude-sonnet 2h
baton status
# After waiting for reset or signing in manually with the official CLI:
baton recheck claude-sonnet --timeout 120
```

`cooldown` requires a configured agent and a positive whole duration with `s`, `m` or `h`
(e.g. `30m`). Deadlines are stored in UTC in `$XDG_STATE_HOME/baton/limits.json`, falling back
to `~/.local/state/baton/limits.json`. They apply to that configured name across repositories.
Writes are locked and atomic; unreadable or malformed state produces an error without replacing it.
An agent becomes eligible at its deadline; expired entries may remain in the file.

A detected limit uses a parsed reset time when usable. Missing, ambiguous or past reset times
use a **1 hour fallback**, stated in the output. Supported hints include timezone-aware ISO
timestamps and relative seconds/minutes/hours; detailed fields are in [adapter notes](docs/adapters.md#usage-limit-classification).

`recheck <agent> [--timeout S]` bypasses cooldown selection and probes only that agent in an
empty temporary directory using your existing CLI login. It uses quota. Exit 0 requires parsed
success, which clears only the observed cooldown; a newer cooldown set during the probe survives.
Another quota failure refreshes the deadline and exits 1. Other failures preserve the cooldown
and exit 1. Login and account selection remain manual. Scheduled rechecks belong to M10.
`probe` remains a diagnostic command based on process exit status; use `recheck` for availability,
especially with pi, whose provider failures can exit 0.

If a chain is exhausted, baton leaves edits intact and the step open. Review and preserve that
partial work before retrying: `baton next` refuses a dirty tree without a pending `[~]` step.
Automatic continuation with partial edits happens within the current invocation.

Optional per-agent patterns **replace** the defaults. Use Python regex strings in a non-empty
array; invalid, blank and empty-text-matching patterns are rejected:

```toml
[agents.custom]
adapter = "pi"
command = "pi"
limit_patterns = ['usage[_ -]limit', 'insufficient_quota', 'provider credits exhausted']
```

Only failures are classified; successful answers mentioning limits remain successful. Generic
HTTP 429 alone does not imply exhausted usage. All four adapters have simulated quota coverage;
real M1 recordings cover success and ordinary errors only. On 2026-11-18, the user explicitly
waived the real quota recording requirement for M5 acceptance, based on the passing offline
tests. No real quota response has been captured. To reproduce the offline recovery sequence:

```bash
uv run pytest -q tests/test_next.py -k cli_limit_recovery
```

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
