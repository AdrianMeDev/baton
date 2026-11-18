# Adapter notes: headless behaviour of the agent CLIs

The probe recordings live in `tests/fixtures/<adapter>/` and are the reference for the adapters
and future limit detection.
All runs used the prompt `Reply with exactly: OK` in an empty temporary directory.

CLI versions: claude 2.1.311, codex 0.169.0, agy 1.5.11, pi 1.5.0.

## Summary

| Adapter | Headless call | Read-only flag | Output | Answer is in | Exit code on bad model |
|---|---|---|---|---|---|
| claude | `claude -p <prompt> --output-format json` | `--permission-mode plan` | one JSON object | `result` | 1 |
| codex | `codex exec <prompt> --json` | `--sandbox read-only` | JSON lines | `item.completed` with `item.type == "agent_message"`, field `item.text` | 1 |
| agy | `agy -p <prompt> --output-format json` | `--mode plan` | one JSON object | `response` | 1 |
| pi | `pi -p <prompt> --mode json` | none found (tools `read`, `bash`, `edit`, `write` are on) | JSON lines | last `message_end` with `message.role == "assistant"`, `message.content[].text` | **0** |

Model selection: `--model <name>` (claude, agy, pi; pi takes `provider/model`) and `-m <name>` (codex).
Probes never read tokens or auth files; the CLIs use the login the user already has.

## claude

- Call: `claude -p <prompt> --output-format json --permission-mode plan [--model M]`.
- stdout is a single JSON object, `type == "result"`. Success: `is_error: false`,
  `subtype: "success"`, `terminal_reason: "completed"`, answer in `result`. Also: `total_cost_usd`,
  `usage`, `session_id`, `duration_ms`.
- Error (unknown model): exit 1, `is_error: true`, `api_error_status: 404`,
  `terminal_reason: "api_error"`, human-readable text in `result`; stderr has
  `[claude-code:unrecognized_model] {...}`. Note that `subtype` is still `"success"`, so check
  `is_error`, not `subtype`.
- Limit message: not verified (no real limit was hit). Expect `is_error: true` with the text in
  `result`; the wording must be recorded when it occurs.
- Fixtures: `tests/fixtures/claude/ok.json`, `error.json`.

### Implement call (`baton next`)

`baton next` uses a write-enabled call, built by `ClaudeAdapter.argv()` in `src/baton/adapter.py`:

```
claude -p <prompt> --output-format json [--model M] \
  --permission-mode acceptEdits --allowedTools Bash --disallowedTools "Bash(git:*)"
```

- `acceptEdits` lets the agent change files without prompting; there is no plan mode here.
- `Bash` is allowed so the agent can run tests; `Bash(git:*)` is disallowed, so the agent cannot
  commit, stash or reset. This is a guard, not a sandbox: baton additionally compares HEAD before
  and after the run and refuses to mark the step if it moved.
- A run fails on a nonzero exit, `is_error: true`, empty or invalid JSON, or a timeout.

## codex

- Call: `codex exec <prompt> --json --sandbox read-only --skip-git-repo-check [-m M]`.
  `--skip-git-repo-check` is needed because the probe runs outside a git repository.
- stdout is JSON lines: `thread.started`, `turn.started`, `item.completed` (answer:
  `item.type == "agent_message"`, `item.text`), `turn.completed` (with `usage`).
- stderr always contains `Reading additional input from stdin...`; ignore it.
- Error (unknown model): exit 1, an `item.completed` with `item.type == "error"` (warning about
  model metadata), then `{"type":"error","message":...}` and `{"type":"turn.failed","error":{"message":...}}`.
  The message says the model is not supported with a ChatGPT account. Detect failure by exit code
  and `turn.failed`.
- Limit message: not verified. Expect it in `turn.failed.error.message`.
- Fixtures: `tests/fixtures/codex/ok.json`, `error.json`.

### Implement call

`codex exec --json --sandbox workspace-write [-m M] -- <prompt>` runs in the repository.
Success requires exit 0, a `turn.completed` event and a final agent message.

## agy (Antigravity)

- Call: `agy -p <prompt> --output-format json --mode plan [--model M]`.
- stdout is a single JSON object: `status` (`"SUCCESS"` or `"ERROR"`), `response` (answer, with
  trailing newline), `error` (only on failure), `conversation_id`, `num_turns`, `usage`.
- Error (unknown model): exit 1, `status: "ERROR"`, `response: ""`, `error` lists the available
  model names; the same text is on stderr.
