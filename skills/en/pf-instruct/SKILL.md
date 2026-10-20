---
name: pf-instruct
description: Record one constraint in the project instruction list, so that agents working later follow it (used inside a pf session).
disable-model-invocation: true
---

# pf-instruct

> Who runs me: the user, only inside a pf-route / pf-conduct session — those are the only ones that read this list
> What I am for: record one sentence of the user's own words into the project instruction list
> Do I dispatch subagents: no

Typed anywhere else, it is typed in the wrong place: say so, and record nothing in the list.

Copy that sentence from the call line into the list **word for word**; `--scope` is required, and
it decides who can see the entry:

```pwsh
# global: every session that reads the list from now on sees it, and it stays in force
pf instruction add --text "<the user's own words>" --scope global

# cycle: the sessions in this cycle see it, and it is gone when the cycle ends
pf instruction add --text "<the user's own words>" --scope cycle --cycle <id>

# node: it governs only the node it was recorded on, and it is gone when that node closes
pf instruction add --text "<the user's own words>" --scope node --node <id>
```

- With two lines side by side, `--cycle <id>` / `--node <id>` has to be named; when it cannot
  bind, the tool refuses the call
- A working run reads the list once when it starts: what you record midway is unseen by the run
  already in flight, and takes effect from the next one
- Words about "how far it runs, when it stops" (stop after three) → `--stop-after-nodes 3` plus
  `--target boundary`: the stop logic reads only the entries in the boundary column, and without
  it the tool will not stop
- Once it is recorded, read the entry's id and scope back to the user
