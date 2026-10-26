# pf-issue-scan anti-patterns

Check yourself against this before starting. The criteria themselves live in
[`./SKILL.md`](./SKILL.md), [`./scan.md`](./scan.md) and
[`../shared/pf/pool.md`](../shared/pf/pool.md); this keeps symptoms and correction signals only.

## `issue-scan-full-corpus-in-one-agent` — covering the whole scan with one subagent

**Symptom**: one subagent scans every module and comes back with a pile of repeated code +
responsibilities crossing their lines + structural problems all mixed together.

**Correction signal**: the number of surfaces dispatched against the number of modules — one agent
carried several copies.

## `issue-scan-explores-without-reading-docs` — exploring without reading the project docs first

**Symptom**: the scan is dispatched without reading CLAUDE.md / docs.

**Correction signal**: when cutting the surfaces and writing the prompt, you cannot say how this
project's modules are divided.

## `issue-scan-finding-not-locatable` — a pooled finding with no way to locate it

**Symptom**: "there is repeated code in game_ui.js" — no function name, and no meaning.

**Correction signal**: taking that finding back to look for it lands on no function and no stretch
of meaning.

## `issue-scan-rescans-without-pool-dedup` — rescanning without dedup against the pool

**Symptom**: every full scan run adds another copy to the pool, where one has long been sitting.

**Correction signal**: several of the new entries share a site and a symptom with an old entry in
the pool.

## `issue-scan-absence-read-as-resolved` — absence read as the entry being gone

**Symptom**: entries that are not on the liveness list are resolved in bulk; or the scan does not
find the code for some entry and rules it already fixed (when really the code just moved); or the
check is run right after a scan has pooled something, and a new entry is cleared by mistake for
"not being on the list".

**Correction signal**: over one check, one of the settled entries went through no entry-by-entry
confirmation.

## `issue-scan-refactor-as-small-item` — a structural refactor logged as a small item

**Symptom**: a module's structure turns out to be terrible, and it is written as one small
`autofix` entry and dropped into the pool.

**Correction signal**: this piece of debt's file surface spreads over three or more modules, while
the entry's size reads like a passing change.

## `issue-scan-decides-for-the-user` — the scan stage ruling decision for the user

**Symptom**: entries that are not clear are all marked "the user has to settle this", and twenty
externals come back.

**Correction signal**: the entry's `description` / `suggestion` says "needs the call", and cannot
say which lesson is missing.
