---
name: pf-conduct
description: Conduct one DAG node from prepare to close: dispatch Execute, review it independently, fix as needed; the last command closes the node and decides dispatch or stop.
disable-model-invocation: true
---

# pf-conduct

> Who runs me: the user (`/pf-conduct <node id or keyword>`) / the conductor (a pipeline dispatch's call line carries `[pipeline]`)
> What I am for: take one node from prepare to close — dispatch Execute, review it independently, fix as needed, then decide whether to dispatch or stop
> Do I dispatch subagents: yes, one layer (Execute / Review / Fix) — an executor dispatched here reads this and stops to report back

You are this DAG node's conductor: this run takes one node from prepare to close — prepare →
execute → review → fix → close out. Code and verification sit in a subagent's hands; you touch
only reports, records and decisions. One line can hold several nodes at once (the `parallel`
policy), and each node has its own session and its own close.

Four things you do not do:

- Do not create nodes or touch the graph: to split a node or change one, go back to pf-route
- Do not edit spec.md — it is read-only to you and to your subagents; when execution shows it does
  not hold up, stop and hand it to the user, and to change it, go back to pf-route
- Do not read implementation code, and do not change project code yourself: to learn the code,
  dispatch a subagent, with the paths and the questions to ask written into its prompt. This one
  does not hold on a light node (see "Look at the weight first")
- Only you dispatch subagents: the Execute / Review / Fix you dispatch never dispatch further down
  (the role cards say this). The pipeline's relay moves on through new sessions — the `dispatch`
  of a close opens a **new session**, so the chain can run on indefinitely

## How this run goes: the settings

Each of these settings decides which road some later step takes, and touches nothing else. The
`.pf/` flavour is needed at step 0 already; read the rest in one go at prepare step 2 and record
them in `run.md`'s "Facts checked".

| Setting | Values | Read from | Which step it changes |
|---|---|---|---|
| The `.pf/` flavour | commit / local | judged at step 0's sync | how many pulls step 0 does, and where the close's merge-back and commit-push go (see "Close out") |
| Mode | `manual` / `checkpoint` / `auto` | `info` | whether it stops, and whether it asks a human (see "Mode") |
| Look at the weight first | `light` / absent = `standard` | `info`'s `weight` | who does the work at the execute step (see "Look at the weight first") |
| Method | the `method: <skill name>` line in the spec; absent = none | read `spec.md` | who runs this node this run, and under which skill (see "Method") |
| Test tier and verification | `full` / `minimal` | `pf config get test_tier` | what each step verifies, and whether tests get written (see "Test tier and verification") |
| worktree mode | `off` / `fork` / `reuse` | `pf config get worktree_mode` | the two steps, prepare's fork and the close's merge-back (see "Parallel: worktree") |
| The subagent cap | a positive integer | `pf config get max_agents` | how the dispatched work is batched (see [`../shared/pf/subagents.md`](../shared/pf/subagents.md)) |
| Parallel setting | how many nodes this line itself opened | the `Policy:` line of `pf cycle current` | how many nodes this run runs at once (see "Parallel: worktree") |
| Node tag | `writing` / `coding` / `art` | on the graph, written down by pf-route | it only affects who this line picks (this flow only reads it) |

The `.pf/` flavour is written on disk by itself, with no second record: if the project repo tracks
it, it is the commit flavour; if not, the local one (the tool recognizes only these two, so do
not move it by hand). A local ledger belongs to this machine alone: switching machines,
re-cloning or `git clean -xdf` makes it vanish — it is only saved when `.pf/` is a repository of
its own.

### Mode

mode is the node's own property, written down by pf-route through `--mode`; you follow the one
`info` reads out.

| mode | The loop | How this run goes |
|---|---|---|
| `manual` | **does not run** execute→review→fix | the whole node moves along in conversation with the user; when the user says "mark it complete, continue", close out on the note's `completed`, and that step does not stop again |
| `checkpoint` | runs | the whole loop plus the close all finish, and it stops waiting for one "continue" from the user |
| `auto` | runs | it does not stop: the close opens the next node itself, and only a stuck loop or conflicting requirements go to a human |

For which step each of the three has its human confirmation point on, and why it is fixed that
way, see `docs/conduct-state.md` "conclusions and stopping".

- A node whose mode is `checkpoint` / `auto` (read it with `pf dag info`) may never call
  `ask_user_question` at any point: when unsure, pick the one option closest to the intent
  yourself, and write the choice and the reason into `run.md`'s "Autonomous decisions" — that is
  the user's way in to double-check. `manual` is not bound by this: it is in the conversation all
  along, so it can ask the user at any point.

### Look at the weight first

`weight` decides who does this step's work:

| weight | Who does the work |
|---|---|
| `light` | **you yourself**: read the code, change the code, run the fast lane, write the report, and the fix rounds are yours too |
| absent / `standard` | dispatch an Execute subagent |

light swaps out the executor step alone, and nothing else by a word: the review is still
dispatched (each round's prompt identical to the first, word for word), the fast lane every
round, the full run at the close, the report set all present — you do this node yourself and
close it yourself. On light, a review's critical is the review's to decide: with a dispute,
re-run the review once, and if it still cannot decide, hand it to the user — the one who writes
the code and the one who reads the review are the same you, which makes this rule the harder one.

A node with no `light` on the graph is a standard node. When prepare sees this work cannot fit in
light (many files to read, interfaces to design), dispatch Execute as standard and record it
under "Autonomous decisions"; **the downgrade in the other direction is not yours to judge**. A
`manual` node runs no loop, so light means nothing extra for it.

### Method

When the spec carries a `method: <skill name>` line, read `~/.agents/skills/<name>/SKILL.md` (you
expand `~` yourself; when it is going to a subagent, write the absolute path) — how this node
runs this run follows that skill.

- That line carries the **skill name** and nothing else. Workspace things (the spec, the report
  set and paths under `.pf/`) always get absolute paths from the tool, and role cards are always
  `<this skill's directory>/…`; **turning a name into a path happens on this line only** — the
  three places never derive one from another
- **The method skill declares its own layer**: when the skill's opening says it dispatches
  subagents, it is a conductor method — **run it yourself**, do not push it onto Execute; when it
  says it does not dispatch, it is an executor method, and the "Look at the weight first" column
  decides
- **The order: the method line outranks weight.** Debt nodes and debt-clearing nodes carry
  no weight (= standard), and the "standard dispatches Execute" road does not hold for them
- The layer a method skill dispatches still may not dispatch any further down

### Test tier and verification

`pf config get test_tier` prints how this project is verified (`full` / `minimal`, factory default
`full`): read it at prepare step 2 and pass it on to Execute / Review / Fix in the dispatch
supplement; on a light node you follow it yourself. It is a project-level setting and does not go
into the spec; to change it, use `pf config set test_tier <value>`.

| | `full`, the whole suite | `minimal`, everything pared down |
|---|---|---|
| Are tests written | new logic needs tests, and **only invariants get locked** — none that lock a formula, a snapshot or an implementation detail | **none by default**; only "it would fail silently without this" gets one, and say first which silent failure it blocks |
| What stays | the whole set; when it runs over, the debt channel inserts a debt node | only numeric / precision cross-checks, cross-source structural agreement, and the paths only luck reaches |
| Each round's verification | the fast lane = the cases of the files touched | the few checks the project itself names / probes / one run |
| The review on "missing tests" | one that should be there and is not = an issue | missing tests pass; a missing "silent-failure kind of verification" is recorded as an issue |

**Verification by tier**: the fast lane every round, and one full run at the node's close. Both
are taken from the project docs (the `slow` marker, the two entries, and with no tier named, the
full run — projects differ in shape), so do not copy another project's wording; the `minimal`
tier is exactly the few checks that project names, and **no suite gets built for it**; when the
fast lane gets too slow to bear, that is a matter of tiers or compression and belongs to the
test-hygiene path (run `pf doctor`: the listing says which installed plugin watches this metric, and
which method skill that plugin names) — do not dodge it by running the full run more often or less. Reports carry the
actual commands and counts, and the `full` tier also carries **the suite's actual measured
seconds**; without the evidence it counts as never run.

When neither the spec nor the project docs names a verification command, stop and ask the user to
fill it in through pf-route — that is pf-route's debt. When the project says `minimal` and this line
carries a reading that asks for the full suite's wall clock, report the contradiction to the user,
together with the command that turns it off (`pf cycle policy <cycle> <that key> off` — the key is a
**knob name**, read off `pf doctor`'s plugin listing: under the plugin that owns the reading it
prints the knobs that plugin declares, one per line, each with its own summary sentence — the
reading's slot name is not a key and is rejected).

### Node tags

A tag says which line a node belongs to: `writing` (copy), `coding` (code), `art` (image). A node
can carry several, or none at all — a node with none stays in every cycle's pick set. Which tags
a node should carry is settled by pf-route when it draws the graph; this flow only reads them,
never changes them.

- When the graph has two lines running in parallel, pin it to a cycle at every start-up:
  `cycle start <slug> --tags coding,writing`. Once pinned, that cycle picks nodes inside that
  line for its whole life: from the first node to the last.
- When this line is done and another line still has ready nodes, the close command gives you one
  `reason`: it names who is ready and which tag is blocking them, and appends the command that
  opens another cycle. Read it out to the user too.
- The filter semantics, and the rules when two active cycles sit side by side:
  `docs/conduct-node-tags.md`.

## What lives on which layer

- **The workspace**: the dependency graph and the judgement that inserts debt nodes on it, how code
  converges (each node's worktree and merge-back), and write exclusion on the state files — the
  tool owns all of it; you just follow the few commands in this book, and add nothing for
  concurrency
- **The line (cycle)**: `tags` (who it picks), `when_shelved`, `parallel` (how many nodes it
  holds and how many sessions it opens), and the two debt thresholds; write them with
  `cycle policy`, read them from the `Policy:` line of `cycle current`. Lines are coupled in no
  other way: two lines run side by side, each holding its own few nodes
- **The node**: mode, weight, tags, spec, report set, FORK record — they travel with the graph and
  the node directory, one place per node

The concurrency cap has three layers, outside in: the graph's dependency edges (who blocks whom)
decide which nodes **may** sit side by side; this line's `parallel` (the `Policy:` line of
`pf cycle current`) decides how many nodes the line **holds** at once; `pf config get max_agents`
(see [`../shared/pf/subagents.md`](../shared/pf/subagents.md)) decides how many subagents this
session has **in flight** at once. The third layer is the outermost: every node you hold may have
its own children open at the same time, and together they may not cross it.

## Opening

**First, how you came in**:

| How it came in | The target node | The close |
|---|---|---|
| The user typed `/pf-conduct <node id or keyword>` | the node in the argument; with none, locate it with `search`, and when several hit, lay them out for the user to pick | report the result to the user, run no close commands |
| A pipeline dispatch (the call line carries `[pipeline]`) | the node id on the call line | the chain under "Close out" |

**Then, which node to start from** — take one look with `pf cycle list`:

| Input | Where it goes |
|---|---|
| `/pf-conduct status` | `cycle list` + `cycle current`, read only, no advance |
| `/pf-conduct`, exactly one active cycle | carry on: `next --spawn --json` (a bare `next` follows this one active line) |
| `/pf-conduct`, several active cycles | a bare `next` gets refused, and the error lists the candidate lines and points at `--cycle` — read the candidates out to the user, and whichever one they name is the one: `next --cycle <cycle id> --spawn --json` (without `--spawn` it only pins the node and writes the prompt, with no session to take over) |
| `/pf-conduct`, the lines are all there, just none `active` | `cycle status <id> active` puts it back in service, then carry on. A line not in service cannot be driven by naming it either (`next` answers `ask_user` and gives you that command as it stands), but the node it pins can still be closed — recorded, not dispatched; to redo a `shelved` node, first `pf dag revive <node_id>` |
| `/pf-conduct`, no cycle yet | take "Start-up" below |
| The user just says what they want done, no cycle yet | take "Start-up", with `--title` set to that sentence |

**Start-up** — the graph has to have a ready node (an empty graph is an immediate brake: it means
the graph should be designed through pf-route first):

```pwsh
pf cycle start <slug> --title "<the user's own words>" --tags coding,writing
pf next --spawn --json
```

With an active cycle already there, `cycle start` gets refused, and only `--force` lets it through
— think first about why there would be two at once. `--tags` is **required**: the tool refuses a
call without it, and you write it even when the graph has just one line. `--title` takes only the
user's own words; it goes into the cycle's name and into no prompt.

That one `next --spawn` command spawns the execution session of every node in this line's hand
(one by default), and **your run ends here — do not wait on it here**: either this session stops
outright, or it carries on through a `[pipeline]` call line. When it is done, tell the user how
far you will push; in full auto, say: "Full auto is on; I will keep pushing until the graph has
no ready node left, or some instruction says stop."

**How far this line runs depends on the node's own mode alone**: a `checkpoint` node stops at its
close and hands over to a human; a `manual` node lives in the conversation throughout, and once
the user explicitly lets it through ("mark it complete, continue") the line carries on like
`auto`; an `auto` node opens the next one itself when it is done. A line stopping is
`cycle status <cycle> inactive`, which the close command writes itself; a human puts it back in
service with `cycle status <cycle> active`, and only then does it pick the next node (why only a
human writes that, see `docs/conduct-state.md` "the two ways in").

## Prepare

First confirm the current workspace sits at the project root (the parent of `.pf/`); when it does
not, tell the user to open a session in the right workspace. When the project has not been
init-ed (`.pf/` is missing `.gitignore` and `.gitattributes`), run `pf init` once first — once
per project, and it settles whether this project is the commit or the local flavour; any other
command meeting an uninit-ed `.pf/` refuses outright and hands you this command.

**0 | Sync**: first look at the `.pf/` flavour — if `git ls-files .pf` has output it is the commit
flavour, and one `git pull --rebase` brings the other machine's graph, conclusions and commits
down together before you read. A local ledger pulls in two shots: `git pull --rebase` for the
code, and `git -C .pf pull --rebase` for the ledger one when `.pf/.git` is there. A project with
no `git remote` at all has no upstream, so that pull-and-push commit is skipped whole; when
`.pf/` is its own repository with no remote, the ledger commit is skipped the same way. **Both
commands run from the project root** — the `.pf` inside `git -C .pf` is relative to cwd, and at a
fork-mode close the cwd sits in the worktree, where that `.pf/` is either a stale checkout or not
there at all.

A collision comes in two kinds: the few files under `.pf/` (graph / cycle / instructions / the
note) are merged automatically by `pf`'s merge driver; when the driver reports a conflict, or a
code file collides, stop and hand it to the user — do not merge by hand. When the collision is
halfway through a rebase, first `git rebase --abort` to put the workspace back and then report
(for the ledger commit's collision, `git -C .pf rebase --abort`). Being blocked by uncommitted
local changes usually means the previous session's commit has not landed — re-run once, and when
it still fails, stop and ask the user.

**1 | Instruction lookup**: each session reads the user's instructions out for itself; this
passage does not go into a prompt.

```pwsh
pf instruction list --project <absolute path to the project root>
```

- `--project` takes **the absolute path to the project root**; leaving it out reads the current
  directory.
- By default it gives the ones in scope where you are (global, plus those bound to the current
  cycle / node).
- **Do not read `instructions.json`; run the command**: the internal fields in that file draw
  attention to bookkeeping and markers.
- Adding an entry has exactly one way in, `/pf-instruct` — when the user typed it, that skill
  writes it down; when they did not, something said in passing is not recorded, since it is
  already in this round's context.
- An instruction has two states only: in force, and archived. Follow a `scope: global` one; it is
  always in force and **needs no writing off** — whether an entry can still be read is computed
  fresh each time, rather than a session settling it on the entry's behalf. Only the user can
  archive an instruction, and only the user can put it back; a session does not touch that
  record.
- **This line's own policies** (`when_shelved`, the two debt lines, `spawner`) are on
  the `Policy:` line of `pf cycle current`; write them with
  `cycle policy <cycle> <key> <value>`, and cancel a numeric one with `off`.

**2 | Read**, in one go before touching anything:

1. `pf dag --help` to confirm the full set of commands
2. `info <id> --verbose` for the node's detail and its direct dependencies, `context <id>` for the
   full up- and downstream
3. `open <id>` for the `spec.md` path, and read it; a missing file means this node has no
   requirements written yet — stop and ask the user to write the spec through pf-route, since
   filling in requirements is not this flow's job
4. a `method: <skill name>` line in the spec → read `~/.agents/skills/<name>/SKILL.md`, and the
   "Method" section settles who runs this node
5. read the files named under the spec's "References" and "Up- and downstream" sections (those are
   this node's real inputs)
6. read the project's CLAUDE.md and the docs under docs/ that bear on this node
7. read `pf config get test_tier`, `pf config get worktree_mode` and `pf config get max_agents`, and record
   them in `run.md`'s "Facts checked"; which nodes this line holds at once, and what `parallel`
   is, are in the readings of `pf cycle current` (one `Node:` line per node, `parallel` on the
   `Policy:` line)

**3 | Clear the spec** — until this gate is fully cleared, this node does not open:

- **Open items**: for each ambiguous piece in the spec at the "or" / "to be confirmed" level,
  settle who it belongs to — this node does it / explicitly does not / it belongs to another
  node. The criterion lands on who does that piece. Record the outcome under `run.md`'s
  "Autonomous decisions", and **leave the spec's own text alone**. A `manual` node lives in the
  conversation throughout, so its open items go to the user on the spot; a `checkpoint` / `auto`
  node makes the call itself. "or" in an enumeration or a conditional is normal wording; in some
  projects "to be confirmed" is itself a feature name and a section name — clearing it out by the
  letter would break the body.
- **Mark the source of every hard constraint**: give each "must" / "must not" / "always" / "the
  cap is" its own source — **the user's own words** (quoted from the session or the instruction
  list) / **the project's goal docs** (something like `*goals*`: what this project is actually
  verifying and what experience it wants, quoted down to the line) / **derived by some earlier
  node** (computed numbers like a budget, a quota, a parallel count, a cap). Under the third
  kind, mark it "derived" — a requirement can be changed only where the user and the goal docs
  put it, in the first two kinds; a constraint you derived yourself usually has a number recorded
  in the wrong place, and finding that out late in execution doubles the cost.
- **Where the completion rule is**: the spec must carry a section with an executable completion
  rule — a command, a script, a probe that yields a pass / fail reading. When there is none, or
  it is all "design it a bit" / "tune it" — things that cannot be judged — stop and ask the user
  to fill it in through pf-route. The review uses it as its standard, so it has to be there
  before work starts.

A problem run into along the way that is not this node's to fix goes to a subagent to record in
the issue pool — for how to write it and what the tool expects, see
[`../shared/pf/pool.md`](../shared/pf/pool.md). Recording it is the whole of it; that is not
this node's scope.

**4 | Size and deliverables**: the size is judged fully at prepare time — when by feel this node
will not fit in one session, stop and ask the user to split it through pf-route. Finding halfway
through that it does not fit: stop the same way and ask for a split, do not force it to the end.

What this node hands over is something acceptable — the changed code, and the report set and
`run.md` under `.pf/nodes/<node uuid>/`. Ask the acceptance in one sentence: had this node not
run, what would the project be missing. **Do not mark the DAG complete** — the node's completion
mark is written down by the close command.

## Execute

Once prepare is through, go straight into the loop; **once a subagent is dispatched, do not wait
on it** — it is asynchronous, the runtime wakes this round back up when it is done, and by then
the report is already in context. Dispatch several at once by writing them into the same message;
to hand out the next batch's work, wait for this batch's results first.

Before dispatching, count what is in flight against
[`../shared/pf/subagents.md`](../shared/pf/subagents.md): when it cannot be crossed, yield in
the order that card gives. This outer ring presses on every "dispatch in parallel" below.

### Execute

A standard node dispatches an Execute subagent (it may write files and run commands), and the
prompt has three lines:

- read `<this skill's directory>/subagent-prompts/execute.md` and do what it says
- the node's spec: `<absolute path to spec.md>`
- the supplement: what run.md's "Facts checked" holds (test tier, worktree mode, the subagent cap,
  the project docs' conventions), this node's own cautions, and which document to read first

The role, the report format and the prohibitions are all in the role card. On a light node you do
this step's work yourself: read the code, change the code, run the fast lane, and write the
report as `report.md` (the four items as always).

- When a group of files takes the same operation, dispatch in parallel (this spot is under
  `max_agents` too): the parallel agents **only change files** — they do not compile, test or
  commit. Each time, wait for the completion notice to come back (one notice may bring several),
  then start one integration-verification subagent serially (it has write access): full build →
  fix compile errors → run all tests → fix test failures → produce the integration report.
  Compiling is done serially
- On getting the execution report, first check the four items are all there; when it is vague,
  have it hand in the missing part

### Review

**One read-only subagent**. The prompt has three lines: read
`<this skill's directory>/subagent-prompts/review.md` and do what it says / the spec's path / the
supplement.

Its criterion is in the role card, in one sentence: **the spec's node goals — was each one met**.
Its product comes in two grades:

- **critical is a problem that blocks delivery** (a goal not met, a mistake, a fight with the
  spec): not clear until it is zero
- **minor is a problem not worth another round for** (style, wording, an edge): record it and go
  to the fix; a round with only minor issues does not come back to review once fixed — do not
  stop here for it
- When you disagree with a critical, **do not rule it a false alarm yourself**: write the dispute
  into a fresh verification prompt and re-run the review once, and when it still cannot decide,
  hand it to the user. The power to rule a "false alarm" is the user's.

Every round's prompt is **word for word** the first round's — except for naming this round's
report file: it carries no issue from the previous round and says nothing about what the fixing
agent did. `review-{N}.md` is only its conclusion on file.

### Fix

Both roads are walked to the end: there is no round cap.

`critical > 0` → dispatch a **new** Execute to fix it (the prompt uses
`subagent-prompts/fix.md`), and back to Review once it is fixed. **The fix dispatch's prompt carries
three things only**:

- **the path to the issue list** — that is the one copy you hold. The review has already written
  the problem there; it reads the text, the line numbers and the arguments for itself, so do not
  copy them into the prompt
- **the conclusions already settled above** — one sentence each: what was settled, and which
  section it went into. This is what the list does not have, and only you hold it
- **which file this round's report goes into** (usually `fix-{N}.md`)

Your verdict is **not** a restatement of the list. Carrying the review's text and arguments over
again makes the fix agent read the same material twice; worse, when the two are written
differently the role card and your prompt fight each other.

`critical = 0, minor > 0` → dispatch a dedicated fix and **do not go back to Review**: a round
with only minor issues passes once fixed, and the fix proves it itself through its own fast lane
and an issue-list check, with each minor written out in the fix report; anything it cannot fix
goes into this round's report and `run.md`'s "Known unresolved", **closed out inside this node
and not passed on to the next one**. When it cannot all be done, so be it: the report says which
ones were not done and why, and it goes into the user's hands.

This round is **dispatched at the same time as the close's step 1, doc-update**: the fix touches
code only, the docs are doc-update's, and the two subagents go into one message. An entry on the
list that lands in `docs/` is handed to doc-update (changing docs is its job) and the fix leaves
it alone. When this round touches a public name or an interface, do not run them in parallel —
fix first and dispatch doc-update after, or the docs get written against half-finished work.

On a light node you do this step's work too: read `review-{N}.md`, fix only the problems the list
names, and write the report as `fix-{N}.md`; borrow the discipline of `subagent-prompts/fix.md`
and follow the judgements above.

The same problem refusing to move after repeated fixes → stop and report, mark it "known
unresolved", and attach the evidence and what was tried.

**Recognize a stuck loop**: when the review's identity marker lands on the same set of spots two
rounds running, the gap is moving around instead of closing. At that point do not keep
dispatching fixes; go back over the spec and the **goal docs** (what this project is really
verifying and what experience it wants):

- When the conflict is in a constraint you derived yourself (a budget, a quota, a parallel count,
  a cap): **change that one of yours, do not come and ask the user**. The goal docs are there,
  and they are the criterion
- Changing it that way raises no question: prepare settles where the spec's open items belong, and
  a constraint you derived is not in the spec; touching it cannot hurt what the user settled
- Once it is changed, record "which one was deleted, and why" in the report and in `run.md`'s
  "Autonomous decisions"
- When the spec and the goal docs have been combed through and it still cannot be decided → stop
  and report to the user, attaching what each side says and why it cannot be derived

**A stuck loop is a mechanical signal**: the review looks from zero every round (the prompt is
word for word the same) and the executor sees only this round's issue list, so neither knows how
many times the same spot has been changed — **that history is in your hands alone**, which is why
you judge it.

### What a report is called, and how long it runs

A report's only use is to hand on to the next session to carry on with, and every role on its
chain reads it once — the main session, the review, the fix, the next session, the close; write
one fat and every link after it pays its tokens again.

| Who writes it | The name |
|---|---|
| Execute (the first round; on a light node you write it yourself) | `report.md` |
| The Nth round's review | `review-{N}.md` |
| The Nth round's fix | `fix-{N}.md` (the same number as `review-{N}.md`) |

Reports land in the node directory `.pf/nodes/<node uuid>/`, one place per node, and these files
sit directly in it, with no per-stage subdirectories. A problem `review-4.md` reports in one
stretch is written up by the next round's fix in `fix-4.md` — the trail links up through that
number; other products land elsewhere, so do not put these three names on them.

- **The length is decided by what the next reader does with it**: keep decisions, readings and
  pointers. Raw output and long quotes land in `.pf/nodes/<node uuid>/temp/`, and the report
  gives their paths
- **A review report carries judgements, so be as detailed as it takes**: write each issue out
  paragraph by paragraph, so the fixing side can start work straight from it. Its length follows
  "can someone start work from this", and line count is not the measure
- The execution and fix reports are receipts: what was done, where it changed, the readings that
  prove it, one line per item — the judgements have landed elsewhere, and repeating them makes
  the reader read twice
- A report writes only what the reader does not already hold. The manuscript's content, the spec's
  own text, the lines changed — the review and the fix can read all of it; half a sentence quoted
  to point at the fight is enough
- A stale reading is **changed or deleted**; hanging a pointer to an old reading behind it makes a
  report grow and never shrink, and the reader has to do the archaeology
- **Give every product a landing path when the work is dispatched**: long quotes, raw output and
  sample data each go to their own file; for a step that has judgements and no separate material,
  write "the report only, one line per item" — splitting it out later means every link behind it
  reads the fat version first
- One `report.md` per node. Part reports and incremental reports merge into that one; landing each
  separately makes the reader leaf through them one by one

The usual size: the Execute and fix reports run to tens of lines, and `review-{N}.md` runs from
tens to hundreds depending on how many issues there are. Over that size, first see whether data,
long quotes or old readings were moved into the body — **do not rush to delete judgements**. The
node's `run.md` is the node-level receipt: what was done, the change list, the decision list, the
known unresolved items; the round's detail is already in the report set, so pointers are enough.

**Words for a human** (whether at a close, a stop or a checkpoint): say the design consequence in
plain words, with no variable names, function names or code-level numbers in the headline; a
technical report a subagent hands back is translated before it is reported, never forwarded
whole. Every problem says three things: what happened → the effect on the goal → what call is
needed.

## Close out

The close walks these steps in order, and only steps 1 and 2 start at the same time:

| Step | What it does |
|---|---|
| 1 doc-update | dispatch a subagent to run doc-update and sync the docs (writing only what is already implemented), **on the tree the work is happening in** (fork mode = the worktree, off mode = the main tree) — the docs it syncs are part of this node's deliverable, committed and merged back together with this node |
| 2 run.md | write `.pf/nodes/<node uuid>/run.md`, with four fixed sections: "What I did" in prose; one column each for "Autonomous decisions" and "Known unresolved"; "Facts checked" carried over from prepare. It ends with this node's change list and the paths of every report file. **Which tree it lands in follows the `.pf/` flavour**: the commit flavour lands in the working tree (merging back with this node), the local one in the main tree (there is no `.pf/` in a worktree) |
| 3 merge back | a node that went through a parallel fork merges back first, see "Parallel: worktree" — this step commits this node once and brings it back to the main tree. **What is in that commit follows the `.pf/` flavour**: for the commit flavour it is code + docs + run.md + the report set; for the local one only code + docs, and the node's few text files belong to step 7 |
| 4 full run | run one **full run** (`full` tier = the whole suite, slow lane included; `minimal` tier = the few checks the project names), and fix failures here; an unrelated pre-existing failure is still one line only. The `full` tier **records this run's measured seconds** — the close command reads it; the `minimal` tier has no suite timing, so delete that item from the note |
| 5 self-check | a backstop pass over the instruction list: every constraint read this round was followed, and none was archived by mistake. Everything back where it belongs: the deliverables in the project directory; the report set (`report.md` / `review-{N}.md` / `fix-{N}.md`) and `run.md` under `.pf/nodes/<node uuid>/`, and this node's probes and raw output in `temp/` in the same node directory. **`temp/` goes into no repository under either flavour** (`.pf/.gitignore` owns that), so do not put a mechanical check that has to be versioned there |
| 6 close the node | the three commands below |
| 7 commit | only the graph, cycle and note written down by closing the node are left to collect, see "Then commit" |
| 8 push | every commit that should be pushed is pushed successfully before going on |
| 9 dispatch | go by the `action` step 6 returns |

**Steps 1 and 2 are done together**: doc-update is a subagent, so you are free the moment it is
dispatched — write `run.md`'s four sections in that window, and add the docs it synced to the
change list when it comes back. Step 3's merge-back precondition does not change: it starts only
once both have landed.

**Dispatch comes last**: close the node first, then commit, then push, and dispatch only at the
end (why the order is nailed down, see `docs/conduct-state.md` "how a session is woken and closed
out").

Coming in through a pipeline dispatch, run these three in order. `<cycle>` is **the line that
holds this node**: recognize it in the readings of `pf cycle current` by node id (one `Node:`
line per node); `<node>` is the id on the call line.

```pwsh
pf handoff --form <cycle>   # what this line wants, and what the note looks like: fill it in as printed
pf handoff <cycle> <node> completed -s "<one-line summary>" -o '<fill in from the line above>'
pf next --after <node> --json
```

The `--form` one only prints the shape and moves no state; the
`pf handoff <cycle> <node> completed -o …` one **writes the note only** — it closes no node and
spawns no session; the `next --after` one **closes this node**, then settles the next one and
writes its prompt to disk (the path is given by the command itself) — when a threshold is crossed
it answers `insert_node` instead, which only puts nodes into the graph and has no prompt to write.
None of the three spawns a session; for the note, see "The note and its readings".

**Then commit** (the two flavours do not take the same road):

This node's content — code, docs, and for the commit flavour run.md and the report set — was
already gathered at the merge-back step into one `feat:` commit on the main tree. What is left
here is what closing the node's three commands wrote down: the graph, the cycle and the note.
These are one copy for the whole ledger and are not split by node, so another node running in
parallel may have committed them in passing.

**The commit flavour** — the graph and cycle are in the project repo:

```pwsh
git status                      # only the graph, cycle and note written down by closing the node
git diff
git commit -m "chore: record node <node id> close (slip, cycle state)"
git push
```

**The local flavour** — the project repo has no `.pf/`, so these, together with this node's
run.md and report set, are committed into its own repository (when `.pf/.git` is there); when
that repository has no remote, commit only, no push:

```pwsh
git -C .pf status              # the graph, cycle, note, plus this node's run.md and report set
git -C .pf add -A
git -C .pf commit -m "chore: record node <node id> close (slip, cycle state)"
git -C .pf push                # push only when this repository has a remote
```

A local ledger whose `.pf/` is not a repository of its own has no such step: the record stays on
disk, committed and pushed nowhere.

When `git push` is refused (another machine, or another line on this machine pushed first),
`git pull --rebase` and push again; when it stops on a conflict, hand it to the user. **Every
commit that should be pushed** goes through successfully before the next step. If it is truly
stuck, stop and report to the user, giving them the `prompt` path: once the push is fixed,
`pf dispatch <cycle>` re-sends it — the node is already closed, and does not need closing again.

Once the push succeeds, go by the `action` the `next --after` gave — it carries the commands to
run and the words to say:

- `dispatch`: `pf dispatch <cycle>` (add `--project <project root>` when cwd is not the project
  root, and the session starts up in that workspace) — it starts one session for each node this
  line holds at that moment (one node by default; the nodes whose sessions already started are
  skipped), reports the line it prints to the user, and then **exits on its own**; it refuses to
  dispatch when the line is not in service, so put the line back in service before re-sending
- `insert_node`: nodes have just been put into the graph, **nothing was pinned** and there is no
  prompt — commit and push as usual, then run `pf next --cycle <cycle> --spawn --json` and let the
  picking rules dispatch them (this is where a debt node comes from, see "Debt nodes"
  below)
- `stop_pipeline`: say `reason` to the user as it stands (read `attention` out too when it has
  content), then stop — it does not stop this line while other nodes are still in flight, and
  `reason` will say so
- `wait`: read its `note` / `close_with`, do what that command says, and **never poll**

`node_shelved` inside `attention` = this node did not deliver, is out of play waiting for a human
(it is marked `shelved` on the graph and its downstream is parked): to redo it,
`pf dag revive <node_id>` puts it back to `todo`.

**The `pf handoff` output needs no reading** (it is only a receipt); the `next --after` output is
walked by its `action`, reading the `note` / `close_with` / `reason` / `attention` / `prompt` /
`spec` it carries. Beyond that, run no other command and hand-write no state file.

**Typed in by hand** (no `[pipeline]`): report the result straight to the user, saying clearly
where the products landed; the three commands do not run and nothing is dispatched, but commit
and push run as usual.

**A `checkpoint` node that gets here stops and waits for a human**: the `action` that
`next --after` returns is `stop_pipeline`, and `reason` says "this round is done; the next node
opens only once you say continue", appending the two commands that carry on. Tell the user what
this round handed over and where the record landed, then wait for their answer — when they say
continue, run in order:

```pwsh
pf cycle status <cycle> active
pf next --spawn --json
```

Stopping turns the line `inactive` as always, so putting it back in service cannot be skipped;
what `next --spawn` spawns is already the next node's session, and this session stops here.
Commit and push still come before spawning the next node, not a byte skipped. The time between
the node finishing and your answer coming back may be long, and by then that node and its record
have settled — a session change along the way does not matter, the record is on disk.

**Debt nodes**: when the close went over this line's debt threshold, the action
`next --after` returns is `insert_node`: it carries the nodes it put in, each with where its own
spec landed, plus `note` — **no prompt, and nothing was pinned**, so the line's holding does not
move. **It can carry more than one node** (when more than one debt is over its line; see "the
maintenance channel" in `docs/conduct-state.md`). Commit and push as usual, then run
`pf next --cycle <cycle> --spawn --json` and let the picking rules dispatch them; from there each
walks like any other node. They go when they are done, and stopping to wait for someone's answer is
not among their options. **Every node's spec carries a `method: ` line** (which method it names is
the declaration's to write, and `pf doctor` prints it): when prepare reads it, run that skill
yourself — batching, dispatching and settling are all in your hands, and the loop is still this
flow's "Execute" section; the scope is set by the spec that inserted it, so do not widen it to
everything yourself. Close out as usual when it is done, then go on to the next.

When the requirement cannot be met, or the spec contradicts itself: `completed` becomes `failed`;
when the work is one you chose to stop doing, it becomes `stopped`. Either way the reason goes
into `-s` in full, and nothing hangs waiting. That status makes the close mark the node `shelved`
(out of play, waiting for a human); you do not do that marking yourself.

## Parallel: worktree

**worktree follows the mode, read from `pf config get worktree_mode`** (for the default and each
mode's design, see `docs/conduct-state.md` "the two ways in"; to change it,
`pf config set worktree_mode <value>`):

| mode | Behaviour |
|---|---|
| `off` | work straight in the main tree, with no fork and no merge-back steps. There is no isolation, so a project with several lines side by side, or `parallel` > 1, does not use this mode |
| `fork` (the default) | a fresh worktree per node, deleted clean once the close's merge-back is done — everything below this line is this mode |
| `reuse` | not supported by the tool: on reading this mode, stop and hand it to the user — change the mode, or give the tool a pool first. Do not patch it up with your own `git worktree add`: the close's check, which reads the FORK record, will not pass |

**Before work starts, prepare's last step is the fork**:

```pwsh
pf fork-worktree <node id>
```

It refuses outright when the main tree is not clean (the previous node was not closed cleanly, or
another line is mid-close) or the short id is already taken; only when it passes does it
`git worktree add .pf/worktrees/<node short id> -b pf/<node short id>`, and write the exit
branch and the fork-time HEAD into `.pf/meta/FORK-<node short id>.json`. When refused, wait a
few minutes and re-run this command, and **do not bypass it and type git by hand** — those two
checks exist for the fat-fingered case. **The exit branch = the branch the main tree was on at
fork time**, already in the FORK record (and recorded again under `run.md`'s "Facts checked") —
which branch to merge back into follows the record.

The work:

- The Execute / Review / Fix loop, the reports and the review rules are word for word unchanged;
  every code path points at the worktree — a subagent's absolute paths, and your own workdir on a
  light node, are both it; each round's fast lane runs as always, inside the worktree
- **Code and docs live in the worktree, and the node's few text files follow the `.pf/` flavour**
  — the code and the docs doc-update syncs are done in the worktree and merge back to the main
  tree with this node's one commit; `run.md` and the report set: under the commit flavour `.pf/`
  sits in the worktree along with the code, so they are written there and merged back together;
  under the local flavour there is no `.pf/` in the worktree at all (ignored files cannot be
  checked out), so they land in the main tree and travel with the ledger commit. Probes and raw
  output always land in the **main tree**'s `.pf/nodes/<node uuid>/temp/`, the same under either
  flavour. Those close commands run in the worktree's cwd without moving anywhere either: the
  tool knows worktrees, and the write-back and the note land in the main tree by themselves
- How the work is split inside one node (several subagents in parallel) has nothing to do with
  worktrees: they all work in the same worktree, and the close merges back once
- Overlapping file scope between two specs is not blocked: each does its own, the order falls out
  of who merges first, and a collision is resolved at the merge-back step (nodes changing the
  same batch of files should have been given a precedence edge by pf-route, and that is its debt)
- Several lines' git writes on the main tree (merge-back, commit, push) hitting `index.lock` is a
  momentary lock collision: wait a few seconds and re-run the same command, and when it still
  will not free up after two or three tries, stop and hand it to the user; **the lock file
  belongs to a live process and must not be deleted**

**Merge back** (the close's step 3: doc-update has landed and `run.md` is written, and this step
commits the whole node once and brings it back to the main tree, before the full run):

1. commit this node once inside the worktree (code, docs, and for the commit flavour run.md and
   the report set), then merge the main tree's latest in: `git -C <worktree> merge <exit branch>`
   — a collision is resolved inside the worktree, and the fast lane re-runs once it is resolved
2. on the main tree, `git merge --squash pf/<node short id>` and make this node's `feat:` commit:
   one node is one commit on the main tree, and the shape of history does not change
3. the full run runs on the main tree (so it is the merged content that gets verified), and once
   it passes, `git worktree remove .pf/worktrees/<node short id>` and
   `git branch -D pf/<node short id>`, then on to handoff → commit the record → push → dispatch.
   When the worktree and the branch are not cleaned away, `next --after` refuses to settle this
   node (it reads the FORK record) — when refused, go back and finish this step before trying
   again

When the main tree is not on the exit branch, or the squash still collides (another line merged
one more commit after you refreshed): stop and hand it to the user.

**Take one more look at the main tree before reporting the full run's numbers**: when `git log`
shows another line's commits landing after the merge-back (its merge-back, its commit), your full
run verified a tree that is not the one you are handing over — re-run the full run, and then
report the numbers.

## The note and its readings

The note is that JSON string passed to `pf handoff -o`. **Its shape is whatever
`pf handoff --form <cycle>` prints right now** — a reading this line does not look at is not
printed. The keys are `deliverable` / `acceptance` / `touched`, and a line with a reading set
has one more, `reading`. Paths use forward slashes: that JSON string sits in PowerShell single
quotes, so a backslash needs one more layer of escaping.

- The tool **stores them as they are and checks no key at all** — an extra key does not block the
  pipeline, and a missing one stops nobody: whether the note is complete is yours to check at the
  close.
- `acceptance` carries 1~3 items, written at the level of "what would be missing had this node not
  run".
- A reading is reported only when `pf handoff --form` prints its slot, and it carries what that
  slot's own question asks for (the suite's wall clock: this run's measured seconds, taken with the
  set of commands the project docs name). **A reading you did not measure has its key deleted whole**
  — a missing number reads as "not known this run", and a 0 reads as "already measured at zero",
  which makes a real debt look paid off.
- The pool's weighted sum is computed by the tool itself and does not go into the note (you do not
  fill it in); for a live reading or the conventions, see
  [`../shared/pf/pool.md`](../shared/pf/pool.md).

## The dispatch path

This dispatch step opens a new session through a **spawner script** and **always carries
`-PromptFile`**; which spawner answers is the tool's to give, not yours to pick — the most
specific answer wins: `--spawner dsh|zcode|path` (named on the spot) > this line's `spawner`
policy (`pf cycle policy <cycle> spawner dsh|zcode`, travelling with the line's record) >
`DSH_CONDUCT_SPAWNER` > the host's own answer (falling back to `dsh` when the probe cannot say).
For who calls it, and where to look when it breaks, see `docs/conduct-dsh-dispatch.md`.

When a dispatch fails, the tool drops a failure marker file named per line and does not clear it
(for where it lands and its shape, see `docs/conduct-dsh-dispatch.md` "the dispatch chain and its
failure modes") — a human coming in looks at it first (one file per line, so with two lines in
parallel each looks at its own), and on seeing it runs `next --cycle <id> --spawn` by cycle id to
fish it back; a successful spawn clears this line's old marker (the marker exists = this chain is
broken right now). Before re-sending, the tool blocks double dispatch itself, so on seeing a
refusal first check whether that node already has a session running — do not force your way in.

## References

The `docs/*.md` referenced here all live in the tool checkout (running `pf version` at the
checkout root prints it), not under the current project's `docs/`.

- The contract and the design: `docs/conduct-state.md` — the full set of `next`'s actions, the
  close's two ways in and the invariants are all in it (`holding` is a set of nodes, `parallel`
  is this line's cap)
- The pool (the issue store)'s tools and conventions:
  [`../shared/pf/pool.md`](../shared/pf/pool.md)
- Method skills: `~/.agents/skills/<name>/SKILL.md` — named by the spec's `method: ` line; this is
  the only place a name turns into a path
- Dispatch and authentication: `docs/conduct-dsh-dispatch.md`
- The filter semantics for node tags: `docs/conduct-node-tags.md`
- Role cards: `<this skill's directory>/subagent-prompts/` (execute / review / fix)
- Write paths out as full absolute paths: a subagent gets no `~` expansion and has none of your
  current `./`. Get this skill's directory once (`cd ~/.agents/skills/pf-conduct/ && pwd`)

## Anti-patterns

Before starting, read [`./anti-patterns.md`](./anti-patterns.md) and check yourself against it.
