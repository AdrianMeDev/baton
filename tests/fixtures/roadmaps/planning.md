# Roadmap: Widgets
Updated: 2026-11-18

## Vision
Everyone uses widgets.

## Architecture
- Widgets live in `src/widget.py`.

## Decisions
- D1: widgets are immutable.

## Milestones

### M0 [x] Skeleton
- **Goal:** Runs.
- **Depends on:** –
- **Size:** S, ~3 commits

### M1 [ ] Widgets
- **Goal:** Users can create widgets.
- **Includes:** model, rendering
- **Done when:** a widget renders
- **Depends on:** M0
- **Size:** M, ~4 commits

### M2 [ ] Gadgets
- **Goal:** Gadgets exist.
- **Depends on:** M1
- **Size:** L, ~6 commits

## Definition of done (every milestone)
- tests green, lint clean

## Changelog
- 2026-11-18: roadmap created
