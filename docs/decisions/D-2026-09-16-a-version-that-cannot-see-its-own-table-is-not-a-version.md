# D-2026-09-16-a-version-that-cannot-see-its-own-table-is-not-a-version — A version that cannot see its own table is not a version

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the `thermalsafety` constants identity.
Follows `D-2026-09-16-the-refusals-belong-in-front-of-the-library`, which moved the atomic weights
into `molmass` and left both halves of this server's published identity blind to them.

## What was measured

`servers/thermalsafety/engine/selftest.py` publishes two things on `/healthz`:

- `CONSTANTS_VERSION`, hand-bumped, `"1.1.0"`;
- `_constants_digest()`, `sha256(oxygen_balance.py)`, whose docstring said "two pods claiming the
  same version can be shown to be serving the same table, which a version string alone cannot do".

Since `6c6a0eb` the table is not in that module. `ATOMIC_WEIGHTS` is
`{symbol: ELEMENTS[symbol].mass for symbol in sorted(ALLOWED_ELEMENTS)}` — seventeen bytes of
comprehension standing in for seventeen numbers that live in another distribution. Neither the
hand-bumped string nor the source digest can see a weight change.

And the weights can legitimately differ between two pods of one image generation: `uv.lock` resolves
**two** molmass releases on purpose — `2026.1.8` for `python_full_version < '3.12'` and `2026.8.15`
at or above it. Both were installed and all seventeen weights compared: identical today. So the
defect is **structural rather than observable**, which is exactly the window in which it is cheap.

## What changed, and why both halves rather than either

A version answers "which one is newer" and a digest answers "are these the same". Both were wrong
about the same table, and each fixes a different failure:

- `CONSTANTS_VERSION` is now `f"{_FIRST_PARTY_REVISION}+molmass-{version('molmass')}"`. The
  hand-bumped revision still names what *this repository* writes — the bands, the formulas — and the
  suffix names the distribution the weights come from. This is `engine_version()`'s argument one
  server over, about the same kind of adopted table.
- `_constants_digest()` now hashes the module source **and** the resolved weights, serialised with
  `repr` in sorted key order. That is the question the version cannot answer: whether the numbers
  actually agree — including the case a version string never could reach, a release that changes a
  weight without changing the fields this server reads.

`retrieved_from` stops claiming "IUPAC 2021 conventional atomic weights", which it has not served
since the table moved, and names the distribution in `version` instead.

## What keeps it true

- `servers/thermalsafety/tests/test_oxygen_balance.py::test_the_published_constants_version_and_digest_both_see_the_adopted_table`
  — both directions: the version names the installed distribution, and the digest **moves** when a
  weight moves, driven by substituting one rather than by pinning a literal.
- `servers/thermalsafety/tests/test_server.py::test_healthz_names_the_constants_this_pod_verified` — the
  published `name@version` string, so the shape an operator reads stays the shape.
