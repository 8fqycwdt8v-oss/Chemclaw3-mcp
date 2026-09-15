# D-2026-09-15-five-copies-varied-the-message-not-the-mechanism — five copies varied the message, not the mechanism

**Status:** accepted · **Date:** 2026-09-15 · **Commit:** `Admission` moves into
`mcp_server_kit.limits`. Closes the `docs/BACKLOG.md` row that asked whether the counter and the
clamp belong there "with each server keeping its own message, or whether five copies is the right
price for five independent dependency closures".

## What was measured before anything moved

The row described five near-identical copies. Driven rather than read, with every string literal
erased from the AST so only the mechanism compared, the five reduce to **three** distinct bodies:

| servers | statements | mechanism |
| --- | --- | --- |
| `chem`, `pyexec` | 8 | byte-identical to each other |
| `rxnlabel`, `rxnpredict` | 10 | byte-identical to each other |
| `calc` | 10 | differs from the pair above in **one expression** |

That one expression is the exception it raises: `AtCapacityError` rather than `ValueError`. And the
8-versus-10 split is not a difference either, because it is the presence of a `cost` parameter whose
default is 1 — `take()` and `take(cost=1)` are the same call.

So what five copies actually varied was **the error type and the message**. Everything else — the
lock, the counter, the clamp into `1..limit`, the floor at zero on release — was the same algorithm
written out five times.

## Why the kit, and why it does not raise

The row noted that "one server never imports another" is true and does not apply to
`mcp_server_kit`, which all five already import. What settles *how* is that `limits.py` had already
solved this exact shape for input bounds, and said so in its own docstring:

> Neither function raises: they return a *worded reason* or `None`. A server that refuses (a chemist
> is waiting) raises its own `ValueError` subclass with the reason.

`Admission` follows that rule rather than inventing one. `take()` returns `Slots(charged, free)` and
never raises; each server keeps its `acquire` and its sentence. That matters because the sentences
are not decoration — `chem`'s names a replica because raising its ceiling cannot help, `calc`'s
leads with `AT_CAPACITY_MARKER` because Chemclaw3 matches on it, `rxnlabel`'s tells a drain to
re-send the identical batch.

**`free` is carried out of the lock with the verdict** rather than read afterwards. A refusal
quoting a count sampled later is quoting a number that was never true: between the refusal and the
read, another call can finish, and the message would name a capacity that existed only *after* the
request it is explaining was turned away.

**The construction refusal is one noun**, so it is a class attribute (`unit`) rather than an
`__init__` override. "would refuse every depiction" and "would refuse every batch" are the
operator's own vocabulary; each server's exact wording is preserved.

## What this is not: a size win

Net **−24 lines** across six files. That is worth stating because it is the honest case against
doing this, and the case for is different: the clamp and the floor-at-zero are the subtle half, and
five copies of a subtle invariant is five places for it to be got wrong independently. One
definition is the point; the line count is a rounding error.

Nor was there a drift incident to cite. The copies had *not* diverged in mechanism — the measurement
above is what establishes that, and it is also what makes the extraction safe, since there was no
behaviour to reconcile.

## What moved and what did not

Moved to `mcp_server_kit.limits.Admission`: the lock, `limit`, `in_flight`, `take` (with the clamp)
and `release` (with the floor). Stayed with each server: the `acquire` verb its call sites use, the
exception type, the refusal message, the `unit` noun, `ADMISSION_MARKER`, and every default and
cost model.

`calc` keeps `AtCapacityError` and `AT_CAPACITY_MARKER` unchanged, including the marker's position
at the head of the message — Chemclaw3 transcribes that literal independently, and this commit does
not touch either side of that contract.

## What keeps it true

- `packages/mcp_server_kit/tests/test_limits.py::test_a_cost_above_the_ceiling_takes_the_pod_exclusively_rather_than_being_unadmittable`
  and `test_a_cost_of_zero_is_charged_one_so_nothing_runs_uncounted` — both halves of the clamp,
  which is the invariant that was written five times.
- `packages/mcp_server_kit/tests/test_limits.py::test_a_double_release_cannot_open_the_gate` — the
  floor at zero, driven by releasing more than was taken.
- `packages/mcp_server_kit/tests/test_limits.py::test_the_free_count_is_taken_under_the_lock_with_the_decision`
  — `free` is the count at the instant of refusal, not a later sample.
- `packages/mcp_server_kit/tests/test_limits.py::test_nothing_ever_waits` — a full budget refuses on
  another thread in under 100 ms rather than blocking, which is why this is a lock and not a
  semaphore.
- `packages/mcp_server_kit/tests/test_limits.py::test_concurrent_takers_never_exceed_the_ceiling` —
  twenty threads racing for four slots are granted exactly four.
- Each server's own `test_admission.py` still passes unchanged, which is what shows the refusals and
  the construction messages survived the move.
