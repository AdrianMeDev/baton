# Adapter notes: headless behaviour of the agent CLIs

Findings of milestone M1, recorded with `baton probe` (see `README.md`). The recordings live in
`tests/fixtures/<adapter>/` and are the reference for the adapters (M3-M5) and limit detection (M6).
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

## agy (Antigravity)

- Call: `agy -p <prompt> --output-format json --mode plan [--model M]`.
- stdout is a single JSON object: `status` (`"SUCCESS"` or `"ERROR"`), `response` (answer, with
  trailing newline), `error` (only on failure), `conversation_id`, `num_turns`, `usage`.
- Error (unknown model): exit 1, `status: "ERROR"`, `response: ""`, `error` lists the available
  model names; the same text is on stderr.
- Limit message: not verified.
- Fixtures: `tests/fixtures/agy/ok.json`, `ok-notty.json`, `ok-tty.json`, `error.json`.

### Decision: no pseudo-terminal for agy

The roadmap risk was that `agy -p` prints nothing without a terminal. With 1.5.11, the run without
a terminal (`ok-notty.json`, identical to `ok.json`) returns the full JSON answer and exit 0, and
the run through a pseudo-terminal (`ok-tty.json`) returns the same content. So the adapter uses
plain pipes. The runner keeps `tty=True` (`baton probe agy --tty`) as a fallback should an agy
update bring the bug back; the adapter should treat "exit 0 but empty stdout" as a failure and
may retry once with a terminal.

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

## Recording format

`baton probe --record PATH` writes JSON with `adapter`, `agent`, `argv`, `tty`, `exit_code`,
`timed_out`, `duration_s`, `stdout`, `stderr`, `recorded_at`, `cli_version`. Before saving, the
home path becomes `~`, the temporary working directory `<workdir>`, e-mail addresses `<email>` and
UUIDs `<uuid>`. Check new recordings for personal data before committing them.

## Open points for M6 (limit detection)

No real limit message was recorded for any CLI because limits cannot be triggered on purpose. When
one occurs, run `baton probe <agent> --record tests/fixtures/<adapter>/limit.json` and add the
wording here.
