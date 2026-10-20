# pf-route anti-patterns

Check yourself against this before executing. The criteria themselves live in
[`./SKILL.md`](./SKILL.md); this keeps symptoms, correction signals and instances only.

## `route-reads-source-code` — reading the project's source code directly

**Symptom**: the user mentions some feature, and the agent reads the source file directly

**Correction signal**: the user says "why are you looking at the code"

## `route-edits-project-files-directly` — changing project files directly, past the pipeline

**Symptom**: during the discussion the agent edits / writes project code or doc bodies itself

**Correction signal**: the user says "I did not tell you to change the code"

## `route-advances-without-reading-graph-or-docs` — advancing without reading the graph or docs

**Symptom**: the user gives an idea, and the agent reads neither the DAG nor the project docs and
goes straight into discussing it and writing nodes

**Correction signal**: the user says "this node is already there", "yours clashes with the earlier
one"

## `route-dumps-raw-data-at-user` — showing the user raw, unrefined data

**Symptom**: the user asks "what is in the graph", and the agent dumps the whole node JSON at them

**Correction signal**: the user says "that is too much", "get to the point"

## `route-node-name-depends-on-source-context` — a name that depends on its source doc

**Symptom**: the node name uses an internal number or a section reference from the source doc,
like "direction 5" or "direction 1"

**Correction signal**: the user says "this name does not make sense", "a name should describe
itself"

## `route-creates-orphan-node` — a new node with no downstream declared, an orphan

**Symptom**: a new node gets only its upstream dependency (who it depends on) and no downstream
dependency (who should depend on it) — an orphan is in nobody's recommended path, its urgency and
importance are 0, and it is as good as shut out of the pipeline

**Correction signal**: listing the graph, this node shows up in no recommended path

## `route-deletes-node-or-edge-without-impact-check` — deleting a node or edge, no impact check

**Symptom**: the user says "delete this node" and the agent deletes it by force; or an edge comes
off with no check of the up- and downstream — what was cut is somebody's prerequisite, and once it
vanishes silently the downstream node is run on a false premise, with nothing on the graph showing
anything was ever missing

**Correction signal**: the user says "why did you cut that edge for me", "who told you to delete
it"

## `route-splits-without-verification-boundary` — a split with no verification boundary

**Symptom**: the node split the agent proposes cannot name an independent verification standard.
Either several nodes share one verification (split too fine), or one node mixes two different ways
of verifying (split too coarse)

**Correction signal**: the user says "these two should be bound together", "why split it that
fine"

## `route-spec-has-implementation-detail-or-no-done-check` — detail in the spec, or no done check

**Symptom**: the spec carries function names, file paths, line counts, call order; or it is all
"do the design for X" / "tune Y", with no acceptance to be found that yields a pass / fail reading

**Correction signal**: pf-conduct's prepare stops and says "the completion rule cannot be run" or
"the spec and the code's premise do not hold"

## `route-lowers-parallel-instead-of-adding-edges` — lowering parallel instead of adding edges

**Symptom**: on finding that two nodes (or three) would change the same batch of files, the agent
goes and turns this line's `parallel` down or to 1; the reason is usually written as "for safety"
or "to avoid a crash"

**Correction signal**: the user says "a crash means add an edge, not lower the parallel", "do not
patch it with parallel"

## `route-writes-node-before-requirements-settle` — writing a node before the requirements settle

**Symptom**: the user is still talking the design through and the trade-offs are not settled, and
the agent presses this round's discussion into a conclusion, builds the node and writes the spec,
with no plan laid out in between

**Correction signal**: the user says "you jumped too fast", "stop first and talk it through with
me", "kill every node you just added"

## `route-creates-discussion-or-acceptance-node` — a discussion or final-acceptance node

**Symptom**: nodes like "discuss the design of X with the user" or "on-device testing /
integration / acceptance after everything is done" show up in the graph

**Correction signal**: the user says "this node does not need building", "I will try this one
myself"
