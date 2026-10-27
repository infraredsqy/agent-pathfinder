# The pool (the issue store)

> The skills on the pool's own branch share this one: scanning, ruling with the user, and
> clearing the debt. For command detail go by `--help`; this writes the conventions only.

The pool lives in the project's `.pf/issues/` and is one unified to-do list. `source` only says
how the entry came about: `scan` (scanned into the pool), `manual` (written down by the user in
passing), `conduct` (run into by a working session).

## Go through the tool only

```pwsh
pf issue <command> --help    # look at it before touching anything
pf issue status             # the present state
pf issue top --limit 10     # by priority
pf issue search '<regex>'    # locate; search like words in one go with |
pf issue weight -v          # how the weighted sum adds up
```

- **Every** read and write of the pool goes through this tool only. **Reading
  `.pf/issues/issues.json` directly is forbidden**: a giant JSON, it burns context and it bypasses
  the tool's own counting conventions too.

## The conventions

- Status: `open` / `acknowledged` / `resolving` are the three that count as actionable; `resolved`
  / `rejected` do not.
- `decision`: `autofix` (it can be fixed by ourselves) / `external` (the trade-off is still open
  in the user's hands) / `unknown` (not ruled yet, the default on the way into the pool). **An
  entry whose call has already been made, with the solution written inside it, is `autofix`
  without exception** — landing on another node or another line counts too.
- **The weighted sum** = P3/P2/P1/P0 = 1/2/4/8, counting only entries that are actionable and
  whose `decision` is not `external`. `unknown` presses on the line just as `autofix` does; ruled
  `external` and it drops out of the count.
- Severity (judge by this when writing each entry): **P0** duplicated and carrying a bug risk, or
  standing on a business-critical path; **P1** a responsibility crossing its line and holding
  iteration back (business logic inside the UI, the logic layer touching the DOM, a dependency
  across layers); **P2** a structural improvement that raises efficiency (an extractable repeated
  pattern, scattered configuration, a feature that takes 3+ files to change); **P3** tidiness
  (naming, stale comments, dead code, formatting).
- **A large change gets an entry of its own**: a refactor signal of ≥3 files that also changes
  inter-module dependencies gets an entry of its own, and moving to `/engineering` is suggested.
  The pool keeps that piece of debt and its criterion only; the refactor plan is not unfolded
  here.
- **Search for the same entry before pooling**: before a new finding enters the pool, search the
  pool once with like words; dedup and merge on the way in — the same piece of debt in two records
  leaves the reader to recognise its own kin.
