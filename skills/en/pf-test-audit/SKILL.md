---
name: pf-test-audit
description: Audit the test code and give two conclusions at once — which tests verify nothing real (delete them) and which slow the fast lane (move them out or split them) — then act by the action table and prove it yourself.
disable-model-invocation: true
---

# pf-test-audit

> Who runs me: the user (`/pf-test-audit`, type it and it runs itself to the end) / the conductor (named by a node spec's `method: ` line — that is how the maintenance channel's node comes about)
> What I am for: take the test suite once — delete what verifies nothing real, move out or split what slows the fast lane, and build a tier yourself when the split is missing
> Do I dispatch subagents: yes, one layer (scan / removal) — an executor dispatched here reads this and stops to report back

> ⛔ **Hard constraints (must hold)**
> 1. **Read the docs first**: the project instruction files (AGENTS.md / CLAUDE.md, whichever
>    is there) and docs/, to find the test structure and the fast / slow lane conventions
> 2. **The conductor does not read code**: the scan has to go to a subagent, and the conductor
>    reads docs and structured results only
> 3. **Make the call yourself**: the verdict is yours — produce no to-be-confirmed list and
>    stop for no questions; when the user gives other instructions on the call, narrow the scope
>    by what he says
> 4. **Verify after changing anything**: compile + run the remaining tests (which tier to run
>    and what counts as passing follow pf-conduct's "Test tier and verification")
> 5. Breaking any one of the above = the task failed.

## The role

You are **the test audit's conductor**: dispatch the scan, round up, rule, do the work, prove it
yourself. Read no code — that keeps the context clean and the judgement accurate. To sound out the
structure of `tests/`, dispatch a subagent.

## Two criteria, one action table

The two criteria read the same file and come out in one scan — splitting them wastes a pass.

- **Quality**: does it verify anything real. The criterion is in `./garbage-patterns.md` (6
  garbage patterns, the safety exceptions).
- **Compression**: does it take up the fast lane's road. The criterion is in
  `./compression-criteria.md` (what counts as slowing it down, how a splittable one is split, the
  two ways to move a test).

The action follows from the two together, and **deletion shows up in the "worthless" column
only**:

| | Worth having | Worthless |
|---|---|---|
| **Slows the fast lane** | move it out of the fast lane; a splittable one gets its fast assertions split into the fast lane | delete |
| **Does not slow the fast lane** | keep it | delete |

- **Deleting has to pass the coverage gate first**: it hits a garbage pattern **and** no other
  case verifies that piece of business — both have to hold. Deleting just because it is slow is
  deleting coverage.
- **The verdict is yours**: all three kinds of action are called by you.

## The two ways in

Both ways in run themselves to the end and wait for no confirmation.

| Who is running | Where the scope comes from |
|---|---|
| The user typing `/pf-test-audit` | the sentence he puts after it; with none, the whole test suite |
| A pipeline node (the spec's `method: ` line names me) | the scope the spec sets, not widened to everything yourself |

The `mode` of the node road (whether it stops, whether it asks a human) is the node's own
property, and pf-conduct owns it.

## The flow

### Step 1: understand the test structure

Read the project instruction files and docs/, and get: how `tests/` is organised, the test
framework (Catch2 / Jest / pytest / node:test), the build system, the verification commands, and
**how the fast and slow lanes each run** (the `slow` marker, the two entries, the parallel
switch). When the docs are not enough, dispatch a subagent to sound it out.

### Step 2: the scan

The number of scan subagents follows the number of files: 1 covering everything by default, split
only when there are so many files that one list cannot be scanned by one. **The cap is not here**
— before dispatching, count what is in flight against
[`../shared/pf/subagents.md`](../shared/pf/subagents.md) (`pf config max_agents`), and when it
cannot be crossed, merge or batch by that card.

Every scan subagent's prompt has to carry:

- the **absolute paths** of `./garbage-patterns.md` and `./compression-criteria.md`, for it to
  read itself
- the generic examples translated into the corresponding syntax for the project's framework
- the list of files to scan
- the return format of the two criteria (see the end of the criteria files)

Start every subagent in parallel. When they finish, the runtime wakes you; wait for the notice, do
not poll.

### Step 3: round up

Group by file, give each entry two conclusions (quality / compression) and an action, and settle
it by the action table: the `entire` / `partial` ones pass the coverage gate, and the `move` /
`split` ones follow their criteria. The report lists by `entire` / `partial` / `move` / `split`,
with the action and the reason written out one by one.

### Step 4: Execute

Dispatch a removal subagent (1 by default, split only when the change surface is especially
large):

- deletion: the named TEST_CASEs or assertion lines; a file emptied out gets dropped from the
  build system
- moving a test: move it out of the fast lane the way the criteria file gives; when the project
  has no fast / slow split and it cannot be pressed down, split the heavy few into a slow lane of
  your own (see `./compression-criteria.md`)
- splitting: pull the fast assertions out into the fast lane, and the rest stays in the slow lane

**Verification is required**: it compiles + the remaining tests pass (or at least no new
failures). Commands and tier follow pf-conduct's "Test tier and verification", taken from the
project docs rather than copied from another project. **The measured time has to be reported**:
the fast lane's measured seconds, which are the whole acceptance of the compression half; the node
road also reports the full run's, since the maintenance channel's node reads exactly that for its
completion rule. No reading means it was not done.

### Step 5: close out

The report: how many were deleted (N whole cases / M partial spots), how many were moved, how many
were split, how many empty files were cleared away, the compile and test results, the fast lane's
measured seconds, and the unhandled items (including what was kept back by the coverage gate).

Commit the changes yourself: commit only the files this run touched, and do not touch anyone
else's uncommitted changes in the workspace. The node road: the close is pf-conduct's — closing
the node, committing, pushing and dispatching are all its to land, and here you only hand the
report back.

---

## Boundaries

| Situation | How it is handled |
|---|---|
| The call said "look only, do not fix", or some other limit | narrow the scope by what he says, which may mean only the scan and the report |
| Verification fails | report the failure in detail and stop — do not guess, do not roll back, do not force it |
| A large-scale test refactor turns up | suggest moving to `/engineering` |
| The project has no tests | report "no test files found" and exit |

**Products go into the report only**: the scan's conclusions enter no repository and land on no
disk; to keep raw output, put it where pf's conventions say (in a node,
`.pf/nodes/<node uuid>/temp/`; in a project, `.tmp/`).

## Anti-patterns

Before starting, read [`./anti-patterns.md`](./anti-patterns.md) and check yourself against it.
