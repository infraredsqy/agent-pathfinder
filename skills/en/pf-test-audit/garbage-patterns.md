# Garbage Test Patterns

> This file is for a subagent scanning tests; the conductor does not read it.

## The 6 Garbage Test patterns

| # | Pattern | Language-agnostic description | Typical example (Catch2 / Jest / pytest) |
|---|---|---|---|
| P1 | `hardcoded-count` | asserts a concrete count of a collection / methods / entries | `REQUIRE(result.size() == 36)`, `expect(methods.length).toBe(10)` |
| P2 | `trivial-init` | verifies the default initialisation state the language / framework guarantees | `REQUIRE(field == false)`, `assert(field is None)`, `expect(obj.count).toBe(0)` |
| P3 | `field-roundtrip` | assigns a value and reads it straight back to verify | `x = val; REQUIRE(x == val)`, `obj.name = "foo"; assert obj.name == "foo"` |
| P4 | `enum-bound` | binds an enum / constant to a concrete integer value | `REQUIRE(static_cast<int>(Enum::A) == 0)`, `assert(Color.RED == 1)` |
| P5 | `container-op` | tests a container's basic add / remove / look-up operations | `map.erase(k); REQUIRE(map.size() == 0)`, `arr.append(x); assert len(arr) == 1` |
| P6 | `trivial-error` | tests only that a bad argument returns a fixed error value | `REQUIRE(func(bad) == -1)`, `assert(parse("").is_err())` |

## The judgement principles

- If every assertion in a whole TEST_CASE (or a container of the same kind) falls under the
  patterns above → **delete the whole stretch** (judgment: `entire`)
- If some assertions fall under the patterns above and some verify business logic → **delete the
  garbage assertion lines only**, keeping the valid part (judgment: `partial`)
- If every test inside a file is deleted → **remove that file from the build system
  (CMakeLists.txt / Cargo.toml / package.json and the like)**

## The coverage gate for deletion

Hitting a garbage pattern is only half. **Before deleting, confirm that the piece of business it
verifies is covered elsewhere too**.

- Both hold → delete
- It hits a garbage pattern, but it is the **only** coverage of that piece of business → keep it,
  and write this into the report (this one looks bad, yet it is the only sentinel)
- When you cannot tell whether there is coverage elsewhere → name that piece of business in the
  report for the conductor to check; **keep it rather than not**

Really go and search, do not go by impression: the neighbouring cases in the same file, the tests
of the same kind of module, the indirect coverage inside end-to-end cases.

## The safety exceptions

The following are **not** garbage tests, so do not delete them by mistake:
- verifying that some field is explicitly initialised to a non-zero / non-empty value (the
  business asks for it and it differs from the default)
- verifying boundary-condition behaviour (an empty-collection query does not crash, a negative
  index gets clamped)
- verifying a serialisation roundtrip (write it and read it back to check the fields agree)
- verifying that a state machine's transition is legal

## The return format

```json
{
  "file": "tests/app/test_example.cpp",
  "findings": [
    {
      "test_name": "Enum values are correct",
      "type": "enum-bound",
      "line_start": 15,
      "line_end": 22,
      "judgment": "entire"
    }
  ]
}
```

- `judgment`: `entire` = the whole test is garbage assertions; `partial` = mixed with useful
  assertions, and only the garbage lines get deleted
