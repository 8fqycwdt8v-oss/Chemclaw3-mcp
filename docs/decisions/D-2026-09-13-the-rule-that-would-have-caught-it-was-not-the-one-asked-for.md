# D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for — The rule that would have caught it was not the one asked for

**Status:** accepted · **Date:** 2026-09-13 · **Commit:** wave W27, on top of `0fb12537`.
No hash is written for this pass, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

The fleet selected `E, F, I, UP, B, SIM, RUF` and had **no coverage measurement anywhere**. The
wave row asked for `S` (flake8-bandit) and `ASYNC` (flake8-async), predicting that "`S` should
mechanically surface" the raw-text cache fallback
(`D-2026-09-13-a-cache-key-derived-from-text-nobody-validated-is-not-a-key`).

**It does not, and that is the first measurement.** Run against the defective file exactly as it
stood on `main`:

```
$ ruff check --isolated --select S,ASYNC old_cache.py
All checks passed!

$ ruff check --isolated --select BLE old_cache.py
old_cache.py:48:12: BLE001 Do not catch blind exception: `Exception`
old_cache.py:56:12: BLE001 Do not catch blind exception: `Exception`
```

Lines 48 and 56 are the two the row itself names. The rule that finds that defect is
**`BLE` (flake8-blind-except)**, which nobody asked for, and `S` finds nothing there at all — `S110`
is `try`/`except`/`pass`, and that handler returns a value.

### What each selector actually found

| selector | findings | what they are |
| --- | --- | --- |
| `S101` | 2,354 | `assert` — every one in a test, `conftest.py`, or the two `src/` helpers whose product is an assertion |
| `S105`/`S106` | 32 | variables named `TOKEN`, `TOKEN_ENV`, `SECRET`, and the `token_env=` kwarg |
| `S603`/`S607`/`S606` | 15 | every `subprocess` call in `calc`, `pyexec`, `scripts/` and the fleet tests |
| `S104` | 3 | the `0.0.0.0` literals `test_egress.py` asserts the guard **refuses** |
| `S102` | 1 | `pyexec`'s `exec`, which is that server's entire product |
| `S110` | 1 | `logging.py`'s redaction filter, already argued in place |
| `ASYNC210` | 5 | sync `httpx` inside `async def`, all driving a uvicorn in another thread |
| `BLE001` | 22 | every `except Exception` in this fleet that answers anyway |

## Decision

Select **`S`, `ASYNC` and `BLE`**, ignore `S101` fleet-wide, and set a coverage floor of **88**.

### `BLE` is the one that pays, and it makes a `CLAUDE.md` claim checkable

`CLAUDE.md` says in the present tense that "every path in this fleet that catches an exception and
answers anyway now classifies it through `mcp_server_kit/degradation.py`". Nothing checked it and
one path did not. `BLE001` lands on exactly the 22 lines that claim is about, so each now carries a
`# noqa: BLE001` and a reason at the site — which is where a reader of the handler looks, rather
than in a document beside it — and a new blind handler is red until somebody writes one.

Eleven of the 22 are one shape (a predictor module's import guard funnelling into
`mark_unavailable`) and get the same four-line reason; the rest are argued individually.

### `S101` is off, and the reason is that something stricter already holds it

`tests/test_fleet.py::test_no_serving_module_enforces_an_invariant_with_assert` scans every
`packages/*/src` and `servers/*/src` and carries the argument for its two exemptions
(`mcp_server_kit`'s `testing.py` and `no_egress.py`, whose *product* is an assertion failure).
Turning `S101` on means transcribing that exemption list into a `per-file-ignores` block — a second
declaration of one rule with nothing reconciling the two, which is the defect this repository
deleted a port table over. The lint rule is also the weaker of the pair: it cannot tell a serving
module from a test helper that lives under `src/`.

### `S` triage: per-file for tests, `# noqa` with a reason everywhere else

`**/tests/**` ignores `S104`, `S105`, `S106`, `S603`, `S607` — a bearer-auth test's fixture
credential is the subject, not a leak, and the `0.0.0.0` literals are what the egress test asserts
is refused. `scripts/*.py` ignores the subprocess rules; `offline_check.py` exists to start one in a
stripped network namespace.

Everything in serving code got a per-site `# noqa` with the reason, not a blanket ignore, so the
next literal that *is* a secret has to be argued too. Seven of those are `token_env=`, which holds
an environment variable's **name** — a naming collision with this fleet's own vocabulary rather
than a finding. Three are `subprocess` calls whose argv is built from a configured binary path and
a private tempdir, with no caller string in it. One is `exec`, which is `pyexec`'s capability.

**Nothing `S` found was a defect.** That is a result rather than a disappointment: the rule that
found one was `BLE`, and it is in the select list because it did.

### `ASYNC210`: five real blocking calls whose harm is absent for a structural reason

Each drives a server that `test_server.py` runs under **uvicorn in its own thread with its own
event loop**, so blocking the caller's loop cannot stall the thing being probed. An in-process ASGI
transport would make the same line a deadlock, which is why the exemption is a per-site `noqa`
saying so rather than a per-file ignore that would cover a future test that did.

### The coverage floor is 88, measured

Measured over the whole suite: **7,220 statements, 811 uncovered, 88.77%**. 88 is the largest
integer below it, which gives roughly 0.8 points of headroom — one commit's churn, not a licence to
drop. It lives in `[tool.coverage.report]` with what it does **not** cover written beside it:

- the eleven optional predictor adapters in `rxnpredict` measure **40-57%**, because their extras
  are not installed in this workspace and each module's body stops at its import guard — and two of
  them are what the *shipped image* installs, so the least-covered code here is code that runs in
  production;
- `pyexec`'s `engine/runner.py` measures **22%** because it runs in a subprocess this process cannot
  trace; its behaviour is covered by `test_sandbox.py` driving the real jail, and none of that lands
  in the number;
- it measures the source tree, never an image.

`make cov` runs the suite with coverage and `make check` calls it **instead of** `test`, rather than
in addition: measured at 176 s against 210 s for the bare run, so the floor costs nothing and paying
for the suite twice would cost everything. `test` stays for a fast bare run while iterating.

## What keeps it true

- `pyproject.toml`'s `select` and `ignore` are the declaration; `make lint` is what runs it, and
  `.github/workflows/ci.yml`'s `check` job now calls `make lint` rather than spelling the two ruff
  commands inline (`D-2026-09-13-a-gate-in-another-system-is-not-a-gate-this-one-can-see`).
- `tests/test_fleet.py::test_no_serving_module_enforces_an_invariant_with_assert` — the stricter
  half of the `S101` decision, and the reason the lint rule is off rather than configured.
- `packages/mcp_server_kit/tests/test_degradation.py::test_every_call_site_derives_its_cause_rather_than_writing_one`
  — the count of `degradation.record` call sites, which went 7 → 8 in this wave and is what makes a
  `# noqa: BLE001` on a path that does *not* classify visible rather than plausible.
- `[tool.coverage.report] fail_under` — driven both ways: at 88 the suite passes, and raised to 99
  the run fails with "Required test coverage of 99.0% not reached".
