# Roadmap: baton – Plan-Schritte auf mehrere KI-Agenten verteilen
Updated: 2026-11-19

## Vision
`baton` ist ein Kommandozeilen-Tool für private Projekte. Es verteilt die Arbeit aus `docs/ROADMAP.md` und `.agent-plan.md` je nach Schwierigkeit auf Claude Code, Codex, Antigravity (`agy`) und pi (lokal über Ollama oder per API, z. B. DeepSeek über OpenRouter). Ist ein Agent am Limit, übernimmt der nächste. Im **Auto-Modus** reicht eine Roadmap: baton plant jeden Meilenstein, setzt die Schritte um, committet, erstellt Pull Requests und macht weiter, bis die Roadmap erledigt ist.

## Scope
- **MVP:** `baton next` (ein Schritt, Stopp für deinen Commit), Adapter claude/codex/agy/pi, Routing nach Stufe S/M/L, Limit-Erkennung mit automatischem Wechsel, Prüfung über den Verify-Befehl, Nachbessern/Eskalieren/Review, `baton status`, `baton doctor`
- **Auto-Modus:** `baton plan` (Meilenstein headless planen), Auto-Commit, Branch + PR pro Meilenstein, `baton auto` (ganze Roadmap am Stück, Warten auf Limit-Reset, Fortsetzen nach Absturz, Benachrichtigungen)
- **Später:** parallele Meilensteine in mehreren Worktrees, TUI, Windows nativ, GitLab (`glab`), MCP-Server-Modus
- **Nicht geplant:** Abo-Logins an den offiziellen CLIs vorbei nutzen (Nutzungsbedingungen), automatische Rotation zwischen Konten, Mehrbenutzer-Betrieb, Firmen-Einsatz

## Architecture
- **Stack:** Python 3.12+, `uv` (`uv tool install`), Standardbibliothek zuerst (`argparse`, `tomllib`, `subprocess`, `pty`), `pytest`, `ruff`, GitHub Actions. Git-Hosting über `gh`. Zielplattform Linux/WSL.
- **Komponenten:**
  ```
  baton CLI ─► Plan-Engine   ROADMAP.md + .agent-plan.md lesen/schreiben, Commit-Sperre
     │       ─► Router        Rolle × Stufe → Agenten-Kette, Cooldowns, Budgets
     │       ─► Adapter       claude | codex | agy | pi  → offizielles CLI, headless
     │       ─► Verifier      Verify-Befehl selbst ausführen
     │       ─► Git/Forge     Branch, Commit, Push, PR, CI-Status, Merge (git + gh)
     │       ─► Auto-Loop     Meilenstein → Plan → Schritte → PR → nächster Meilenstein
     └───────► State & Log    ~/.local/state/baton: limits.json, runs.jsonl, auto.json
  ```
- **Rollen:** `plan` (Meilenstein → Schritte), `implement` (S/M/L), `fix`, `review`. Jede Rolle hat ihre eigene Agenten-Kette, z. B. plan: Opus → Argon; L: Sonnet → Argon → Codex; S: Qwen lokal → Haiku.
- **Core data:** `Roadmap` 1─n `Milestone` · `Plan` 1─n `Step` (Commit-Message, Status, Stufe, Verify) · `Agent` (Adapter, Modell, Limit-Muster) · `Route` · `Run` · `Cooldown` · `AutoState` (aktueller Meilenstein/Schritt, Branch, PR)
- **Konfiguration:** `~/.config/baton/config.toml` (Agenten, Routen, Auto-Regeln) + optional `.baton.toml` im Projekt

