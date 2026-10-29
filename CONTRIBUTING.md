# Contributing

Pull requests are welcome. Only the maintainer writes to this repository directly, so a change
lands by review, and an answer may take a while.

## What a patch has to clear

- **No session start in a test.** `pf next --spawn` and `pf dispatch` open real sessions and spend
  real tokens, so a case stands on the seams instead. `cases_suite.py`'s
  `TestNoRealDispatchInTheSuite` walks the cases for a spawn switch, and `harness_sandbox.py` binds
  that flag in one place.
- **A `.ps1` carrying any non-ASCII carries the UTF-8 BOM.** Windows PowerShell 5.1 reads a
  BOM-less file in the machine's codepage, which mangles those bytes and can swallow the ASCII
  after them. `cases_suite.py`'s `TestShippedPowerShellCarriesItsBom` reds on a script that has
  non-ASCII and no BOM; one that stays pure ASCII needs no BOM.

The README's Development section runs both.

## Commits

`<type>: <description>` -- English, present tense, lowercase, no period. The types in use: `feat`,
`fix`, `docs`, `refactor`, `style`, `test`, `chore`, `perf`, `build`.

## The contract docs

`docs/` carries the rules these tools run under. It is Chinese for now.