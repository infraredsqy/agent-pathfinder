# Review subagent anti-patterns

Check yourself against this once you have a review task. The criteria themselves live in
[`../subagent-prompts/review.md`](../subagent-prompts/review.md); this keeps symptoms and
correction signals only.

## `conduct-review-fixes-code-itself` — changing code yourself during the review

**Symptom**: you find a problem and go ahead and fix it, reporting only that this one was not
done.

**Correction signal**: the workspace changed during this round, and your prompt only let you read.

## `conduct-review-only-rechecks-last-issues` — rechecking only the last round, then PASS

**Symptom**: the second and third rounds only check whether the previous round's critical / minor
issues are fixed, and report PASS as soon as they are.

**Correction signal**: this round's list holds only the previous round's few items.

## `conduct-review-adds-features-to-spec` — adding features the spec never asked for

**Symptom**: "A is a prerequisite of B, and the spec never wrote A", so the report says "A needs
to be added".

**Correction signal**: an issue on the list asks for something the spec never mentioned.

## `conduct-review-judges-other-nodes` — judging a prerequisite node or another node out of bounds

**Symptom**: a prerequisite node's missing feature, or another node's work, is marked critical /
minor.

**Correction signal**: an issue's location falls outside this node's spec scope.

## `conduct-review-accepts-preexisting-failure-claim` — taking the pre-existing claim at face value

**Symptom**: the executing side says a test failure is pre-existing, and the review nods it
through.

**Correction signal**: the report only retells it, with no command and output you ran yourself.

## `conduct-review-reports-unrun-verification` — reporting a verification result without running it

**Symptom**: the report says "the verification flow should pass" or "the tests are expected to all
pass", with nothing actually run.

**Correction signal**: the conclusion is in the speculative mood, and the command column is blank.

## `conduct-review-count-line-missing-or-mismatched` — no count line, or a count that mismatches

**Symptom**: the report is a pile of analysis with no count on the first line, or the count does
not add up against the list.

**Correction signal**: you take the first line's two numbers down the items and they do not match.

## `conduct-review-copies-known-material` — copying material the reader already holds

**Symptom**: one issue's location lists ten line numbers and every field after it lists them
again; long stretches of the spec or the manuscript are quoted verbatim as "grounds"; it opens
with a table of several files' `sha256` and line counts. Four hundred and more lines in a round,
judgements making up less than half of it.

**Correction signal**: the copied lines outweigh the judged lines.

## `conduct-review-clamped-reading-as-evidence` — a clamped reading passed off as evidence

**Symptom**: the proving script contains something like `segs = Math.min(slots, …)`, which clamps
the excess back under the cap, and so "zero breaches" holds; run it again without those two clamps
and the real overrun shows up.

**Correction signal**: the proving script reports "all pass" under the old behaviour too.
(Instance: one node's fourth-fix-round proving script, `fix4-verify.mjs`.)