## Decisions
| # | Decision | Why | Alternative |
|---|---|---|---|
| 1 | Python + uv, Standardbibliothek zuerst | vertraut, einfache Subprozesse/PTY | TypeScript/Node |
| 2 | Nur offizielle CLIs mit eigenem Login | Nutzungsbedingungen | direkte API-Aufrufe |
| 3 | pi als Adapter für API-Modelle und Ollama | ein Adapter, viele Anbieter | eigener API-Client |
| 4 | baton verwaltet Status, Prüfung, Git; Agenten ändern nur Code | zuverlässig über Modelle hinweg, nie Commits ohne grüne Prüfung | Agenten committen selbst |
| 5 | Dateiformat wie `/plan-commits` und `/next` | interaktiv und automatisch mischbar | eigene Datenbank |
| 6 | `next`: du committest · `auto`: baton committet mit der Message aus dem Plan | Review-Gate bleibt wählbar | immer automatisch |
| 7 | Ein Branch und ein PR pro Meilenstein | übersichtliche, reviewbare Einheiten | ein PR pro Schritt |
| 8 | Auto-Merge nur bei grüner CI und Review ohne kritische Befunde (abschaltbar) | Qualität vor Tempo | immer manuell mergen |
| 9 | Auto-Modus arbeitet in eigenem Git-Worktree | dein Arbeitsverzeichnis bleibt frei | im Hauptordner |
| 10 | Bei Limit-Wechsel mit dem aktuellen Diff weitermachen | keine Arbeit geht verloren | Schritt zurücksetzen |
| 11 | Für jeden Anbieter (claude, codex, agy, pi-API) gilt: ein Konto, baton rotiert nie selbst zwischen Konten, liest keine Tokens aus und startet nur die offiziellen CLIs. Du meldest dich bei Bedarf von Hand um, baton erkennt es per `recheck` | Nutzungsbedingungen (Umgehung von Limits per Programm gilt als Verstoß, manueller Wechsel nach Aussagen der Anbieter nicht) | automatische Konten-Rotation, Proxys mit Abo-Tokens |

## Risks & unknowns
| Risk | Impact | Mitigation |
|---|---|---|
| Headless-Flags/Ausgaben der CLIs ändern sich | Adapter brechen | isolierte Adapter, aufgezeichnete Ausgaben als Testdaten, `baton doctor`/`probe` |
| `agy -p` ohne Terminal teils ohne Ausgabe (bekannter Bug) | Antigravity unbrauchbar | M1: mit agy 1.5.11 nicht reproduzierbar, Pipes genügen; Fallback Pseudo-Terminal (`--tty`) bleibt, siehe `docs/adapters.md` |
| pi liefert bei Fehlern (z. B. unbekanntes Modell) Exit 0 und hat kein Read-only-Flag | Fehler unbemerkt, Schreibzugriff | Fehler im JSON-Strom prüfen (`stopReason`/`errorMessage`), Schutz über Worktree und HEAD-Prüfung |
| Limit-Meldungen variieren | Wechsel greift nicht | Muster je Agent, Reset-Zeit parsen, `baton cooldown <agent> 2h` |
| Agenten laufen ohne Rückfragen | ungewollte Änderungen | eigener Worktree, Schreibrechte nur im Projekt, HEAD-Prüfung, kein git im Agenten-Prompt |
| Auto-Modus produziert schlechten Code am Stück | Fehler landen in `main` | Verify + CI + Review durch anderen Anbieter, Merge nur bei grün, Stopp-Regeln |
| Endlosschleifen oder hoher Verbrauch | Kontingente leer | Versuchs-Grenzen, Tages-Budgets, Stopp bei Eskalations-Ende, `baton stop` |
| Headless-Plan ist schlecht geschnitten | Folgefehler | Plan-Validierung (Struktur, Schrittzahl, Verify vorhanden), Stopp statt Raten |

## Milestones

### M0 [x] Walking skeleton
- **Goal:** Lauffähiges, getestetes Grundgerüst mit Konfiguration.
- **Includes:** uv-Projekt, `baton --version`, `config.toml` laden + Beispiel, `baton doctor` (Agenten, CLIs im PATH), ruff, pytest, CI, `.zed/tasks.json`
- **Done when:** `uv run baton doctor` zeigt die Beispiel-Agenten; CI grün
- **Depends on:** –
- **Size:** S, ~4 commits

### M1 [x] Spike: Headless-Verhalten der CLIs
- **Goal:** Gesichertes Wissen über Aufruf, Rechte, Ausgabe und Limit-Meldungen von claude, codex, agy, pi.
- **Includes:** `docs/adapters.md`, `baton probe <agent>`, echte Ausgaben unter `tests/fixtures/`, Entscheidung zum agy-Terminal-Problem
- **Done when:** je CLI ein Probe-Lauf und ein Fehler-/Limit-Beispiel als Testdatei
- **Depends on:** M0
- **Size:** M, ~5 commits

