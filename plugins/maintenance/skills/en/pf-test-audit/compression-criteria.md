# Compression criteria

> For a scan subagent to use, read together with `garbage-patterns.md`. The criterion looks at one
> thing only: **how much this one adds to the fast lane's measured time**. Whether a test verifies
> the right thing is `garbage-patterns.md`'s business.

## What to judge

A test counts as slowing the fast lane as long as it satisfies any one of these:

1. **It is in the fast lane and it is expensive**: it starts a real subprocess, reads something
   outside the real filesystem, waits on real time, loads a model, opens a port, runs over the
   network.
2. **It is in the fast lane and it falls under the project's own slow-lane criterion** (the
   project docs usually write the home judgement: a real service subprocess / waiting on real
   measured time / injecting a slow model / a single one over its measured threshold).
3. **The project has no split**, and it clearly drags the whole suite's measured time longer in
   the full run.

The criterion follows **the project docs**. When the project has set a line itself (say "the fast
lane ≤10 seconds per round"), use its line; when it has not, report the facts (how long this one
takes, what share of the suite it is).

## How a splittable one is split

One test may mix two kinds of assertion:

- **Pure-logic / pure-structure assertions** — milliseconds, resting on no subprocess, clock or
  model. These **get pulled out into the fast lane**.
- **The part depending on a real environment** — stays in the slow lane.

A fast lane test that was pulled out has to **stand on its own**: resting on no state left behind
by the slow lane, leaning on no shared fixture's side effects, and able to run by itself in the
fast lane once split.

> Splitting is a better action than moving the whole thing: it satisfies compression and coverage
> at once. So when scanning, first see whether it can be split, and move it whole only when it
> cannot.

## The two ways to move a test

Which one to pick follows the project:

- **A marker** (when there is a marker mechanism, like `slow`): put the marker on the slow one or
  the slow file, and the fast lane command filters it out itself.
- **An entry** (when the project has made fast and slow into two commands / two directories, like
  `tests/` and `bench`): move the entry to the slow entry, and keep the build / collection list in
  step.

**When the project has no fast / slow split, build one yourself.** The few that cannot be pressed
down (starting a real subprocess, waiting on real time, loading a model, opening a port) get split
into a slow lane of their own: give it an entry or a marker, move those few over, and change the
build and collection lists in step. Once it is built, run each lane once and report the measured
readings.

## What not to do

- **Trading deletion for time is not allowed.** For a test that is slow but verifies real
  business, the right action is to move or split it; deleting it = deleting coverage.
- **Moving a slow lane's assertions into the fast lane "to pad the count" is not allowed** — only
  millisecond-level pure-logic assertions deserve the fast lane.
- **Skipping the verification to make the numbers look good is not allowed**: the changed fast
  lane has to be really run once, and the actual measured time reported.

## The return format

Merged into one with `garbage-patterns.md`'s return format, and every finding carries these two
columns:

```json
{
  "file": "tests/test_example.mjs",
  "findings": [
    {
      "test_name": "the shell's one job: listen on 127.0.0.1 only",
      "quality": { "type": "hardcoded-count", "line_start": 15, "line_end": 22, "judgment": "entire" },
      "compression": {
        "slows_fast_lane": true,
        "reason": "starts a real service subprocess + waits on real time",
        "splittable": [
          { "what": "the port-parsing function's pure-logic assertions", "lines": "31-38" }
        ],
        "action": "move"
      }
    }
  ]
}
```

- `quality.judgment`: `entire` (the whole stretch is garbage assertions) / `partial` (mixed, only
  the garbage lines get deleted) / `none` (it verifies something real)
- `compression.action`: `move` (move it out of the fast lane) / `split` (splittable, with the
  pulled-out fast assertions in `splittable`) / `none`
- **Whether to delete is the conductor's to judge by the action table**: only when
  `quality.judgment` is `entire` or `partial` **and** the coverage gate can be passed does
  deletion come up. You report the facts only, not a "suggest deleting".
