# Agent instructions for <project name>

Replace the placeholders with this repository's commands and conventions before copying this
file to the repository root as `AGENTS.md`. Use the same instructions for each configured agent.
If an agent CLI reads a different instruction file, have that file point to `AGENTS.md`.

## Project commands

Run these commands from the repository root.

- Install dependencies with `<setup command>`.
- Run a focused test with `<test command accepting a path or selector>`.
- Run the full suite with `<full test command>`.
- Check lint and formatting with `<lint command>` and `<format check command>`.
- Build with `<build command>`.

Follow <project conventions, source and test locations, and files to avoid editing>.

## Working on a baton step

- Read the assigned step and inspect the files it touches. Implement or repair that step only.
  Preserve partial work from earlier attempts and cover the behavior with the requested tests.
- Use the step's Verify command to check your work locally. Report the checks you ran and their
  outcomes. Baton runs the original Verify command from the working tree and records the result.
- Leave `.agent-plan.md` and Git state to baton and the operator. Change a tracked roadmap only
  when the assigned step calls for it. If the roadmap makes the step impossible, reply with
  `BATON-ROADMAP-PROBLEM: <reason>` and stop.
- For a planning or independent review task, work read-only and return the format requested
  in the baton prompt.

## Commits and publication

Plain `baton next` marks a verified step `[~]` and leaves the changes for the operator to inspect
and commit. The next invocation reconciles the commit with the plan. With `--commit`, baton owns
the commit and plan update. With `--push` or `baton auto`, baton also pushes the milestone branch.
The operator controls PR publication and merging through `baton pr` or the selected auto options.
