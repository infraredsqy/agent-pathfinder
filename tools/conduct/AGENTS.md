# The conduct tool's development discipline

> This file (AGENTS.md / CLAUDE.md) must not exceed 100 lines.

## Layout

- The rules live in `docs/tool-modules.md`. The conduct root holds four entries (`state-manage.py` /
  `conduct_pipeline.py` / `handoff.py` / `signal.py`) and four machine directories: `state/` (the
  write side: cycle records, standing instructions, settings, init), `pipeline/` (advancing and
  closing nodes out), `spawn/` (the machine chain that opens sessions), `ledger_merge/` (the merge
  driver's body) — plus `features/`, which holds plugins and no core machinery.
- A **feature** is a folder under `features/` with two files in it — its declaration (`feature.json`)
  and its implementation (`impl.py`) — with the folder's name as its identity.
  `pipeline/feature_registry.py` is the core's whole knowledge of them — four knob kinds, three
  timings, two actions, and the rules a declaration has to satisfy — and it is loaded by path from
  both packages (`feature_host.py` asks it for actions, `state/knobs.py` merges the knobs they
  declare), one copy per process. Its `verify()` is the validator over both sides (the declaration's
  cards, and one real call of each implementation against a stand-in project): the suite and
  `pf doctor` are its only callers, never the pipeline's route. A feature's names live in its
  declaration only: adding or removing one is a folder move, and no core file names one. The
  declaration names, per metric, the method the generated node's session reads; that card lives in the
  same folder, under `cards/<lang>/<method>/`, and `install.ps1` links it where the method reads it.
- `ledger-merge.py` must stay at the conduct root: `pf`'s `merge-ledger` forwarding finds it by
  this relative path. A project registers through `pf register`, which records
  `pf merge-ledger %O %A %B %P` — independent of where the checkout lives, so moving it needs no
  re-registration; when `pf` is not on PATH the absolute form is recorded first, and once PATH is
  fixed, one `pf register` re-run swaps it back.
- Entries reference each other by HERE-relative paths: moving a file moves the cluster and syncs
  these references; afterwards run the conduct suite (see `README.md`, Development).

## How tests are written

- One temp project per case, run in parallel across the CPU cores — these cases spend nearly all
  their time waiting on subprocesses.
- **Call in-process whenever you can**: load `conduct_pipeline.py` by path with importlib and call
  `apply()` / `record()` directly; only a case whose subject is the CLI itself really invokes
  `state-manage.py`; the DAG and the `state.json` skeleton are built by writing files.
- The red line is judged on a single case (≤5 s); the full suite's measured time is still reported
  as usual.

## The dispatch path is live ammunition

- This tool tree's `--spawn` / `dispatch` goes through `conduct_spawn.py` → a spawner script
  (`dsh-spawn.ps1` on DSH, `zcode-spawn.ps1` on ZCode) and opens **real sessions** that burn
  tokens; the ZCode one also writes rows into the user's installed
  `~/.zcode/v2/tasks-index.sqlite`. Tests, debugging and sample runs never go through it.
- To verify advancing logic, stop at "the action came back and what it wrote landed on disk" — a
  dispatch's prompt, an insertion's spec. To assert dispatch semantics, use a fake spawner
  (precedent: `TestOneSpawner` in `tests/cases_cli_entries.py`).
- A mechanism change is always verified in a **one-shot sandbox project**: copy the graph's records
  into a git repo under temp (or build a fake graph outright), driven by a fake spawner; on a real
  project only read-only queries are allowed (`cycle list` / `cycle current` / `--plan`) — no
  opening a cycle, no `dispatch` / `--spawn`.
- New tests must not splice `--spawn` into a command string; temp projects use a `tempfile`
  sandbox, cleaned up afterwards. On the ZCode side, point `ZCODE_DATA_BASE_DIR` at a temp
  directory (precedent: `tests/cases_zcode.py`).
