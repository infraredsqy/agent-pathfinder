# agent-pathfinder — pf: tools and skills for an agent execution pipeline

> This file (AGENTS.md / CLAUDE.md) must not exceed 100 lines.

Read before working: [`tools/conduct/AGENTS.md`](tools/conduct/AGENTS.md) — the conduct tool's
development discipline (the dispatch path is live ammunition).

- docs/ — the contracts' one source: state and advancing, dispatch and authentication, node-tag
  filtering (read before changing conduct's behaviour)
- skills/ — one skill tree per language, `skills/<lang>/`: the `pf-*` skills plus one copy of the
  `shared/pf/` common cards each; `install.ps1 -Lang` picks a tree and links it into
  `~/.agents/skills` by junction. A card bound to a feature is not here: it lives in that feature's
  own folder (`tools/conduct/features/<name>/cards/<lang>/<method>/`) and is linked too, so it is
  installed and removed with the feature.
  Common cards are named after themselves (`subagents.md`, `pool.md`, `toolcraft.md`, …) —
  `AGENTS.md` / `CLAUDE.md` are host-reserved names, case-insensitive: whichever directory one
  falls into, the host injects it as instructions into every session that starts work there
- tools/ — the `pf` command and its implementation: the `bin/` entry, the `conduct/` pipeline, the
  `dag/` graph, the `issue/` pool, `shared/` for the pieces used across tools (libraries only, no
  entries)
- install.ps1 — install and self-check: link the skills, put `pf` on PATH; idempotent, re-running
  repairs it
- .gitignore — the blocklist for workspace litter

## Documentation discipline

- Docs state what is, and their wording follows the code and the tests (where a mode's human
  confirmation point sits, what a call line looks like, which kinds of `ask_user` there are —
  `tools/conduct`'s behaviour is the authority). Change the behaviour, change the docs in the same
  commit.
- No history, no "this used to be called X"; counts, timings and other readings are run and filled
  in on the spot, never copied into prose.
- **Docs do not copy code**: field names, commands and flags, constants, file lists, word lists
  live in exactly one place, the code (or `--help`); a doc carries the design intent, the criteria
  and the "why it is set this way". The same thing is not stated twice in one doc either.
- A call line a doc or skill quotes is taken from the tool's own mouth — `--help` or the code — at
  writing time, never from memory; a line the tool would reject does not go in.
- References name the file and the section, never a section number or a line number — and not one
  code comment points at a doc.
- **A skill's docs and its anti-patterns connect through one door only**: the docs under `skills/`
  (`SKILL.md` and the role cards) must not cite any anti-pattern entry. A criterion they need
  belongs in their own file: an entry being pointed at either moves into that doc and out of the
  anti-patterns, or the pointer goes. The anti-patterns are found only through the one sentence
  "check yourself against this before starting", which sits in the Anti-patterns section at the end
  of the doc that carries them — written once, in one place.
- **One criterion, one home**: the anti-patterns keep the symptom, the instance and the correction
  signal; "why it is dangerous" is criteria text and stays out the same way. Criteria live in the
  skill's docs — wherever a criterion is already written, the anti-patterns do not write it a
  second time. When two skills share one criterion, it lives in one of them and the other points at
  it without copying the text.
- **An anti-pattern entry carries a stable id**: `<skill dir name minus the pf- prefix>-<english
  slug>`; the subagent branch is bound by role, the executor's role and the fixer's role sharing
  one book, named `conduct-execute-…` (Execute / Fix) and `conduct-review-…`. The id opens the
  heading line; adding or removing entries never renumbers and never reuses an id; entries
  reference each other by id only.
