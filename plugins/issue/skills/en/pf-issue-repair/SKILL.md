---
name: pf-issue-repair
description: Repair the issue pool's debt back under the line: batch by change domain, Fix→Review per batch, commit once a batch passes; how an entry is disposed of follows "whose board is still open" only.
disable-model-invocation: true
---

# pf-issue-repair

> Plugin I bind: `issue` — this skill is installed and removed with that plugin; the plugin not installed, this skill is not here
> Who runs me: the conductor — a pipeline node's conductor, or the user's own session
> What I am for: repair the pool's repairable entries back under the line (one metric / one batch, with the scope set by whatever called you in)
> Do I dispatch subagents: yes, one layer (Fix / Review) and no more. An executor dispatched here reads this, stops and reports back — it does not work from it

There is one place to draw material from: the pool. Tools and conventions are in
[`../shared/pf/pool.md`](../shared/pf/pool.md). **The scope is not yours to set** — the pipeline
node's spec, the few lines of "why it was inserted", or the user's sentence: whoever called you in
sets it, and you do not widen it to everything yourself. Once the criterion is passed, stop.

## How one entry is disposed of

There is one criterion only: **is this piece of debt's board still open**. Do not pick the
disposal by "which layer it reached", "how big it is" or "which line it belongs to" — none of
those three is the criterion.

| The board | What the entry looks like | The disposal |
|---|---|---|
| **Already called** | `reason` / `suggestion` write the road and the rule out in full, or a `plan_path` hangs off it; `decision=autofix` is what that says | **do it** (through the "Run" section), then `resolve` |
| **Still open** | nobody has made the call and only the user can pick; or doing it by the solution shows the solution does not hold | `update --decision external`, with the two roads and their costs in the reason |
| **Not debt** | looking into it shows it does not stand | `reject`, with why it is not fixed in the reason |

There are only three ways out: putting an entry off (`acknowledge`) belongs to the pool's human
face (`pf-issue-discuss`), and is not done here.

Two hard rules:

- **An entry that has been given a solution may not turn `external`** — when the solution still
  holds, do it; what this run truly cannot schedule stays untouched (its weight still counts, and
  it is named in the report).
- **Report the readings as they are.** Not at the line means reporting that it is not at the line
  and naming what stands in the way; running inside the pipeline with the reading not at the line
  means writing `failed` and putting the number read into `-s`, not `completed`.

## Drawing the material

1. First read **the standard**: what the spec's "completion rule" says — one metric's reading
   (`pf issue weight -v`), or "this batch of entries all reaching a terminal status"
2. Take out the entries this round will touch: **actionable** (the three grades' conventions are
   in pool.md; adding only `--status open` misses the deferred ones) **and `decision=autofix`**,
   ordered by severity (to narrow it, add `--severity` / `--module`)
3. Settle what has no disposal yet: a `decision` still `unknown` gets its disposal by the table
   above — what can be fixed is re-ruled `autofix` and scheduled into this round; what needs a
   trade-off is re-ruled `external`. `decision=external` is not fixed
4. What `reason` / `suggestion` already spell out is done as written, not discussed again; when a
   `plan_path` hangs off it, read the plan first

## Run

The loop and the conductor's few things-not-to-do go through `/pf-conduct`, and are not copied a
second time here. This flow adds three things only:

- **Batch by change domain, with the batch width under the in-flight cap**: entries of the same
  module form one batch; a change family that truly has to cross modules is declared explicitly as
  a whole batch. The same verification covers the changes inside a batch. Each batch's Fix plus
  Review is 2 in flight, so before dispatching several batches together, count once against
  [`../shared/pf/subagents.md`](../shared/pf/subagents.md)
- **Fix → Review per batch**: Fix uses `~/.agents/skills/pf-conduct/subagent-prompts/fix.md`, and
  Review the `review.md` in the same directory (read-only); the prompt follows pf-conduct's "Fix"
  section
- **Commit once a batch passes**: only when the whole batch reviews with no issue is it committed.
  Running outside the pipeline there is no node-close step, so the commit lands here

`critical > 0` → dispatch a new round of Fix, and back to Review once it is fixed; a `minor` that
is clearly harmless → recorded as a new entry for a later same-domain batch, and the rest is fixed
as usual.

New debt run into while working is recorded into the pool as always (`add`, conventions in
[`../shared/pf/pool.md`](../shared/pf/pool.md)), and then **its disposal is settled on the spot by
the table above**: what the same batch and the same domain can swallow is swallowed, and what
cannot is settled as it really is. A freshly recorded entry counts into the readings as it really
is — "it was just found" does not cancel that number.

## Settling

Settle one entry at a time, leaving nothing half open (which disposal each takes is in the table
above):

```pwsh
pf issue resolve <id> --reason "<where it was fixed, the verification reading>"
pf issue update  <id> --decision external --reason "<the two roads and their costs>"
pf issue reject  <id> --reason "<why it is not fixed>"
```

**What only the user can call, you may not call for a single one of them**: write each into the
report's "left for a human" section, with the two roads and their costs.

## The report

- **The batch composition**: which entries each batch holds, which change domain, which commit
- **The settling table**: how many resolve / external / reject, one line per entry
- **The standard's reading**: where the number the spec wants stands now (the measured value of
  `weight -v`; not at the line means saying so as it is, and naming what stands in the way)
- **Left for a human**: one by one, where the trade-off is and what each of the two roads costs
- **Known unresolved**: the entries this run did not get done, one line each (which one, why it
  was not done, who can carry on with it in the next round); write "none" when there are none

## Anti-patterns

Before starting, read [`./anti-patterns.md`](./anti-patterns.md) and check yourself against it.
