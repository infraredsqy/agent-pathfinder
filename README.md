# agent-pathfinder

English | [中文](README.zh.md)

`agent-pathfinder` (`pf`) is a command that keeps a project's todo graph, and a pipeline that
hands one node of it at a time to a coding agent.

Ideas go into a DAG: a node is a piece of work, an edge is a dependency. A **cycle** takes the
node that matters most right now, gives it to a fresh agent session, and records what came back.
The session works under the `pf-conduct` skill — prepare, execute, independent review, fix, close
— and hands back one line saying whether it delivered.

Everything the pipeline knows lives in `.pf/`.

## Status

Early, and moving fast. Expect breaking changes, and expect file formats to shift under you.

`pf next --spawn` and `pf dispatch` start real agent sessions that spend real tokens.

## Install

Needs Python 3.10+, git, and PowerShell on Windows. From this checkout:

```pwsh
git clone https://github.com/infraredsqy/agent-pathfinder/
cd agent-pathfinder
pwsh -File install.ps1
```

It links the `pf-*` skills into `~/.agents/skills` and puts `tools/bin` on your `PATH` — dropping
any other checkout's entry, so `pf` resolves here. Re-run it after moving the checkout; it is
idempotent, and its closing self-check includes `pf doctor`.

```pwsh
pf version     # where this checkout lives
pf doctor      # whether this checkout and the current project look right
```

## Use it

In the project you want to work on:

```pwsh
pf init                                        # put .pf/ in place, register the merge driver
pf dag add "billing page" --tags coding
pf dag add "rate-limit the API" --depends-on <uuid>
pf dag list                                    # sorted by urgency × importance
```

Then start a line and let it open the first session:

```pwsh
pf cycle start billing --title "billing" --tags coding
pf next --spawn
```

`pf next` is the one entry point: bare, it drives the line to its next action; `--plan` shows the
same answer without writing anything.

## Skills

Worker sessions are skills, linked into `~/.agents/skills`:

| Skill | What it does |
|---|---|
| `pf-route` | talk a request into a node; edit and read the graph |
| `pf-conduct` | run one node: prepare, execute, independent review, fix, close |
| `pf-instruct` | record a standing instruction that later sessions follow |
| `pf-issue-scan` | find debt in the code that is not in the pool yet |
| `pf-issue-discuss` | go through the pool with the human, batch by batch |
| `pf-issue-repair` | repair pool debt back under its weight limit |

## Development

Each tool's suite runs from `tools/` — nothing is discovered under the `tools/` root itself:

```pwsh
cd tools
python -m unittest discover -s dag/tests -t .
python -m unittest discover -s issue/tests -t .
python -m unittest discover -s shared/tests -t .
python -m unittest discover -s conduct/tests -t .
```

The contract docs live in `docs/` (Chinese for now).

## License

[MIT](LICENSE)