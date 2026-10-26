# Subagents: how many to dispatch at a time

> The value is not here. Before dispatching work, run `pf config get max_agents` to read it: the
> global copy is in `~/.pf/config.json`, the project copy in `<project>/.pf/config.json`, and the
> factory default is written in the tool. `pf config list` prints that number together with the
> layer it came from. This file writes only how to use it.

**It is a number the pipeline estimates itself**: it consults no host and recognizes no particular
substrate software. The pipeline has no resident window, so it does not check in between sessions
either — open one subagent and write the file once, close it and write once more, and that shape
does not work. Every session estimates with this number by itself.

## How to use it

Before dispatching work, count what is in flight once: in flight + what this dispatch is adding ≤
the cap. When it cannot be crossed, yield in this order:

1. **Merge**: one subagent carries several things. The surfaces are cut into copies as usual (each
   copy one finding, each with its own three layers), and on dispatch several copies go into the
   same prompt.
2. **Batch**: when merging still cannot cross it, wait for this batch's notice to come back before
   dispatching the next.

When merging, every copy still has to stand as its own piece: one piece per report, not mashed
into one paragraph, or the merge into the pool has nothing to dedup on.

## Why leave headroom

When the cap is hit, that batch comes down together and the whole batch is lost. Another session
open at the same time is holding a slot too, so estimate on the conservative side rather than
running right at the ceiling.
