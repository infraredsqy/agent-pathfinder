# pf-test-audit anti-patterns

Check yourself against this before starting. The criteria themselves live in
[`./SKILL.md`](./SKILL.md), [`./garbage-patterns.md`](./garbage-patterns.md) and
[`./compression-criteria.md`](./compression-criteria.md); this keeps symptoms and correction
signals only.

## `test-audit-conductor-reads-code` — the conductor reads code

**Symptom**: the conductor flips through the test source files with Read.

**Correction signal**: what the subagent reports, the conductor had already seen in the source.

## `test-audit-ships-without-verifying` — shipping without verifying

**Symptom**: nothing is compiled and the remaining tests are not run after the change.

**Correction signal**: the close report carries no compile or test reading.

## `test-audit-deletes-a-whole-mixed-case` — deleting a whole mixed case

**Symptom**: only some assertions in a TEST_CASE are garbage, and the whole thing gets deleted.

**Correction signal**: the deletion grain is the whole container, while assertions verifying
business are still left in it.

## `test-audit-empties-a-file-without-the-build` — emptying a file without the build system

**Symptom**: a test file emptied out stays where it is, and the build system still compiles it.

**Correction signal**: the file is empty, and CMakeLists.txt / Cargo.toml / package.json still
lists it.

## `test-audit-splits-for-the-count` — splitting subagents to pad the count

**Symptom**: four or five scan subagents are carved out by module mechanically when there are not
even many files.

**Correction signal**: one subagent could have carried the whole list, and it got split into
several.

## `test-audit-trades-coverage-for-time` — trading deletion for time

**Symptom**: to press down the fast lane's measured time, tests that are slow but verify real
business are deleted.

**Correction signal**: the to-delete entries hit the compression criterion only, and the quality
column stands empty.

## `test-audit-moves-one-lane-only` — touching the fast lane only, not the other one

**Symptom**: tests are moved out of the fast lane without the slow lane's command / marker /
collection list kept in step; or nothing is actually run once after the move.

**Correction signal**: the case that was moved is in neither lane, or the report has no measured
reading after the move.

## `test-audit-scanner-decides` — the scan result carrying "suggest deleting"

**Symptom**: a scan subagent writes "suggest deleting" or "can be deleted" in its return.

**Correction signal**: the structured return carries an action verdict, while the hit facts of the
two criteria are gone.

## `test-audit-asks-the-human` — pushing the verdict back to the user

**Symptom**: the report says "please tick which ones to delete" or "waiting for your confirmation
before touching anything".

**Correction signal**: a to-be-confirmed question shows up in a report that was meant to run to
the end.
