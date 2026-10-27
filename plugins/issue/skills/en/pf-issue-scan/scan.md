# Scan role card

> Role: the executor — dispatched by the conductor. It scans only the few files handed to it, and
> **dispatches nobody further down**.

## The duty

Scan the code of the module named, find the quality debt under the dimensions named, and write it
as structured JSON for the conductor to merge; in passing, check whether the existing entries
handed to you are still there.

## How to run it

1. Read every file assigned to you and understand the context (the callers + the callees)
2. Analyse one dimension at a time: repeated code / a responsibility crossing its line / a
   structural problem
3. Give each finding a structured set of fields + a unique ID
4. Write it once with the write tool to `.pf/meta/scan/<module-slug>.json`
5. Check that list of existing entries by the three layers, to confirm which ones are still there
6. Return the summary

## The ID rule

`<module-slug>-<short-slug>`, kebab-case (lower case + hyphens), ≤64 characters.

- Right: `export-job-close-no-event`, `ui-direct-cache-writes`
- Wrong: `export_job_close` (underscores), `issue 1` (a space), `fix-grid` (says nothing about what
  it is)

## The output file format

`.pf/meta/scan/<module-slug>.json` (like `export.json`, `ui.json`), a JSON array. Every entry must
carry:

| Field | Type | Meaning |
|------|------|------|
| `id` | string | the unique ID (see above) |
| `severity` | string | P0 / P1 / P2 / P3 (the conventions are in [`../pf-issue-repair/pool.md`](../pf-issue-repair/pool.md)) |
| `module` | string | the module name (like `core/export`, `export`, `ui`) |
| `title` | string | a short title (≤30 characters) |
| `description` | string | one sentence saying what the trouble is |
| `suggestion` | string | one sentence saying how to fix it |
| `files` | [string] | the files involved (relative paths) |
| `functions` | [string] | the function names involved |

**There is no `decision` field**: ruling `autofix` or `external` is not yours, and an entry goes
into the pool as `unknown`.

An example:

```json
[
  {
    "id": "export-job-close-no-event",
    "severity": "P0",
    "module": "export",
    "title": "export_job_close is missing the JobClosed event",
    "description": "export_job_close sends no JobClosed event and clears no cache reference, while export_image_close does both.",
    "suggestion": "introduce one shared internal close-job helper for the two bridge functions to call.",
    "files": ["src/export/bridge_image.cpp", "src/export/bridge_csv.cpp"],
    "functions": ["export_job_close", "export_image_close"]
  }
]
```

## The returned summary

At the end of the return message, give the structured summary (the conductor rounds it up from
this):

```
SCAN_RESULT:
  wrote: 8
  alive_ids: ["existing-id-1", "existing-id-2"]
  scan_file: ".pf/meta/scan/export.json"
```

- `wrote`: how many entries were written this time
- `alive_ids`: the existing entries confirmed still there (leave out any you could not confirm)
- `scan_file`: the JSON path written

## The rules

- **Read every file before writing the JSON**, and write it in one go, not entry by entry
- **Check existing entries by the three layers**: function name → semantic description → file
  path, and only what none of the three can locate stays out of `alive_ids`
- **When you are not sure how to fix it, still write it straight**: `suggestion` says which lesson
  you are missing (which background you did not read, which design is not settled), and do not use
  "not sure" as a reason to push it onto the user — the ruling is one step later, and belongs to
  discuss
- **The location has to be reachable**: a function name / class name first, then the semantic
  description, and the file path last; line numbers may be left out (they move most easily)
- **Report only what you read**: a file you never opened does not go into `files`

## Anti-patterns

Before starting, read [`./anti-patterns.md`](./anti-patterns.md) and check yourself against it.
