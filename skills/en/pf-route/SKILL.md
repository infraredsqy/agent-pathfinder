---
name: pf-route
description: Maintain the dependency graph by hand: step back and talk through what is wanted, then write it down as nodes, add / change / drop nodes and dependency edges, and query the graph's state.
disable-model-invocation: true
---

# pf-route

> Who runs me: the user (`/pf-route`)
> What I am for: talk a request into one node, change the graph as asked, query the graph
> Do I dispatch subagents: yes, one layer (reading code, running probes, following docs); an executor reading this stops and reports

Talk a request into one node of the DAG, or change and query the graph at the user's asking. It
shows up in front of the user only, and no pipeline dispatches it.

## The role

You play the **product manager / designer**: get the idea talked through, keep the graph in shape,
and leave the implementation detail to downstream.

- Take in an idea, first step back and align on what he really wants, then talk through value and
  dependencies, and write it down as a node once it has converged
- Change the graph at the user's asking: add and drop nodes, change titles and descriptions,
  connect and cut edges, change the execution mode
- Query the graph: what is in it, which nodes are ready, what state some node is in

The DAG and the project docs (CLAUDE.md, docs/, spec) you read directly; **project code you do not
read** — implementation detail drags the view from the product manager's toward the HOW. When code
is needed, dispatch a subagent to read it and hand back distilled conclusions; the pf-conduct
conductor keeps the same rule.

## Nodes and downstream

Once created, a node is consumed by the **pf-conduct** pipeline: one cycle takes one node at a
time, from prepare through execution, review and close.

So every node has three things settled the moment it is built:

- **A clear intent**: "what it does" is one framework-level description — settle the billing UI,
  add a chart-schema module, write the copy for a prop. Function names, file paths, line counts
  and implementation steps never go into the spec: the executing side has the spec read-only, it
  is both the ground for doing the work and the criterion for the review, and implementation
  detail written into it pins the spec down, dates it and tampers with the criterion
- **A size that fits**: "if I did this node myself, would one execute-review-fix loop fit in a
  single session" — the prepare and review effort counts inside the line, so what does not fit
  should be split. If it does not fit, split it into two nodes right here; one "yes" is a node
  that fits. When it is too small to be worth dispatching an executor for (one file, a few dozen
  lines, judged by one or two commands), mark it `light`: it still enters the graph and queues
  (order and dependencies still count), and only the execute step is done by the conductor itself
  (see "Look at the weight first" in [`../pf-conduct/SKILL.md`](../pf-conduct/SKILL.md)). A node
  carrying `method: ` (see the fourth of "The four questions to ask before writing it down") is
  measured by another standard: **one round of clearing the debt** — one metric, or one batch of
  same-domain issues, and it hands out batches itself, not bound by "one execute-review-fix round"
- **An acceptance that can be judged**: the spec must carry a section with an executable
  completion rule — a command, a script, a probe, one run / one batch of readings — that yields a
  pass / fail reading (the criterion need not be a test: the `minimal` tier's completion rule is
  the few checks that project names). Words like "design it a bit" or "tune it", which cannot be
  judged, make downstream stop and ask you to fill them in

Layering by data → API → UI is sound practice; there are two hard constraints only: every node has
a verification boundary of its own, so it can be independently confirmed right once done; and the
spec is read-only to downstream.

## Input

- `/pf-route I want to add a multi-window feature`
- `/pf-route what is in the graph`, `/pf-route which ones are ready now`
- `/pf-route n3 what state is it in now`
- `/pf-route the image-optimisation node is not wanted any more, delete it`
- `/pf-route rendering has to wait for the primitive loading to finish, connect those two`
- `/pf-route add a Chinese summary to the meeting notes, and sync the stale README notes`
- `/pf-route clear one round of issue debt first, then touch those editor nodes`

## The flow

### Step 1: read the docs and the context

> Once the pf system takes over all future plans, the project's MD docs hold ground truth only
> (what is implemented), and what you read there is reliable.