- Limit message: not verified.
- Fixtures: `tests/fixtures/agy/ok.json`, `ok-notty.json`, `ok-tty.json`, `error.json`.

### Implement call

`agy -p <prompt> --output-format json --mode accept-edits [--model M]` uses ordinary pipes.
Both pipe and terminal probes returned valid JSON on 1.5.11. An empty response stream fails
the implementation run. `baton probe agy --tty` remains available for investigating terminal
behavior.

## pi

- Call: `pi -p <prompt> --mode json --model <provider>/<model>`. The recorded run used the
  `zai` provider (`zai/glm-5.3-flash`); local Ollama and OpenRouter models are selected the same way.
- stdout is JSON lines: `session`, `agent_start`, `turn_start`, `message_start`/`message_update`/
  `message_end` per message, `turn_end`, `agent_end`, `agent_settled`. The answer is the text in
  the last `message_end` whose `message.role` is `assistant`; `message.usage` has token counts and cost.
- The first events contain the full system prompt; the recordings are therefore large.
- No read-only or sandbox flag was found: pi offers `read`, `bash`, `edit` and `write` tools.
  Read-only runs must be enforced outside pi (own worktree, HEAD check, see roadmap risks).
- Error (unknown model): **exit code is 0.** pi only warns on stderr (`Model "..." not found for
  provider "zai". Using custom model id.`) and the failure appears in the stream: the assistant
  message has `stopReason: "error"` and `errorMessage` (for example
  `400: {"code":"1211","message":"Unknown Model, please check the model code."}`). Detection must
  inspect `stopReason`/`errorMessage`, not the exit code.
- Limit message: not verified; expect it in `errorMessage` (HTTP 429 or provider-specific text).
- Fixtures: `tests/fixtures/pi/ok.json`, `error.json`.

### Implement call

Implementation uses the same pi arguments as the probe. The configured `model` is passed
unchanged. baton waits for `agent_settled`, checks the final assistant stop reason and sums the
available assistant message costs.

## Disposable-repository smoke test

Run this from the baton checkout with its development environment installed. Each selected agent
gets a fresh repository. Use names and model IDs from your config if they differ from the example.
The agent timeout defaults to 1,800 seconds and the Verify timeout to 900 seconds.

```bash
set -e
config="$PWD/src/baton/config.example.toml"
for agent in claude-sonnet codex-astra agy pi-ollama pi-openrouter; do
  repo=$(mktemp -d)
  git -C "$repo" init -qb main
  cat > "$repo/.agent-plan.md" <<'PLAN'
# Plan: Adapter smoke
Milestone: none

## Steps

### 1. [ ] test: write a smoke marker
- **Changes:** Create smoke.txt containing the single line baton-smoke.
- **Verify:** `grep -Fxq baton-smoke smoke.txt`
PLAN
  git -C "$repo" add .agent-plan.md
  git -C "$repo" -c user.name=Baton -c user.email=baton@example.invalid commit -qm init
  before=$(git -C "$repo" rev-parse HEAD)
  uv run baton --config "$config" next --path "$repo" --agent "$agent"
  grep -Fxq baton-smoke "$repo/smoke.txt"
  grep -Fq "[~] (on ${before:0:7})" "$repo/.agent-plan.md"
  test "$(git -C "$repo" rev-parse HEAD)" = "$before"
  printf '%s passed in %s\n' "$agent" "$repo"
done
```

### M4 smoke results

Claude Sonnet, Codex Astra, agy with Gemini 4 Argon High, and pi with Z.AI
(`zai/glm-5.3-flash`) each edited a disposable repository, passed Verify and kept HEAD
unchanged. agy timed out at 90 seconds and passed on a fresh run with a 150-second timeout.
The example Ollama route could not connect to its backend. The example OpenRouter route
lacked an API key.

## Recording format

`baton probe --record PATH` writes JSON with `adapter`, `agent`, `argv`, `tty`, `exit_code`,
`timed_out`, `duration_s`, `stdout`, `stderr`, `recorded_at`, `cli_version`. Before saving, the
home path becomes `~`, the temporary working directory `<workdir>`, e-mail addresses `<email>` and
UUIDs `<uuid>`. Check new recordings for personal data before committing them.

## Open points for M5 (limit detection)

No real limit message was recorded for any CLI because limits cannot be triggered on purpose. When
one occurs, run `baton probe <agent> --record tests/fixtures/<adapter>/limit.json` and add the
wording here.
