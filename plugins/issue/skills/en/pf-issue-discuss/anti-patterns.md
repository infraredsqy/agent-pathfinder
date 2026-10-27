# pf-issue-discuss anti-patterns

Check yourself against this before starting. The criteria themselves live in
[`./SKILL.md`](./SKILL.md); this keeps symptoms and correction signals only.

## `issue-discuss-batch-over-decision-quota` — a batch over the 4-decision quota

**Symptom**: a dozen externals are laid out at once, each asked "change it or not? A or B?"

**Correction signal**: the user starts answering "you decide", or this batch laid out five or
more.

## `issue-discuss-submits-unreviewed-externals` — submitting externals without reviewing them

**Symptom**: whatever is marked external in the pool is served up as it stands — of twenty
entries, twelve actually have a suggestion that already spells out the fix.

**Correction signal**: on the first one put in front of the user, you have not tried writing a
concrete fix for it.

## `issue-discuss-details-instead-of-tradeoffs` — giving the user detail instead of trade-offs

**Symptom**: the discussion throws function names / variable names / line numbers / call chains at
the user, leaving him to find the decision point himself among the code detail.

**Correction signal**: the user comes back with "so what is it that I have to settle".

## `issue-discuss-reason-records-discussed-only` — `--reason` says "discussed", not the call

**Symptom**: the user says "report errors uniformly", and the reason carries only "discussed".

**Correction signal**: showing that `--reason` to whoever does the work later, he cannot read what
the user settled.