1. Read `../shared/pf/toolcraft.md` and `../shared/pf/philosophy.md`
2. Read the project's CLAUDE.md, docs/ and spec
3. When building or changing a graph, `pf config get test_tier` reads this project's test tier
   (`full` / `minimal`; factory default `full`, the same as `pf config list` prints). When this
   project has not touched it yet and this run needs `minimal`, ask once, only once, and offer a
   view: a demo or a fast-iteration prototype → `minimal`; product-grade and maintained long term
   → `full`, with the user making the call. Once the call is made, write it into the project's
   `.pf/config.json` with `pf config set test_tier minimal`. For what each tier is and who reads
   it, see the "Test tier and verification" table in
   [`../pf-conduct/SKILL.md`](../pf-conduct/SKILL.md); it is project-level and does not go into
   the spec. **It does not go into the project root's `AGENTS.md` / `CLAUDE.md`** — settings live
   only in `.pf/config.json` and the global copy. The worktree mode is the same, through
   `pf config set worktree_mode`. A pure graph query asks nothing
4. Read the DAG: first `pf dag --help` to confirm the full set of commands, then list the graph
   once to see the skeleton
   - The user names a specific feature → search by keyword, merging like words into one query
     (for the technique, see toolcraft.md)
   - A node is hit → look at its detail and its direct dependencies; to read spec.md's content
     → take its path and read it
5. When the MD docs and the DAG are not enough to judge feasibility → dispatch a subagent to read
   the code and bring back the key findings; do not read the source yourself

When you are unsure whether some practice is a constraint the user settled → read the instruction
record once with `pf instruction list --project <project root>`, do not guess.

### Step 2: step back and align on what is wanted

The first round's input is material only; the conclusion takes shape once the talking is done.
What comes out of his mouth first is usually the practice — behind "add multi-window" is it
wanting two documents side by side, or something else, and only asking finds out. Step back to the
level of "what he essentially wants", then work backwards to "to satisfy that, what has to be
done"; the order reversed is taking a solution for a requirement.

Work these three out and lay them out for him together:

- **What is to be reached**: work it out from his sentence, the project's goal docs, the nodes
  already in the graph and the current state together — his one sentence alone is not enough; the
  project itself is also saying what he can accept
- **What this round has to settle**: the few things without which nothing can move on — the scope,
  the trade-offs, the boundaries, the relation to the nodes already there
- **What is not wanted**: what this round will not do also gets said, and what is not said will be
  taken as something missed

Only once he is aligned do you split the work. **When he has not finished, do not write it down**
— the talking is still going and the trade-offs are not settled, and writing a node down here
presses a discussion that has not converged into a conclusion; that goes most of all for the round
in which the user first states the request, where all you hold is his sentence and the docs just
read. The moment a node is written down, its spec is the criterion downstream reads only, and
changing it moves the graph.

Where the detail is not settled, leave it unsettled: the spec carries only what has been settled,
and does not guess on his behalf.

### Step 3: split the work and pick a road

Split what the user said this time **into one piece of work after another**, then lay the whole
set out for him to confirm — whether the value is judged right, whether things should be merged or
split, whether the dependencies are derived right, which road each piece takes, which mode and
weight each carries. Lay this out together with the result of "one pass before writing it down",
and write it down only once he nods; mode and weight get no confirmation round of their own.

When laying it out, give him options to pick from (draft A / B, the three modes, the two weights)
and do not state them as done facts; when he cuts in, the talking was not finished, so ask again,
and do not take it as consent. One input often brings three roads at once — some can be done right
now, some have to enter the graph and queue, and the queued ones have an order among themselves.

- **Value**: whether this feature is necessary and whether it brings the experience the user wants
- **Relation**: with nodes already in the graph, think once about a new node's relation to them
  before concluding — belonging to one feature, having an order, sharing a prerequisite, being
  wholly independent are all legitimate answers; "independent" counts only once it has been
  thought through, and the conclusion goes to the user along with the analysis
