# Execute / Fix subagent anti-patterns

Check yourself against this before executing a spec. The criteria themselves live in
[`../subagent-prompts/execute.md`](../subagent-prompts/execute.md); this keeps symptoms and
correction signals only.

## `conduct-execute-modifies-spec` — the executing side changes spec.md

**Symptom**: you find something in the spec unreasonable and change it to your own reading, or
settle a "to be confirmed" item by guessing.

**Correction signal**: the report carries a goal that is not in the spec, and spec.md changed
after the moment work started.

## `conduct-execute-adds-out-of-scope-work` — adding work outside the spec yourself

**Symptom**: reading the code you see a spot that "is still missing a piece" and fill it in — the
spec never had that in it.

**Correction signal**: a line in the change list whose matching spec item cannot be named.

## `conduct-execute-parallel-edits-other-file-group` — editing files outside your group in parallel

**Symptom**: under parallel execution you change a file that is not in your file group and collide
with another agent.

**Correction signal**: a file in the change list that was not handed to you.

## `conduct-execute-does-not-stop-on-blocker` — hitting a blocker and not stopping to report

**Symptom**: tests keep failing and you keep retrying; the spec and the real code do not line up
and you keep changing things anyway; you muddle through an operation you are not sure about.

**Correction signal**: the report is one long "let me try again", with not one FAIL in it.

## `conduct-execute-runs-irreversible-operations` — running irreversible operations

**Symptom**: destructive work done under the banner of "fixing the problem" — deleting files or
directories in bulk, `git reset --hard`, overwriting on a wide scale, deleting `.git`.

**Correction signal**: at the moment before touching anything, you cannot answer "how does this
step get undone".

## `conduct-execute-continues-past-failing-tests` — carrying on past a failing test

**Symptom**: a test failure is reported with "it does not affect what comes later" tacked on.

**Correction signal**: the report's passing readings do not line up with the files this round
touched.

## `conduct-execute-assumes-failure-preexisting` — ruling a failure pre-existing yourself

**Symptom**: on seeing a failure you conclude "this was already broken before" and skip over it.

**Correction signal**: the report has the conclusion and nothing else — no test, no command, no
output.

## `conduct-execute-vague-deviation-report` — a deviation report that names nothing

**Symptom**: something in the work departs from the spec and the report says "a few small
adjustments".

**Correction signal**: the deviation item is all adjectives, and no one can read out where it was
or what was actually done.

## `conduct-execute-pastes-evidence-into-report` — pasting evidence into the report's body

**Symptom**: raw command output, synthetic samples, hashes and long quotes go into `report.md`
whole; one delivered file comes with a thousand lines of explanation.

**Correction signal**: the report is too long for the next link to finish, and most of it is
material the reader already holds.

## `conduct-execute-appends-stale-readings` — appending to a stale reading instead of correcting it

**Symptom**: a number in the body changes later, so you add "**historical reading**: …" beside it
and leave the old reading where it was. The report goes from a few hundred lines to over two
thousand (instance: one node's manuscript ran 429 lines and its `report.md` 2017).

**Correction signal**: the same number appears twice in one report, and one of them is marked
"historical".

## `conduct-execute-dispatches-on-conductor-instructions` — dispatching on the conductor's notes

**Symptom**: the spec or a method card says "batch by change domain, dispatch Fix / Review per
batch, commit once a batch passes", and you start dispatching subagents along with it — one layer
inside another.

**Correction signal**: a dispatch action shows up in your step, and your prompt does not carry
that item.

## `conduct-execute-defers-as-delivery` — "known unresolved" standing in for delivery

**Symptom**: one item in the spec cannot be done now, so you mark it "known unresolved" and call
it handed over.

**Correction signal**: the report ends on a "known unresolved" that cannot say what it is missing,
who can solve it, or which acceptance items it touches.
