---
name: pf-issue-scan
description: Scan the project's code for debt that has not made it into the pool, write it into the issue pool, and check whether the entries already there are still there.
disable-model-invocation: true
---

# pf-issue-scan

> Plugin I bind: `issue` — this skill is installed and removed with that plugin; the plugin not installed, this skill is not here
> Who runs me: the user (`/pf-issue-scan`) / the conductor (named by a pipeline node spec's `method: ` line)
> What I am for: scan the code for new debt + check whether the old debt is still there, and go into the pool only
> Do I dispatch subagents: yes, one layer (scan); an executor reading this stops and reports back

The scan does two things only: find new debt and write it into the pool, and confirm whether the
old debt is still there. **It fixes nothing, discusses nothing with the user and rules no
`decision`** — a new entry always stays `unknown`, and the ruling is done one step later by
`/pf-issue-discuss` (what was ruled at the moment of the scan is stale by the time anyone really
works on it).

The pool's tools and conventions are in [`../pf-issue-repair/pool.md`](../pf-issue-repair/pool.md).

## Prepare

1. Read the project's CLAUDE.md / docs/: how the modules are divided, and what the architecture
   conventions are. This is the ground for cutting the scan surface and writing the prompt
2. Sound out the change surface: the code root, the order of magnitude of the files, which
   directories are tools / tests / assets
3. Call `status` and `list` for the list of **in-play entries**: the ones this round hands to the
   scan for a liveness check

## Dispatch the scan

Cut the surface by **module × dimension** (a subagent carrying a write tool, since findings have
to land in a file), and each surface watches one copy only. The dimensions: repeated code / a
responsibility crossing its line / a structural problem. When there are only one or two modules,
use your own judgement on whether to merge the dimensions.

Once the surfaces are cut, take them in batches by the in-flight cap: `pf config get max_agents` is
the cap, and **one subagent can carry several surfaces** — for how to merge them and when to
batch, see [`../shared/pf/subagents.md`](../shared/pf/subagents.md).

Every prompt has to give:

- the role card: `<this skill's directory>/scan.md` (the absolute path) — the role, the output
  format and the prohibitions are all in it
- the project's architecture conventions (distilled from the docs) and the file list handed to it
  (as many files as it carries)
- the list of in-play entries (for the liveness check)
- the output path: `.pf/meta/scan/<module-slug>.json` (as many files as the subagent carries, one
  file each)

Only once the whole batch is back do you go on.

## Merge into the pool

1. read every `.pf/meta/scan/*.json`
2. **Dedup and merge**: when `files` overlap **and** (`functions` overlap, or the descriptions are
   close in meaning) → merge them into one entry, keep the more detailed description, and absorb
   the other's `files` / `functions`
3. `add --from-file <the merged>.json --auto-id`; for the kind that spans several files or needs a
   new component designed, also carry `--plan-path .pf/issues/specs/<id>.md`
4. **Liveness check**: round up the scans' `alive_ids` → `mark <ids>` (it only adds to check_count
   and reports candidates, and **never changes a status**). Settling an entry goes through
   `resolve <id> --reason` one entry at a time, and only when that entry has a scan's explicit
   "not locatable by the three layers" report; a freshly added entry not being on the list is
   normal
5. Clear away the temporary files under `.pf/meta/scan/`
6. Report: N new entries (graded by severity), the check's result, the candidate list

## Boundaries

- **Only the user or a conductor runs it**: this skill dispatches subagents; an executor dispatched
  to it reads it and stops to report
- It rules no `decision`, fixes no code, changes no spec and enters no graph — picking and
  ordering are the DAG's business
- On running into "a new feature that should enter the graph", record one line for the user; build
  no node yourself, that is `/pf-route`'s work
- A large-scale refactor signal gets an entry of its own, and its threshold and criterion are in
  [`../pf-issue-repair/pool.md`](../pf-issue-repair/pool.md)

## Anti-patterns

Before starting, read [`./anti-patterns.md`](./anti-patterns.md) and check yourself against it.