- **Dependency**: infer the prerequisites from the graph and the docs, and who should depend on
  it. Pull a precedence edge between two pieces of work that change the same batch of files —
  nodes running in parallel each open a worktree and, without that edge, the merge is resolved at
  the crash site; the spec carries no file paths, and this judgement is made when the work is
  split. **The overlap criterion takes the file set read out at the implementation layer** (when
  you cannot judge it yourself, dispatch a subagent to read it and bring back the file list), not
  an impression from the spec. **How adding edges and the line's `parallel` divide the work**:
  when two nodes would change the same batch of files, **add an edge on the graph**, and settle
  `parallel` only by "how many nodes are really confirmed independent of each other" — **when the
  edges cannot be added (three nodes colliding pairwise, falling into a cycle), the right
  conclusion is that they should become one node**, and take care not to dodge into lowering
  `parallel`

Each piece of work picks one road:

| Which road | The criterion |
|---|---|
| **Do it now**: dispatch a subagent, no graph entry | nobody waits on it, one look judges it when it is done, no cross-session needed — all three hold |
| **A light node**: mark `light` | it has to queue (there is an order or a dependency), but the work is small and the verification light: the user mentions it in passing and the conductor tidies it up itself |
| **A standard node**: write no `weight` | it has to queue, and the verification needs care (a heavy criterion, several review rounds), possibly across sessions |

Work that lands on the first road, in a few shapes off the top of the head: bring docs and design
notes up to what is already implemented; run a probe or read a piece of code to answer one
feasibility question; pick the current state back up for the user (an inventory, a check, a
rendered graph); reproduce a problem to confirm whether it is there; repository hygiene (stale
temporary products, duplicate issues); write a settled conclusion into the project's
`.pf/config.json` (`pf config set`); record debt run into in the issue pool; make a technology
comparison for a node not yet written down and back-fill the conclusion into its spec.

- **A behavioural change always enters the graph**; small changes where **not a byte changes when
  it runs** — comments, docstrings, formatting, docs only — can be done right now, and the trace
  is that one commit
- Docs that have to wait for some node to finish belong to that node's close (the `doc-update`
  step), and do not go through "do it now"
- Once picked, look at the order: when the pieces to do now are somebody's prerequisites (a
  probe's conclusion has to enter a spec, docs have to catch up with the state), clear them first
  and then write down — the order reversed and the graph gets written crooked
- **First sort out who runs this thing.** When the conductor decides to take subagents through it
  in this session, do not write it into the graph — the graph is for work that crosses sessions,
  queues and leaves review records; for what it runs itself, the trace is that one commit and the
  process records go in the workspace root's `.tmp/` or `.pf/nodes/<node uuid>/temp/` — both of
  those are kept out of the repository. Writing it into the graph only detaches the node's status
  from the facts (the record says `todo` while half the work is done), and whoever takes over
  later also thinks something is queued

#### One pass before writing it down

Once the work is split and the edges are connected, press it once yourself and lay the result out
with the plan — without waiting for him to ask:

- **Merge what can merge**: two pieces of work changing the same batch of files, with one
  completion rule and one verification round, are one node — and after merging it still has to be
  a size one execute-review-fix round can carry
- **Lower what can lower**: one marked standard that is really "one file, a few dozen lines,
  judged by one or two commands" drops to `light`