### M2 [x] Plan-Engine und Commit-Sperre
- **Goal:** `.agent-plan.md` und `ROADMAP.md` exakt im Skill-Format lesen und schreiben.
- **Includes:** Parser/Writer (Status, Stufe, Verify, Milestone), Commit-Sperre wie `next`, `baton status`
- **Done when:** Tests mit Beispielplänen und temporären Git-Repos decken alle Zustände ab
- **Depends on:** M0
- **Size:** M, ~5 commits

### M3 [x] Erster Durchstich mit Claude
- **Goal:** `baton next` setzt einen Schritt mit `claude -p` um und stoppt für den Commit.
- **Includes:** Prompt-Vorlage (nur dieser Schritt, kein git), Claude-Adapter, Verify ausführen, `[~]` setzen, Bericht + Commit-Befehl
- **Done when:** Beispiel-Repo: Schritt umgesetzt, geprüft, markiert; HEAD unverändert
- **Depends on:** M1, M2
- **Size:** M, ~5 commits

### M4 [x] Alle Adapter und Routing
- **Goal:** Jede Rolle und Stufe geht an ihre Agenten-Kette.
- **Includes:** Adapter codex, agy, pi (Ollama + OpenRouter), Routen je Rolle × Stufe (Rollen: plan_roadmap, plan_commits, implement, fix, review), `--agent`/`--tier`, Standardstufe M, Timeouts. Beispielkonfiguration: plan_roadmap und plan_commits(L) → Opus/Astra/Argon; plan_commits(M/S) → Sonnet/Argon; implement(L) → Sonnet/Argon/Codex; review → stärkstes Modell eines anderen Anbieters
- **Done when:** ein Beispielschritt läuft per Override über jeden Adapter; S/M/L laut Konfiguration verteilt
- **Depends on:** M3
- **Size:** L, ~7 commits

### M5 [x] Limit-Erkennung und Wechsel
- **Goal:** Am Limit übernimmt automatisch der nächste Agent.
- **Includes:** Limit-Muster, Cooldowns mit Reset-Zeit, Wechsel mit aktuellem Diff, `baton cooldown`, Verfügbarkeit in `status`, Wiedererkennung nach manuellem Kontowechsel bei jedem Adapter: `baton recheck <agent>` startet eine winzige Probeanfrage und hebt den Cooldown bei Erfolg auf, im Auto-Modus optional mit Hinweis "agy im Cooldown, anmelden mit anderem Konto oder warten bis HH:MM" und alle paar Minuten erneuter Probe; kein Anmelden, keine Token-Dateien anfassen
- **Done when:** simuliertes Limit löst Wechsel aus; echte Meldungen aus M1 werden erkannt; Fake-Adapter: erst Limit-Fehler, nach `recheck` Erfolg → Cooldown aufgehoben
- **Depends on:** M4
- **Size:** M, ~5 commits

### M6 [x] Qualitätsschleife
- **Goal:** Fehlschläge selbst reparieren, schwierige Schritte eskalieren, unabhängig reviewen.
- **Includes:** Fix-Versuch mit Fehlerausgabe, Eskalation zur höheren Stufe (nach 2 Fehlschlägen auf L: Opus/Astra), Review durch anderen Anbieter (Schweregrade), Befunde im Bericht
- **Done when:** Fake-Adapter-Tests zeigen Fix → Eskalation; Review nie beim selben Anbieter
- **Depends on:** M5
- **Size:** M, ~5 commits

### M7 [x] Auto-Commit und Branches
- **Goal:** baton committet grüne Schritte selbst auf einem Meilenstein-Branch.
- **Includes:** `baton next --commit`, Branch `baton/m<n>-<name>`, Commit mit Plan-Message, Roadmap-Status im ersten/letzten Commit, Push, eigener Worktree
- **Done when:** ein Meilenstein im Beispiel-Repo endet als Branch mit einem Commit pro Schritt, alle Commits grün
- **Depends on:** M6
- **Size:** M, ~5 commits

