# Roadmap: baton – Plan-Schritte auf mehrere KI-Agenten verteilen
Updated: 2026-11-18

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

### M5 [~] Limit-Erkennung und Wechsel
- **Goal:** Am Limit übernimmt automatisch der nächste Agent.
- **Includes:** Limit-Muster, Cooldowns mit Reset-Zeit, Wechsel mit aktuellem Diff, `baton cooldown`, Verfügbarkeit in `status`, Wiedererkennung nach manuellem Kontowechsel bei jedem Adapter: `baton recheck <agent>` startet eine winzige Probeanfrage und hebt den Cooldown bei Erfolg auf, im Auto-Modus optional mit Hinweis "agy im Cooldown, anmelden mit anderem Konto oder warten bis HH:MM" und alle paar Minuten erneuter Probe; kein Anmelden, keine Token-Dateien anfassen
- **Done when:** simuliertes Limit löst Wechsel aus; echte Meldungen aus M1 werden erkannt; Fake-Adapter: erst Limit-Fehler, nach `recheck` Erfolg → Cooldown aufgehoben
- **Depends on:** M4
- **Size:** M, ~5 commits

### M6 [ ] Qualitätsschleife
- **Goal:** Fehlschläge selbst reparieren, schwierige Schritte eskalieren, unabhängig reviewen.
- **Includes:** Fix-Versuch mit Fehlerausgabe, Eskalation zur höheren Stufe (nach 2 Fehlschlägen auf L: Opus/Astra), Review durch anderen Anbieter (Schweregrade), Befunde im Bericht
- **Done when:** Fake-Adapter-Tests zeigen Fix → Eskalation; Review nie beim selben Anbieter
- **Depends on:** M5
- **Size:** M, ~5 commits

### M7 [ ] Auto-Commit und Branches
- **Goal:** baton committet grüne Schritte selbst auf einem Meilenstein-Branch.
- **Includes:** `baton next --commit`, Branch `baton/m<n>-<name>`, Commit mit Plan-Message, Roadmap-Status im ersten/letzten Commit, Push, eigener Worktree
- **Done when:** ein Meilenstein im Beispiel-Repo endet als Branch mit einem Commit pro Schritt, alle Commits grün
- **Depends on:** M6
- **Size:** M, ~5 commits

### M8 [ ] Headless planen
- **Goal:** `baton plan` macht aus dem nächsten offenen Meilenstein eine gültige `.agent-plan.md`.
- **Includes:** Planungs-Prompt nach den Regeln von `plan-commits` (inkl. Stufe je Schritt), Rolle `plan`, Validierung (Struktur, 3–8 Schritte, Verify je Schritt), bei ungültigem Plan ein zweiter Versuch, dann Stopp
- **Done when:** für die Beispiel-Roadmap entstehen gültige Pläne; ungültige werden abgewiesen
- **Depends on:** M4, M2
- **Size:** M, ~5 commits

### M9 [ ] Pull Requests und Merge
- **Goal:** Jeder fertige Meilenstein wird ein PR und je nach Regel gemergt.
- **Includes:** `gh pr create` mit Beschreibung (Ziel, Schritte, Prüfungen, Review-Befunde), auf CI warten, Auto-Merge bei grün und ohne kritische Befunde, sonst offen lassen und stoppen
- **Done when:** im Test-Repo entsteht ein PR, der bei grüner CI gemergt wird und bei roter offen bleibt
- **Depends on:** M7
- **Size:** M, ~5 commits

### M10 [ ] Auto-Modus
- **Goal:** `baton auto` arbeitet die ganze Roadmap ohne Zutun ab.
- **Includes:** Schleife Meilenstein → Plan → Schritte → PR → Merge → nächster; Warten bis zum frühesten Limit-Reset, wenn alle Agenten pausieren; Fortsetzen nach Absturz aus Dateien + Git; Stopp-Regeln (Eskalation erschöpft, Merge abgelehnt, Roadmap-Problem gemeldet, Budget erreicht); `baton stop`; Benachrichtigung bei Stopp/Meilenstein (`notify-send` oder ntfy)
- **Done when:** eine Beispiel-Roadmap mit 2 Meilensteinen läuft mit Fake-Adaptern vollständig durch; jede Stopp-Regel hat einen Test
- **Depends on:** M8, M9
- **Size:** L, ~7 commits

### M11 [ ] Auswertung und Verteilung
- **Goal:** Sehen, wer wie viel arbeitet, und Kontingente vorausschauend verteilen.
- **Includes:** `runs.jsonl`, `baton stats` (Läufe, Dauer, Tokens je Agent), Tages-Budgets und Gewichte im Router
- **Done when:** `stats` zeigt die Verteilung; überschrittenes Budget lenkt um
- **Depends on:** M5
- **Size:** M, ~4 commits

### M12 [ ] Integration und Doku
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
- 2026-11-18: roadmap created
- 2026-11-18: Auto-Modus ergänzt (M7–M10), Statistik/Doku nach hinten verschoben (M11–M12)
- 2026-11-18: M0 done (walking skeleton: --version, config, doctor, CI, Zed tasks, README)
- 2026-11-18: M1 done (baton probe, runner with PTY mode, recorded fixtures for claude/codex/agy/pi, docs/adapters.md, agy needs no PTY)
- 2026-11-18: M2 done (plan and roadmap parser/writer, commit gate, baton status)
- 2026-11-18: M3 done (baton next: prompt, claude adapter, verify, `[~]` marking, report; HEAD unchanged)
- 2026-11-18: M4 done (S/M/L routing, agent and tier overrides, verified file edits with all four adapters). GitHub Actions passed on `aa6525c`. See [adapter smoke results](adapters.md#m4-smoke-results) for the outstanding Ollama and OpenRouter checks.
