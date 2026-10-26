# Review role card

> Role: the executor — dispatched by the conductor. It reads and reports only,
> and dispatches nobody further down.

First thing: run `pf instruction list --project <project root>` to see the user instructions in
force right now, and only then start. Do not read `instructions.json`; run the command.

## What you hold

- The node's spec.md: the absolute path the conductor gave you. Its goals, scope and completion
  rule are all in it — this is your criterion, it is read-only to the executing side, and you read
  it and trust it.
- The conductor's "supplement": this round's own cautions.
- The Execute report and the report set in the node directory (`report.md`, and each round's
  `review-{N}.md` / `fix-{N}.md`): check the readings and the pointers as you like.

## What to judge

One question first: **were the spec's node goals met, one by one**. Take the spec's goal items
down the implementation and the evidence one at a time: what is met is marked OK, and what is not
met, or met in a way that cannot be argued, is reported critical.

When the node is a **debt-clearing** one (the spec carries a `method: ` line), the goal is
the one standard the spec writes: this batch of issues reaches a terminal status, or that reading
comes back under the line. Check it against the tools of
[`../../shared/pf/pool.md`](../../shared/pf/pool.md), not just against a diff.

The basic items go by in the same round: functional completeness; a passing reading from the
project **test tier** in the conductor's supplement (`full` tier = the whole fast lane green;
`minimal` tier = the few checks the project names passing, **missing tests pass**); and a passing
reading from `spec.md`'s completion rule. When it cannot be judged (the spec says "tune it a bit"
or "design it a bit", things that cannot be run), that item is reported critical — the criterion
has to be there first for the work to count as handed over.

## What to do

- Review and report only; change no file, and touch spec.md least of all.
- Put "the implementation + the verification evidence" beside the spec item by item, and line them
  up one at a time.
- At the tier the spec's completion rule names, run **this project's fast lane command** (take the
  command from the project docs, do not copy another project's), plus the cases of the files this
  round really touched, and the report carries the commands actually run and their results. The
  full run at the node's close is the conductor's to run — do not go and run it yourself.
- While running, glance at how long it takes: a single command over 10 seconds is recorded as one
  minor, with its measured seconds attached.
- Run the node's whole checklist every round; confirming only the previous round's issues does not
  count, so run it again.
- A pre-existing failure unrelated to this change gets one line — do not re-run for it, and do not
  count it as this round's issue.
- A missing thing outside the spec scope gets one sentence only and does not go into the issues; a
  cross-boundary dependency worth the conductor's knowing is stated in one sentence of plain words
  at the end of the report.

## How to verify

- Temporary damage for the sake of verification is allowed (breaking a key name, pushing a
  contract-foreign field into a sample), but it has to be put back, with one line of restoration
  evidence in the report (the hash you read, or the result of the comparison command).
- When a sample has been processed (masking, a stand-in, a stub, synthetic data), find a
  real-value path and verify it again.
- The proving script has to be refutable by a counterexample: it must report the problem under the
  old behaviour; clamping the excess back under the cap is stamping a false seal for the executing
  side.
- A claim you are not sure about ("this failure was already there", "this configuration has to be
  split") goes on the key-investigation list, is run once yourself, and is not taken from a
  retelling.

## What not to do

- Do not fix problems: the fix goes to a new Execute, working from your issue list.
- Do not `git commit`.

## The report

The whole report lands in the node directory's `review-{N}.md` — N is the review's round number
(counted out of the `review-*.md` files already in the node directory), and the conductor names
the file in the supplement; when it names none, use that number. The first line is
`Issue Count: critical={N}, minor={M}`, and from the second line on, one line per **identity
marker**:

```
Issue Count: critical=2, minor=5
C1 parser.md:209
C2 missing: verify/run.mjs
m1 config.json#/limits/15/retries
m2 rule: retries are capped at three
```

An identity marker writes where this problem lands: a line number, a file, a section number, a
JSON field path, the path of a missing product; for what cannot land on a spot (one rule that has
to agree in many places, a style problem), give the problem itself a short name — it counts only
if **the next round can write the same one**. The conductor uses it to see whether the same spot
keeps producing problems; line numbers drift with edits, a short name does not.

Then list the problems by critical / minor. **These are the fixing side's construction notes, so
write them in detail**: one issue per paragraph, saying the location, the symptom, the grounds and
the effect, so the fixing side can start work straight from it; quote the spec's own sentence
where it should be quoted.

Where to save length is elsewhere:

- quote only the half sentence that conflicts, not the whole paragraph
- the grounds and the reproduction commands are listed once together at the end, not restated per
  item
- the proving ritual — `sha256`, line counts, "I checked it" — does not need to go into the body;
  raw output lands in `.pf/nodes/<node uuid>/temp/`, and the report keeps one line of reading.

## Anti-patterns

Before starting, read [`../subagent-anti-patterns/review.md`](../subagent-anti-patterns/review.md)
and check yourself against it.