### M8 [x] Headless planen
- **Goal:** `baton plan` macht aus dem nächsten offenen Meilenstein eine gültige `.agent-plan.md`.
- **Includes:** Planungs-Prompt nach den Regeln von `plan-commits` (inkl. Stufe je Schritt), Rolle `plan`, Validierung (Struktur, 3–8 Schritte, Verify je Schritt), bei ungültigem Plan ein zweiter Versuch, dann Stopp
- **Done when:** für die Beispiel-Roadmap entstehen gültige Pläne; ungültige werden abgewiesen
- **Depends on:** M4, M2
- **Size:** M, ~5 commits

### M9 [x] Pull Requests und Merge
- **Goal:** Jeder fertige Meilenstein wird ein PR und je nach Regel gemergt.
- **Includes:** `gh pr create` mit Beschreibung (Ziel, Schritte, Prüfungen, Review-Befunde), auf CI warten, Auto-Merge bei grün und ohne kritische Befunde, sonst offen lassen und stoppen
- **Done when:** im Test-Repo entsteht ein PR, der bei grüner CI gemergt wird und bei roter offen bleibt
- **Depends on:** M7
- **Size:** M, ~5 commits

### M10 [x] Auto-Modus
- **Goal:** `baton auto` arbeitet die ganze Roadmap ohne Zutun ab.
- **Includes:** Schleife Meilenstein → Plan → Schritte → PR → Merge → nächster; Warten bis zum frühesten Limit-Reset, wenn alle Agenten pausieren; Fortsetzen nach Absturz aus Dateien + Git; Stopp-Regeln (Eskalation erschöpft, Merge abgelehnt, Roadmap-Problem gemeldet, Budget erreicht); `baton stop`; Benachrichtigung bei Stopp/Meilenstein (`notify-send` oder ntfy)
- **Done when:** eine Beispiel-Roadmap mit 2 Meilensteinen läuft mit Fake-Adaptern vollständig durch; jede Stopp-Regel hat einen Test
- **Depends on:** M8, M9
- **Size:** L, ~7 commits

### M11 [x] Auswertung und Verteilung
- **Goal:** Sehen, wer wie viel arbeitet, und Kontingente vorausschauend verteilen.
- **Includes:** `runs.jsonl`, `baton stats` (Läufe, Dauer, Tokens je Agent), Tages-Budgets und Gewichte im Router
- **Done when:** `stats` zeigt die Verteilung; überschrittenes Budget lenkt um
- **Depends on:** M5
- **Size:** M, ~4 commits

### M12 [~] Integration und Doku
- **Goal:** baton fügt sich nahtlos in den Workflow ein.
- **Includes:** Stufen-Feld für den Skill `plan-commits` unter `integrations/`, gemeinsame `AGENTS.md`-Vorlage, Zed-Tasks `baton next`/`baton auto`, README (Installation, Konfiguration, Auto-Modus, Sicherheit)
- **Done when:** README führt auf einem frischen Rechner bis zu einem durchgelaufenen `baton auto`
- **Depends on:** M10, M11
- **Size:** S, ~4 commits

## Definition of done (every milestone)
- Baut, Tests grün, `ruff check` und `ruff format --check` sauber, CI grün
- Neue Logik hat Tests; CLIs in Tests nur über Fake-Adapter oder aufgezeichnete Ausgaben, nie live
- README bzw. `docs/` aktualisiert, wenn sich Bedienung oder Konfiguration ändert

## Changelog

