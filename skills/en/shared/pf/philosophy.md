# The pf system's design philosophy

> This file describes the pf system's background and design principles; pf-route cites it and does
> not retell it.

## The core problem

While developing, the mind jumps on its own: you are working on feature A and ideas B / C / D pop
up. Those ideas cannot be held, and writing them into docs/ pollutes the source of truth — an
agent cannot tell "already implemented" from "a future plan". Trying to keep the ideas in your
head costs the current work its focus.

## The system's goal

**An external brain.** Unload ideas from the human brain into structured storage. Once the user
has emptied his mind he can focus on the current work, and the ideas are neither lost nor
forgotten.

## The key designs

### A DAG, not a list

Ideas have natural dependencies among them (B can only be done once A is finished). A linear list
loses that structure, and a hand-kept priority goes stale.

The graph's topology derives two core metrics on its own:

| Metric | Meaning | The graph algorithm |
|------|------|--------|
| **urgency** | can it be started now | a node nearer the front of the topology = its dependencies are met = the most urgent |
| **importance** | how many things depend on it | a node with a large reverse-reachable set = many things depend on it = the most important |

The two metrics are computed for **unfinished** nodes only. Once a node is done, the nodes
depending on it become more urgent on their own, and urgency / importance update dynamically, with
no hand adjustment.

The two together make the **four-quadrant decision frame** (the Eisenhower Matrix's counterpart on
a DAG), and it is the ground on which an agent helps the user judge which nodes to keep or drop:

- **Urgent and important** (small in-degree + large out-degree): few dependencies, so it can be
  done at any time, and many things depend on it. A cornerstone feature, MUST DO NOW. The highest
  priority.
- **Important but not urgent** (large in-degree + large out-degree): a large effect, but its
  prerequisites are not done. It cannot be done now, so clear its prerequisite nodes first.
- **Urgent but not important** (small in-degree + small out-degree): few dependencies, so it can
  be done at any time, but nothing depends on it. Doing it affects nobody else, so consider
  whether it is worth the time.
- **Neither urgent nor important** (large in-degree + small out-degree): neither urgent nor
  depended on. The icebox, and the lowest priority.

### A finished node is not deleted

A node that is done is marked `completed` and its node directory stays where it is — no
unmaintained, orphaned MD file is left lying in the filesystem. Only when the idea itself is
scrapped and the user wants it out of the graph does `pf dag rm` really delete it.

In practice `pf dag list` shows everything by default but sorts by urgency×importance, with the
completed nodes at the end (and no score shown). When a UUID you have never seen shows up in
another node's `deps`, it most likely points at a completed node — confirm with
`pf dag list --status completed`, and do not rule it a dangling ref straight away.

### Ground truth stays clean

Once every future plan has moved into the pf system, the project's docs/ holds implemented content
only. An agent reading docs/ can trust that what it reads is the present state, and no longer
mixes up "is this a plan or is it implemented".

Not everything enters the graph: bringing docs up to the state, running one probe to answer a
feasibility question, a non-behavioural small change — those are done on the spot (the criterion
is pf-route's "do it now"). The graph holds "what we are going to do"; those pieces of work write
down "what is there now".

## Its relation to the skills already there

The **pf-conduct** pipeline (one pf-conduct node finishing: prepare, execute, an independent
review, fix, close out) answers "how do we implement it".

The pf system answers "what are we going to do" — it is the pf-conduct pipeline's upstream input.
An idea enters the DAG from pf-route, and pf-conduct refines it and executes it.
