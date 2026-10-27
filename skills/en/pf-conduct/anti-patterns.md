# pf-conduct anti-patterns

Check yourself against this before starting. It keeps only the ones **no tool will stop you on the
spot, but you will walk into without noticing**; the criteria themselves live in SKILL.md.

## `conduct-treats-critical-as-false-positive` — the conductor rules a review a false positive

**Symptom**: you disagree with the review's verdict and rule it yourself — "this is not really a
problem" — and let it go.

**Correction signal**: the report says "this one is a false positive", and the re-run of the
review never happened.

## `conduct-polls-for-subagent-completion` — waiting for a subagent by polling

**Symptom**: after dispatching a subagent, you check its product every so often with `Start-Sleep`
plus `Test-Path`, or call `list_agents` over and over. The duration is picked by feel (90 seconds,
240 seconds), every round burns a stretch of measured time for nothing, and the polling goes into
the log as "progress".

**Instance**: one node's session piled up 22 waits of this kind, each with a line reading
`# wait-guard: a necessary wait - waiting for some subagent to write report.md`.

**Correction signal**: `Start-Sleep` and `Test-Path` show up in pairs in the log. A
`# wait-guard:` is for waiting on time itself — a backoff retry, a rate-limit window.

## `conduct-flat-count-read-as-convergence` — a flat review count read as convergence

**Symptom**: the review reports `critical=0, minor=10` round after round, and the conductor keeps
dispatching the next round because the numbers did not go up; or the same spot is changed three or
four times, each round another spot produces a fresh critical, and you keep changing things along
with what the review says.

**Correction signal**: the review's identity marker on its first line lands on the same set of
spots two rounds running.

## `conduct-constraints-without-provenance` — an unmarked constraint read as hard all the way down

**Symptom**: one node's session works out a budget or a cap, and writes it into a document in the
same wording as the user's own requirement; the nodes after it copy the reference, and it becomes
a hard constraint nobody can touch.

**Instance**: a project's goal doc (`goals.md`) does not contain a word of "chapter / segment /
slot", and its design constraints all sit at the experience level (a reader is never left
waiting, two panes never disagree, the outline stays visible); "a chapter holds at most three
segments" was worked out by one `layout-budget.md` node's session, quoted layer by layer by
`section-spec` / `render` / `diff-check` / `outline`, and finally locked horns with another one
it derived itself, "a named diagram fills the chapter it opens in", grinding away in the middle
for five rounds across two nodes.

**Correction signal**: a "must / must not / always / the cap is" whose source cannot be found
anywhere in the user's own words or the goal docs.

## `conduct-freezes-criteria-with-findings` — criteria frozen along with the findings

**Symptom**: you read "word for word" as "the supplement's text is frozen" — the boundary moved (a
line unfrozen, the scope narrowed, a user criterion swapped in), yet that criterion goes out unchanged
in the next round's review prompt. The review judges by a stale standard, so what it reports is a
defect in the standard; the conductor then handles it as a defect in the delivery.

**Instance**: a node's second round ruled "the stale name in that one comment has to go, the rest
stays frozen" and told the fix so; the third round's supplement still read "any change under that
directory is a critical", and the review reported a critical off it (1 insertion / 1 deletion, zero
runtime impact) which the user ruled did not stand.

**Correction signal**: one criterion in this round cannot hold at the same time as the ruling you
gave the fix last round.

## `conduct-hand-edits-ledger-or-resolves-conflicts` — hand-editing `.pf/` or ruling a conflict

**Symptom**: to line the two sides' graphs up, you read and edit a record file under `.pf/`
yourself; or a pull stops on a conflict and you splice the two JSON files together line by line.

**Correction signal**: a hand-edited record file in the workspace, or a stretch of JSON spliced
together line by line.
