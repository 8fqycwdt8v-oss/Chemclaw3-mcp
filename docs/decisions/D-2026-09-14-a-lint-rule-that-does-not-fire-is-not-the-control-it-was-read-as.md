# D-2026-09-14-a-lint-rule-that-does-not-fire-is-not-the-control-it-was-read-as — A lint rule that does not fire is not the control it was read as

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** fix pass over W26/W27/W29, on top of
`f3f3c9c`. Revisits `D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for`,
whose substantive finding stands.

## Context

That record selected `BLE`, and both `CLAUDE.md` and `pyproject.toml` wrote down what it bought:

> `BLE001` lands on exactly those lines, so a new blind handler is red until somebody writes the
> reason at the site.

### Measured

`uv run ruff check --isolated --select BLE` over a probe holding five handler shapes:

| handler | `BLE001` |
| --- | --- |
| `except Exception as exc: logger.warning(...)` | flagged |
| `except Exception: return None` | flagged |
| `except Exception as exc: logger.exception(...); return None` | **not flagged** |
| `except Exception: raise` | not flagged (correctly — it does not answer) |
| `except Exception: if cond: raise` | **not flagged** |

The third shape is the one that matters, because it is a handler that **answers anyway** and it is
what this fleet actually writes. Two shipped handlers are exactly it and carry no `noqa` and no
reason on the line:

- `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/engine/naming.py:166`
- `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/engine/mapping.py:245`

Both classify through `mcp_server_kit.degradation` today, so nothing is broken. **What does not
exist is the control**: anybody writing `except Exception: logger.exception(...); return None`
without classifying gets a green lint and no prompt for a justification — precisely the class the
previous record says it closed.

### And the stated remedy is unavailable on those lines

`RUF` is selected, so `RUF100` rejects a `# noqa` ruff did not need. Driven on both unflagged
shapes:

```
RUF100 [*] Unused `noqa` directive (unused: `BLE001`)
```

So "write the reason at the site" is not merely unenforced for those handlers — it is *rejected*.
That closes the obvious repair (blanket-noqa the two) and decides the shape of the real one.

## Decision

**Extend the control; do not narrow the claim to what ruff enforces.** The claim is about a property
of this fleet's answers, and a property that holds for two handlers because their authors happened
to be careful is not a property.

`tests/test_fleet.py::test_every_blind_handler_that_answers_anyway_is_argued` AST-scans every
`packages/*/src` and `servers/*/src` for a blind `except` whose last statement is not a `raise`, and
requires one of three things, each meaning a reason exists where a reader will find it:

- it calls `classify` or `record` — `mcp_server_kit.degradation`, the counter the claim is about;
- it carries `# noqa: BLE001`, which means ruff *did* flag it and the convention put the reason on
  that line;
- it is named in `BLIND_ANSWER_IS_ARGUED` with the argument beside it.

Ruff stays selected. It is the faster half, it catches the commonest shape, and it fires while
somebody is typing; the scan is the half it cannot reach. Stating that as a pair is the correction —
the previous framing had one rule doing a job neither half does alone.

### The one allowlist entry

`packages/mcp_server_kit/src/mcp_server_kit/auth.py:432`. The body-cap middleware discards the
downstream app's exception **only when it has already refused the request** — the app raised because
this middleware cut its receive channel, which is this code's own doing rather than a component
going missing. A `record()` there would publish a degradation every time a caller sent an oversized
body, which is the opposite of what that series means.

`test_the_argued_blind_handlers_are_still_there` holds the list in the other direction, so a moved
handler cannot leave an argument about nothing behind for the next one to inherit.

## What keeps it true

- `tests/test_fleet.py::test_every_blind_handler_that_answers_anyway_is_argued`. Driven twice:
  appending `except Exception: logger.exception(...); return None` to `servers/props/.../tools.py`
  gives `ruff check --select BLE` → *All checks passed* and this test → **failed**, in one run; and
  deleting the `degradation.classify`/`record` pair from the real `naming.py:166` handler gives the
  same pair of answers.
- `tests/test_fleet.py::test_the_argued_blind_handlers_are_still_there`. Driven: changing the
  allowlist entry's line number reds both tests.
- `packages/mcp_server_kit/tests/test_degradation.py::test_every_call_site_derives_its_cause_rather_than_writing_one`
  — unchanged, and the complement: this one says every answering handler reaches `degradation`, that
  one says every call site derives its cause rather than writing one.
