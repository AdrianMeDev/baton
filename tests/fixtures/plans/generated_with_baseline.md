# Plan: Widget support
Created: 2026-11-18 · Branch: feat/widgets · Base: abc1234
Milestone: M2 – Widgets (docs/ROADMAP.md, tracked)
Baseline: `uv run pytest -q && uv run ruff check .`

## Goal
Widgets work.

## Assumptions
- Something.

## Steps

### 1. [ ] feat(widget): add widget model
- **Why:** needed first.
- **Changes:** `src/widget.py`; `docs/ROADMAP.md` – M2 `[~]`
- **Done when:** model exists
- **Verify:** `pytest -q`
- **Size:** S

### 2. [ ] feat(widget): render widgets
- **Why:** users see them.
- **Changes:** `src/render.py`
- **Done when:** rendering works
- **Verify:** `pytest -q`
- **Size:** M

### 3. [ ] docs(widget): document widgets
- **Why:** users find them.
- **Changes:** `README.md`; `docs/ROADMAP.md` – M2 `[x]`
- **Done when:** README explains widgets
- **Verify:** `pytest -q`
- **Size:** S

## Log
