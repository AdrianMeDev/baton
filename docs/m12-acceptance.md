# M12 acceptance: fresh machine to a finished `baton auto`

M12 is done when the README leads from a fresh machine to a completed `baton auto` run. This
checklist records that run. Follow the README literally; every place where you had to deviate
from it is a README bug to fix before M12 is marked `[x]`.

## Setup

- [ ] **Fresh environment:** a new WSL distribution, VM, container or Linux user account with no
      baton, uv, `~/.config/baton`, `~/.local/state/baton` or agent CLI configuration from earlier
      runs. Note what you used: ____________
- [ ] **Disposable GitHub repository** with permission to push branches, run Actions, open PRs
      and squash-merge. Use the README's greeting demo, or create a larger one with the
      [test project prompt](#test-project-prompt) below. Repository: ____________
- [ ] Agent CLIs installed and on `PATH` in the fresh environment (README section 1). Versions:
      claude ______ codex ______ gh ______ uv ______

## README walkthrough

Tick each item only if the README command worked as written.

- [ ] Section 1: `uv tool install` succeeds, `baton --version` prints the version, all logins
      succeed, `gh auth status` is green.
- [ ] Section 2: config saved, `baton doctor` finds both agents, `git` and `gh`.
- [ ] Section 2: `baton probe writer` and `baton probe reviewer` succeed.
- [ ] Section 3: repository cloned, `AGENTS.md`, `docs/ROADMAP.md` and CI workflow committed and
      pushed; `git status --short --branch` shows a clean `main`.
- [ ] Section 4: `baton auto` runs without manual intervention and ends with exit code 0
      (`echo $?`).
- [ ] Output contains `Merged M<n>.` for every milestone, followed by `Roadmap complete.`
- [ ] `gh pr view '<PR URL>' --json state,mergeCommit` shows `MERGED` for every milestone PR.
- [ ] Local `HEAD` equals the merge commit of the last PR, the tree is clean.
- [ ] Every milestone in `docs/ROADMAP.md` is `[x]`.
- [ ] The project's own test command passes on the merged `main`.
- [ ] No milestone worktree is left behind (`git worktree list` shows only the base checkout).
- [ ] Each milestone branch has one commit per plan step with the plan's commit message
      (`gh pr view '<PR URL>' --json commits`).

## Optional checks

These are covered by offline tests; run them live only if time and quota allow.

- [ ] `baton stats` lists the attempts of the run per agent.
- [ ] `baton status` shows agent availability and no open plan.
- [ ] **Resume:** interrupt `baton auto` with Ctrl+C during a step, run `baton auto` again; it
      continues the same milestone and step.
- [ ] **Stop:** run `baton stop` in a second shell during a run; `baton auto` stops before the
      next step with exit code 1 and a notification (if configured).
- [ ] **No merge:** `baton auto --no-merge` stops after publishing the PR with exit code 0.
- [ ] **Notifications:** with `[auto] notify = "notify-send"` or `"ntfy"`, a message arrives for
      each merged milestone and for the completed roadmap.

## Result

- Date: ____________
- baton commit: ____________ (`git -C "$baton_source" rev-parse --short HEAD`)
- Duration of `baton auto`: ____________
- README deviations found (each one becomes a fix before acceptance):
  - ____________
- Verdict: [ ] accepted – mark M12 `[x]` and add a changelog entry with the PR links ·
  [ ] not accepted – reason: ____________

## Test project prompt

Use this prompt with an interactive agent (for example Claude Code) in an empty directory to set
up a disposable test repository with two milestones. It only prepares the repository; baton
implements the milestones. Two milestones exercise the loop from one merged milestone to the
next, which the single-milestone README demo does not.

````text
Set up a small disposable test repository for an end-to-end test of the tool "baton",
which plans and implements roadmap milestones with coding agents and merges one PR per
milestone. Do NOT implement any of the milestones yourself; only prepare the repository.

Project: "wordstat", a tiny Python 3.12 command-line tool with no dependencies besides the
standard library and pytest for tests.

Create exactly these files:

1. `README.md`: one paragraph describing wordstat and a "Development" section with
   `uv run pytest -q` as the test command.
2. `pyproject.toml`: project "wordstat", requires-python ">=3.12", package in `src/wordstat/`,
   pytest as a dev dependency (dependency group "dev"), a `wordstat` console script pointing to
   `wordstat.cli:main`.
3. `src/wordstat/__init__.py` with `__version__ = "0.1.0"`, and `src/wordstat/cli.py` with a
   `main()` that only prints the version for `--version` (argparse).
4. `tests/test_cli.py` with one passing test for `--version`.
5. `AGENTS.md`: agents only change code, never run git commands, keep changes to the current
   step, run `uv run pytest -q` before finishing; Python 3.12, type hints, standard library only.
6. `.github/workflows/ci.yml`: on `pull_request` and on `push` to `main`, set up uv
   (astral-sh/setup-uv), run `uv sync` and `uv run pytest -q`.
7. `docs/ROADMAP.md` with exactly this structure (keep headings, field names and status markers
   exactly as shown):

   # Roadmap: wordstat

   ## Milestones

   ### M1 [ ] Count words
   - **Goal:** `wordstat count FILE` prints the number of words in FILE.
   - **Includes:** a `count_words(text) -> int` function in `src/wordstat/core.py` with tests,
     the `count` subcommand with tests, README usage. Plan one commit per item in that order.
   - **Done when:** `uv run pytest -q` passes and the README shows `wordstat count`.
   - **Depends on:** –
   - **Size:** S

   ### M2 [ ] Top words
   - **Goal:** `wordstat top FILE -n N` prints the N most frequent words, case-insensitive,
     one `word count` pair per line, ties sorted alphabetically.
   - **Includes:** a `top_words(text, n) -> list[tuple[str, int]]` function with tests, the
     `top` subcommand with `-n` (default 5) and tests, README usage. Plan one commit per item.
   - **Done when:** `uv run pytest -q` passes and the README shows `wordstat top`.
   - **Depends on:** M1
   - **Size:** S

   ## Definition of done
   - `uv run pytest -q` passes locally and in the pull request CI job.

   ## Changelog

8. `.gitignore` for Python and uv (`.venv/`, `__pycache__/`, `*.egg-info/`).

Then:
- Run `uv sync` and `uv run pytest -q`; both must succeed. Commit `uv.lock` too.
- `git init -b main`, commit everything as
  `chore: prepare wordstat test project for baton`.
- Show me the command to create the GitHub repository
  (`gh repo create <name> --private --source . --push`) and wait for my confirmation before
  running it. After the push, enable squash merging with
  `gh repo edit --enable-squash-merge` and check that the CI run on `main` is green.
- Finish with a short summary: repository URL, CI result, and the next command to run
  (`baton doctor`, then `baton auto`).
````
