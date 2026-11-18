# Plan: Mixed statuses
Created: 2026-11-18 · Branch: feat/mixed · Base: abc1234
Milestone: M3 – Mixed (ROADMAP.md, local)

## Steps

### 1. [x] – abc1234 feat(a): first
- **Why:** w1
- **Verify:** `true`
- **Size:** S

### 2. [-] chore(b): skipped
- **Why:** not needed
- **Verify:** `true`

### 3. [~] (on def5678) feat(c): pending
- **Why:** w3
- **Changes:** `a.py`,
  `b.py`
- **Done when:** done
- **Verify:** `pytest`
- **Size:** L
- **Unknown:** ignored

### 4. [ ] feat(d): open
- **Why:** w4
- **Verify:** `pytest`
- **Size:** M

## Unknown section
whatever

## Log
- 2026-11-18: step 1 committed
- skipped step 2