- 2026-11-20: `baton audit --jobs N` reviews up to N batches at the same time (one adapter per worker, results recorded in batch order, stops record every finished batch); default `--jobs 1` is unchanged.
- 2026-11-20: `baton audit` reviews every tracked file batch by batch into `docs/AUDIT.md` (inventory, findings, fix milestones, suggestions; resumable, read-only reviewer); `baton audit --fix` works through the fix milestones via `baton auto --roadmap docs/AUDIT.md`; `baton plan` and `baton auto` accept `--roadmap PATH`.
- 2026-11-19: Review fix loop: a critical review finding with a location is handed to the `fix` route (tier of the step, then L), the step is verified and reviewed again, and only the last review becomes the evidence; a finding that survives two rounds still blocks the auto-commit. Review prompt: concerns that cannot be shown in the diff are warnings, not critical.
- 2026-11-19: Plan repair and `baton roadmap`: a `BATON-ROADMAP-PROBLEM` from an implementing step makes an L-tier read-only planner propose replacement steps that baton validates and writes (`repair.py`, logged and listed in the PR, one repair per step, `BATON-NEEDS-DECISION` stops); `baton roadmap "<idea>"` writes `docs/ROADMAP.md` after up to six questions with defaults; `baton next --resume`; README quick start from install to `baton auto`.
- 2026-11-19: Hardening after the implementation review ([#19](https://github.com/AdrianMeDev/baton/pull/19), [CI 44481925450](https://github.com/AdrianMeDev/baton/actions/runs/44481925450) green): prompts reach claude and codex via stdin and large agy/pi prompts via a private file, reviewer context is bounded, planners and reviewers run with read-only agent flags, the run journal survives a torn last line, a second concurrent `baton auto` per repository is refused, the suggested commit command is shell-quoted, and the README documents the trust model and security limits.
- 2026-11-19: Auto hardening ([#18](https://github.com/AdrianMeDev/baton/pull/18), [CI 44429010427](https://github.com/AdrianMeDev/baton/actions/runs/44429010427) green): unattended runs are hardened and the periodic cooldown rechecks deferred from M5 are available in auto mode (`[auto] recheck_interval_seconds`, opt-in). [#17](https://github.com/AdrianMeDev/baton/pull/17) makes `baton next` reject agent changes to the active plan.
- 2026-11-19: M12 integration delivered ([#16](https://github.com/AdrianMeDev/baton/pull/16), [CI 44418863354](https://github.com/AdrianMeDev/baton/actions/runs/44418863354) green): size-aware `plan-commits` skill under `integrations/`, shared `AGENTS.md` template, Zed tasks `baton next`/`baton auto`, README walkthrough from a fresh install to a merged `baton auto` run. M12 remains [~]: the walkthrough has not yet been run end to end on a fresh machine; see the [acceptance checklist](m12-acceptance.md).
- 2026-11-19: M10 and M11 CI confirmed: [#14](https://github.com/AdrianMeDev/baton/pull/14) ([CI 44412423202](https://github.com/AdrianMeDev/baton/actions/runs/44412423202)) and [#15](https://github.com/AdrianMeDev/baton/pull/15) ([CI 44417027700](https://github.com/AdrianMeDev/baton/actions/runs/44417027700)) passed on `main`.
- 2026-11-18: M10 auto mode delivered: `baton auto` loops milestone → plan → steps (`next --worktree --push`) → PR → merge → fast-forward base, waits for the earliest cooldown reset, resumes from plan + roadmap + git + `auto.json`, stops on escalation exhausted, merge refused, roadmap problem, budget or `baton stop`, and notifies via notify-send or ntfy. New exit codes 3 (route exhausted) and 4 (roadmap problem) for `next`/`plan`. A two-milestone roadmap runs end to end offline with fake agents, a stateful fake `gh` and a bare origin (`tests/test_auto_acceptance.py`). CI result pending the push.
- 2026-11-18: M9 live acceptance complete in AdrianMeDev/baton, targeting `feat/milestone-pull-requests` (main unchanged). `baton next --commit` used Claude Haiku implementation and isolated Codex review; both final fixtures passed local verification and review without findings.
  - Green: [PR #13](https://github.com/AdrianMeDev/baton/pull/13), reviewed head `3d064d96c04d0868095b6bbedf7c14a7100592d0`, merged by `baton pr --merge` as `b4c6096ebaf59d3c013aaadf9a6923eb56d82e0d` after [PR CI](https://github.com/AdrianMeDev/baton/actions/runs/44408538232) and [push CI](https://github.com/AdrianMeDev/baton/actions/runs/44408523588) passed on Python 3.12 and 3.14. Publish-only created the PR; repeat merge invocation reported Already merged.
  - Red: [PR #12](https://github.com/AdrianMeDev/baton/pull/12), head `cbef71e38493807991515a224031e3eba2f8c349`, remains OPEN. The [intentional failure job](https://github.com/AdrianMeDev/baton/actions/runs/44408006014/job/135168304828) failed; `baton pr --merge` exited 1 with the PR URL and CI failed. Repeat invocation reused #12 and again stopped. This fixture must not merge.
  - Live testing found that isolated Codex review requires `--skip-git-repo-check`; fixed the adapter and CLI assertions. Superseded [PR #11](https://github.com/AdrianMeDev/baton/pull/11) stopped on the stale assertion and was closed. Final local validation: 632 tests, Ruff lint/format, build and doctor passed.

- 2026-11-18: M9 offline acceptance passed: publish-only, green merge, red CI stop and retries via public CLI; 632 tests, Ruff checks, build and doctor passed on Python 3.12. Live GitHub acceptance and Python 3.14 CI remain outstanding.
- 2026-11-18: roadmap created
- 2026-11-18: Auto-Modus ergänzt (M7–M10), Statistik/Doku nach hinten verschoben (M11–M12)
- 2026-11-18: M0 done (walking skeleton: --version, config, doctor, CI, Zed tasks, README)
- 2026-11-18: M1 done (baton probe, runner with PTY mode, recorded fixtures for claude/codex/agy/pi, docs/adapters.md, agy needs no PTY)
- 2026-11-18: M2 done (plan and roadmap parser/writer, commit gate, baton status)
- 2026-11-18: M3 done (baton next: prompt, claude adapter, verify, `[~]` marking, report; HEAD unchanged)
- 2026-11-18: M4 done (S/M/L routing, agent and tier overrides, verified file edits with all four adapters). GitHub Actions passed on `aa6525c`. See [adapter smoke results](adapters.md#m4-smoke-results) for the outstanding Ollama and OpenRouter checks.

- 2026-11-18: M5 implementation delivered: quota-only handoff with partial edits, persistent cooldowns, local reset times in status, manual cooldown and parsed one-shot recheck. Offline fake-executable recovery and all real M1 recordings are covered by tests. Local verification: 390 tests pass on Python 3.12 and 3.14; lint, formatting, build and doctor pass (gh is not installed). M5 remains [~]: no real quota recording exists; acceptance requires a redacted capture or explicit waiver, plus green CI for the final changes. Scheduled rechecks remain in M10.
- 2026-11-18: M5 accepted: the user explicitly waived the real quota recording requirement. No real quota capture exists; acceptance relies on offline recovery coverage (133 adapter, limit and next tests passed) and previously recorded green CI for `6b5adcc` (run `44377237276`). See [acceptance notes](adapters.md#offline-recovery-evidence-and-m5-acceptance).
- 2026-11-18: M6 implementation and documentation delivered: same-tier repair then one L repair, independent provider review with severity findings before manual commit, and isolated reviewer working directories. The offline CLI quality-loop test covers partial quota handoff, escalation and exclusion of all contributing providers. Local verification: 483 tests pass on Python 3.12 and 3.14; lint, formatting, package build and doctor pass (gh is not installed). M6 remains [~] until CI is green for the final committed changes on both Python versions.
- 2026-11-18: M6 accepted: [GitHub Actions run 44385734356](https://github.com/AdrianMeDev/baton/actions/runs/44385734356) passed for `0f8ac1a` on Python 3.12 and 3.14 (lint, formatting, tests and doctor). Local package build and 483 tests on each Python version were already verified. The quality-loop milestone is complete; commits remain manual.
- 2026-11-18: M7 done (baton next --commit/--push/--worktree: milestone branch `baton/m<n>-<name>`, one commit per step with the plan message, roadmap status in the first and last commit; acceptance test builds a 3-step milestone branch where every commit passes its Verify)
- 2026-11-18: M8 done (baton plan: next open or named milestone, plan_commits route by milestone size, validation of structure, 3–8 steps, Verify and Size per step, one retry with the errors, otherwise no plan; acceptance test plans the example roadmap and feeds the result to baton next)

- 2026-11-18: M11 abgeschlossen: Nutzungsjournal, Statistik, UTC-Tagesbudgets und gewichtete Verteilung.
