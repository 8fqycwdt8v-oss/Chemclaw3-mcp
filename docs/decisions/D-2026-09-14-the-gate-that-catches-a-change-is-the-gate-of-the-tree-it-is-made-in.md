# D-2026-09-14-the-gate-that-catches-a-change-is-the-gate-of-the-tree-it-is-made-in — The gate that catches a change is the gate of the tree it is made in

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** Wave 30.5, `eb58363`.

## Context

Three bundle manifests (`chem`, `rxnpredict`, `safety`) exist in **both** this fleet and
`Chemclaw3`, declaring the same tool list, the same read-only partition and the same bearer
variable. A fourth fact — `servers/calc/tool-surface.json` — has a manifest in *neither* tree:
`calc` is `mount: backend`, and that repository reaches this fleet's server with the tool names and
argument dicts hardcoded in `connectors/calc/`.

`Chemclaw3` checks both from its side (`tests/test_sibling_manifest_agreement.py`, merged there
under `D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it`). Run against this
real checkout on 2026-09-14 — it resolves `/home/user/Chemclaw3-mcp` with no variable set, because
its `infra/live/siblings.sh` searches `dirname($REPO_ROOT)` first — all three tests pass and
compare: the intersection of the bundle names both trees declare, on `tools`/`read_only`/
`state_changing` as **sets** plus `auth.token_env`; 13 hardcoded call sites in
`connectors/calc/{compose,remote}.py` naming 10 tools against this fleet's recorded surface; and
that repository's fake calc server against the same file.

**Nothing in this tree checked any of it**, and the drift that matters is not the sloppy commit —
it is the correct one. `CLAUDE.md` requires a tool-surface change to be a `connector.yaml` change
in the same commit, and `assert_manifest_matches` holds that against a *running* server plus
`tool-surface.json`. So a rename done **completely** here passes everything this repository runs.

Driven: `ich_impurity_limit` → `ich_impurity_limits` across `servers/safety`'s `tools.py`,
`connector.yaml`, `tool-surface.json`, `README.md`, `MODULES.md` and the server's own four test
modules.

| run | result |
| --- | --- |
| `pytest servers/safety/tests` | **261 passed** |
| `pytest tests` (fleet invariants) | **212 passed** |
| the consumer's agreement suite | fails: *"`safety` declares a different tools in the two repositories"* |

The whole repository was green on a commit that breaks the consumer, and would have stayed green
until somebody else's merge.

## Decision

**This tree runs the consumer's own agreement module rather than reproducing it.**
`tests/test_consumer_agreement.py` invokes `Chemclaw3`'s `tests/test_sibling_manifest_agreement.py`
with that checkout's own interpreter and `CHEMCLAW_MCP_REPO` forced to *this working copy*.

Not because a second copy would be untidy — because the copy would be **the thing under test**.
That module resolves the calc seam's tool names with an AST walker over literals, ternaries and
name bindings; a reimplementation here would agree with itself and drift from the call sites it is
meant to read, which is exactly the failure `tests/test_identity_contract.py` records for the
identity headers — two constants consistent with each other, one wrong about the sender for as long
as it existed. Delegating also means an agreement check the consumer adds next month is inherited
here with no edit.

Three failure modes, chosen so the check cannot go quiet:

- **no checkout** → skip, with the reason, and `conftest.py::pytest_terminal_summary` says at the
  end of the run how many cross-repository checks did not run and that the run is not evidence
  about them. A skip is not a pass.
- **checkout present, the module renamed** → *fail*, not skip. That is the one part of this
  arrangement that could be retired in silence.
- **the consumer's run skips** → fail. This side supplied the checkout, so a skip means the module
  could not read what it was pointed at.

The checkout lookup **mirrors** `Chemclaw3/infra/live/siblings.sh` — two roots, two casings, plus
an environment variable — rather than inventing a third answer to "where is Chemclaw3"; all four
candidates are driven under a temporary root rather than asserted as a tuple of names, which would
pass on a search that consulted the tuple and looked nowhere.

## What this costs, stated rather than implied

The consumer's suite is the authority on what is compared, so a check **deleted** there is silently
deleted here. That is a smaller risk than a divergent copy and it is not zero.

Neither repository's CI clones the other, so in CI this skips on both sides. Queued.

And the seam is wider than the check: running the consumer's own AST walker over
`src/chemclaw/connectors/calc/server/tools.py` — a third caller its `_CALLERS` tuple does not list —
found **11 more hardcoded call sites naming 10 tools, 8 of them named by no checked module**. Every
one served, every argument declared: sound, and watched by nothing. The fix is one line in that
repository; the row here is what keeps it from being forgotten.

## What keeps it true

- `tests/test_consumer_agreement.py::test_the_consumer_still_agrees_with_the_surface_this_tree_declares`
  — the delegation itself. Mutation-checked four ways: the complete `safety` rename (caught), a
  renamed key in `servers/calc/tool-surface.json` (caught, by two of the consumer's three tests), a
  renamed `AGREEMENT_MODULE` (fails rather than skips), and a consumer run that skips (fails).
  Deleting its `returncode == 0` assertion makes it pass on a broken subject, which is what says
  that assertion is the load-bearing one.
- `tests/test_consumer_agreement.py::test_every_place_the_live_lanes_look_for_the_checkout_is_looked_in_here`
  — all four candidate paths plus the variable, driven under `tmp_path`, so the property survives
  on a machine where the check above can only skip.
- `conftest.py::pytest_terminal_summary` reads the skip marker out of the module that produces it,
  by path, so a reporter matching a transcribed phrase cannot fail by reporting nothing.
