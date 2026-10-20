# pf-issue-repair anti-patterns

Check yourself against this before starting. The criteria themselves live in
[`./SKILL.md`](./SKILL.md) and `/pf-conduct`; this keeps symptoms and correction signals only.

## `issue-repair-skips-review-for-throughput` — skipping the review for throughput

**Symptom**: with many entries you think "this one is too small to review" and commit right after
the Fix — and you cannot judge what counts as "no review needed".

**Instance**: a change that "just lifts an RAII guard into a shared header" missed an `#include`,
did not compile, and only the review ran into it hours later.

**Correction signal**: this batch has the Fix report only, and not the Review one.

## `issue-repair-mixes-change-domains-in-one-batch` — mixing change domains into one batch

**Symptom**: entries from different modules that cannot be verified in one pass are packed off to
the same Fix; or fixing an entry inside the batch changes code outside it in passing — what is
smuggled in from outside pollutes the batch's boundary, and the review cannot trace it.

**Correction signal**: this batch's entries land in two or more modules, while there is only one
verification.

## `issue-repair-batch-composition-unreported` — the report not listing the batch composition

**Symptom**: how the batches were cut and what each holds goes unmentioned at the close.

**Correction signal**: the report has the readings and the settling table, yet the reader cannot
piece together which entries this batch changed or which commit it took.

## `issue-repair-acknowledge-or-external-as-exit` — acknowledge or external as repair's exit

**Symptom**: when it cannot be fixed, it gets `acknowledge` — putting an entry off is the pool
human face's (`pf-issue-discuss`) action, and the repair side does not do it; the other mistake of
the same family is re-ruling an entry that has already been given a solution, and whose solution
still holds, as `external`.

**Correction signal**: `acknowledge` shows up in the settling table, or an entry given a solution
is turned into `external`.

## `issue-repair-batch-uncommitted` — a batch that passed review but is uncommitted

**Symptom**: a whole batch is changed and the review has passed, and the next batch is opened
right away, with no commit.

**Correction signal**: the next batch's changes are mixed into the same uncommitted change as the
previous batch's.

## `issue-repair-widens-scope` — widening the repair surface past the scope

**Symptom**: the spec says "pull this one metric back under the line", and you clear the pool's
other domains in passing.

**Correction signal**: among the entries this run resolved, several have a metric that is not in
whatever called you in.