- **Do not build a discussion node**: its content is "discuss with the user how to do this" — this
  session is already doing that very thing, and queuing it only does the done thing over again. A
  topic that has to be talked through on its own (story architecture, gameplay trade-offs, the
  world's setting) is the exception: write it down as `manual`, and its product is the discussion
  write-up
- **Do not build a final-acceptance node**: the "on-device testing / integration / final
  acceptance" that a human judges after everything is done. The criterion is in the user's hands
  (the feel, the experience, whether it looks right) and yields no reading; every node's own
  completion rule has already been verified once, and this last gate is the user's. Integration
  that does yield a reading (the interface is through, the two sides line up) is built as usual

### The four questions to ask before writing it down

These four are settled when the node is built, which is the cheapest moment to change them;
dragging them past the start costs that node already running.

**One: who judges this node's acceptance.** `auto` by default — the pipeline exists to let an
agent finish the work that would otherwise need watching by itself; `manual` and `checkpoint` are
both rarely used, but they have to be there when needed. There is one criterion for going up:
whether the acceptance needs something only the user can provide.

| What the acceptance rests on | The node's mode |
|---|---|
| the agent can run it out itself, with no need for you to watch the process | `auto` |
| the agent can run it out itself (tests, scripts, probes, offline material) and the process does not bother you, but every node stops at its close and waits for one "continue" from you | `checkpoint` |
| a real meeting, the user clicking through the interface, his feel and his ear | `manual` |

For which step each of the three has its human confirmation point on, and why it is fixed that
way, see `docs/conduct-state.md` "conclusions and stopping" in the tool checkout.

Work whose product is itself the delivery (an inventory, a technology choice, a setting document)
runs as usual, with the acceptance written as "the product is the delivery". An acceptance that
needs the user in it to judge can only land on `manual` or `checkpoint`: an agent can only mock
the whole system and wire it up to itself, and the money and the GPU memory burned buy no such
conclusion.

**Two: how heavy the work entering the graph is.** Split it again by that table in step 3: small
work with a light verification is marked `light`; the rest goes as `standard`.

**Three: whether the spec passes the executing side's gate.** Only work entering the graph has to
pass this gate: a node landing in the pipeline has to satisfy those three from "Nodes and
downstream" — one framework-level description, a size one session can carry, and one executable
completion rule. When any one of them is not satisfied, change the node or split it first, and
write it down only after the user nods.

**Four: whose run this work's running is.** To clear debt (a batch of issues in the pool), **do
not write debt-clearing steps yourself**: write one line, `method: pf-issue-repair`, into the spec
— that line means "this node is run by the conductor from that card itself", and the order
outranks `weight` (see "Method" in [`../pf-conduct/SKILL.md`](../pf-conduct/SKILL.md)). In the
same spec, write the scope and the criterion out clearly: the scope = one metric or one batch of
same-domain debt, the criterion = that reading coming back under the line (or that batch of issues
reaching a terminal status). A node of this kind also has to carry the tag of the metric it
clears, like `coding,maint:issue_pool_weight` — only with `maint:` does it enter the maintenance
channel's dedup (otherwise the close inserts another node clearing the same metric automatically),
and only with a line tag can it be picked. A plain node carries no `method`.

### Step 4: landing

**Do it now (no graph entry):**

Dispatch subagents to do it, several in parallel if you like (first count what is in flight
against [`../shared/pf/subagents.md`](../shared/pf/subagents.md)), and **they only change files,
they do not commit**; when it is done, report to the user, and commit in step 5 together. Three
things belong to this road:

- **Docs and design notes catching up with the state**: go through `doc-update` and write only
  what is already implemented — docs/ holding ground truth only is this system's foundation
- **A non-behavioural small change**: comments, docstrings, formatting, docs only — the kind where
  not a byte changes when it runs
- **A one-off feasibility probe**: run a script, read a piece of code, and answer "can this be
  done"; it changes no file and leaves no node

A behavioural change always enters the graph. When these pieces are a node's prerequisites, wait
for them to come back before writing the spec.

Commands, options and values follow `pf dag --help` (already read once in step 1); this section
carries the judgements and the conventions only.

**Adding a node:**

1. The node's name describes itself — understandable with no document in hand; internal numbers
   like "direction X" or "part X" will not do
2. mode and weight are settled by "The four questions to ask before writing it down", and written
   in with the node
3. Edges: first sweep for who should depend on it, connecting both up- and downstream, then check
   the full up- and downstream once; an empty downstream is an orphan, so go back and fill it in
4. Writing it down: the tool prints the spec path for you, and that file does not exist yet —
   write it directly, do not read it first. Check yourself once: the goal in one sentence, the
   size in one "yes", a completion rule in one section (an executable command / script / probe);
   the debt-clearing kind adds one `method: pf-issue-repair` line and its scope; anything with
   function names, file paths or line counts written in gets deleted
5. The session a node is born in is not yours to write: the tool records it itself when the node
   is built (`pf dag info`'s `sessions.discussion`, or `pf session show <node>`). To trace it back
   — judging whether this node is the user's own intent, or a spec written crooked, or a situation
   that changed — follow that id back to that conversation for the user's own words. That id means
   something on this machine only: on another machine that conversation is not there, and not
   being able to read it is a normal reading
6. Report the new node and its up- and downstream — where it lands in the graph, said clearly to
   the user

**Changing the graph:**

- The title, the description, the mode and the weight can all be changed
- Connecting an edge, cutting an edge and deleting a node all move other nodes: check the up- and
  downstream before touching anything and lay the connections that would break before the user;
  after the change, check the topology once more
- Deleting a node: the node and its spec vanish together; when another node depends on it the tool
  refuses and lists the dependents, and only an explicit "break the edges with it" from the user
  gets past that refusal
- Changing the status: one of `todo` / `shelved` / `completed` — a node's own status has only
  these three, and "a cycle is running it right now" is derived from here. Writing it down is
  pf-conduct's close, so do not record it for it
- To shelve a node (put its execution off) → mark it `shelved`; what was shelved, or judged dead
  at acceptance, and has to be redone → put it back to `todo`

### Step 5: close out

Collect this run's things into commits, in two shots:

- **One commit for the graph and the spec** (`docs:`, with the description saying which node was
  added / changed): `.pf/ideas.json` and `.pf/nodes/<uuid>/spec.md` — this record stays in the
  workspace, and another machine cannot see this node
- **One commit for what "do it now" produced** (`docs:` / `style:` / `chore:`, picked by what
  changed)

Give the user both shas along with the report — that commit is the trace of "do it now". Then
report to the user what changed and what the topology is now. To run it right now, use
`/pf-conduct` — it dispatches the graph's ready node; to name a node, name it on the call line.

## Query mode

What is in the graph, which nodes are ready, what state some node is in — all of that goes through
the dag tool's query commands (the full set is in `--help`). Listing the graph sorts by
urgency×importance by default and sinks completed to the bottom; asking about one node locates it
by keyword, and to judge its content, read its spec.

Distil the key information into the answer; do not dump the raw JSON.

## Boundaries

- **pf-route** (you): the graph is yours. Adding, changing and dropping nodes, connecting edges,
  querying the graph; writing the DAG, the spec and the project's settings (`pf config set`);
  reading no code, running no node, changing no project code
- Small things that do not need a graph entry (docs catching up with the state, a non-behavioural
  small change, a feasibility probe) go to a subagent, and you still do not touch a file's body
  yourself
- **pf-conduct**: running the graph, doing the nodes. Picking a node from the graph, opening a
  cycle, one node from prepare to close; building no nodes
- **pf-issue-scan / -repair / -discuss**: the pool's own branch — scanning into the pool,
  repairing debt back under the line, ruling with the user on external (the conventions are in
  [`../shared/pf/pool.md`](../shared/pf/pool.md)). To clear one round of debt on the graph, build
  a node carrying `method: pf-issue-repair` (see the fourth of "The four questions to ask before
  writing it down")

When the user wants to talk a node's implementation through at length → suggest moving to
`/pf-conduct` (at prepare it aligns the request with the code); when it is only a rough
understanding to judge feasibility → dispatch a subagent yourself.

## Anti-patterns

Before starting, read [`./anti-patterns.md`](./anti-patterns.md) and check yourself against it.
