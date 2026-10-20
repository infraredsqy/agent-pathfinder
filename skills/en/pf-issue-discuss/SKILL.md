---
name: pf-issue-discuss
description: The pool's human face: record one entry, look at the state, review which entries really need the user's call, then settle the external ones batch by batch.
disable-model-invocation: true
---

# pf-issue-discuss

> Who runs me: the user (`/pf-issue-discuss`)
> What I am for: reading, writing and ruling on the pool — take one entry in, look at the state, rule autofix/external, and put what needs the call in front of the user
> Do I dispatch subagents: no. To repair, go to `/pf-issue-repair`; to scan, go to `/pf-issue-scan`

Tools and conventions are in [`../shared/pf/pool.md`](../shared/pf/pool.md): commands go through
`pf issue` only, and run `--help` before touching anything.

## Read the intent at the door

| The user says | What to do |
|---|---|
| "the highlight covers the expand arrow on hover" | **Record**: make a kebab-case id, `add --severity P3 --decision unknown --source manual --title "<his own words>"`, and answer "recorded: `<id>`" |
| "how much is in the pool right now" | **Look at the state**: `status` + `top --limit 10`, reporting the total and the grades by severity and by source |
| "that one is fixed" | **Settle it**: locate with `search` → `resolve <id>`; when you are not sure which one it is, lay them out for the user to pick |
| "walk me through the external ones" | **Rule**: go through the two steps below |
| "fix this bug" | A single debug goes to `/trace`; a batch of same-domain debt goes to `/pf-issue-repair` |

When recording, do not cross-examine: one sentence is enough to record it. When the user says more
about the file or the module, fill in `--module` / `--files` / `--description` in passing.

## Ruling

### 1. Review first; put no fake external up

Go through the entries with `decision=external` one at a time: when the suggestion is already
clear enough that no trade-off is needed →
`update <id> --decision autofix --reason "no discussion needed: <the one-line fix>"`. What is left
over is what really needs the user's call.

### 2. Discuss batch by batch, at most 4 decisions at a time

More burns the judgement, so what is over goes into the next batch.

- Putting it to the user means **plain words**: what this trouble is, why it is dangerous, which
  roads there are, and what each of them costs. Function names / variable names / line numbers /
  call chains stay with the entry itself — that is the machine language for whoever does the work.
  The two languages live side by side and do not stand in for each other.
- Once the user has settled it, settle each entry:
  - **A simple problem** (extracting a single function, replacing a constant, about 5 changed
    lines) → `update <id> --decision autofix --status open --reason "<the settled call>"`
  - **A complex problem** (spanning several files, needing a new interface designed) → first write
    the design into `.pf/issues/specs/<id>.md`, then
    `update <id> --decision autofix --status open --plan-path <path> --reason "<the settled call>"`
  - Not fixing it → `reject <id> --reason "<why>"`; putting it off →
    `acknowledge <id> --reason "<waiting for what>"` (a deferred entry still counts toward the
    weight, see pool.md)
- **`--reason` carries what the user settled itself** ("report errors uniformly"), not "discussed"
  — the one who does the work can read that sentence only.
- Close each batch with one line: how many externals are left.

## Boundaries

| Situation | What to do |
|---|---|
| Talking a single entry's implementation through at length | suggest moving to `/engineering` |
| A large-scale refactor signal | an entry of its own (threshold and conventions in [`../shared/pf/pool.md`](../shared/pf/pool.md)), and suggest moving to `/engineering` |
| A batch to repair | `/pf-issue-repair` |
| The pool is due for a refresh | `/pf-issue-scan` |
| A new feature that should enter the graph | `/pf-route` |

## Anti-patterns

Before starting, read [`./anti-patterns.md`](./anti-patterns.md) and check yourself against it.
