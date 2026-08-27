"""`CalculationKey` — the identity Chemclaw3 addresses a stored calculation by, derived here.

**This is the one piece of Chemclaw3 this server duplicates on purpose, and the duplication is the
whole point of the port.** Read this before changing anything in it.

## Why the key is derived here and not there

Chemclaw3 keeps the calculation cache and the calibration ledger; this server computes. The key
that addresses a row in either is built from things only this process can see:

- `calc_version` is assembled from `xtb --version` (a subprocess), the installed `tblite` and
  `rdkit` distribution versions, a Hamiltonian-revision constant, and — for pKa — seven calibration
  settings. After the split, a Chemclaw3 pod has none of those installed.
- `input_hash` is `stable_hash` over a `structure_id`, which is a hash of the 3D coordinates RDKit
  embedded *here*, at this RDKit version, from this seed.

A client re-deriving either locally would not fail loudly. `xtb_cli.binary_version()` returns the
literal string `"absent"` when the binary is missing rather than raising, so a Chemclaw3 pod with
no xtb would compute a *valid-looking* `calc_version` matching zero ledger rows, and
`calculator_trust("pka")` would confidently report `UNCALIBRATED`. So every compute result this
server returns carries `calc_version`, and every result whose source derived a key carries the flat
`calc_type@calc_version:input_hash:params_hash` string beside it. Chemclaw3 re-derives neither.

## The duplication, stated exactly

Two things in this file are copies of Chemclaw3's `chemclaw/science/calc/store.py`:

1. `CalculationKey.build` / `as_str` — the field *names*, the two `stable_hash` calls, the
   `{"epoch": ..., "params": ...}` envelope, and the `@`/`:` separators of the flat form. **This
   is the half that must agree.** `connectors/calc/remote.py::remote_key` rebuilds a key out of
   `key["calc_type"]`, `key["calc_version"]`, `key["input_hash"]` and `key["params_hash"]` by name,
   so a renamed field is a rename with no local consequence here and a `CalcToolError` on every
   calculation there.
2. `CALCULATION_EPOCH` — the version of *ChemClaw's own* contribution to a stored result, folded
   into `params_hash` by `build()`. **This half does not have to agree, and saying it did was
   wrong.** The claim rested on Chemclaw3 still building keys for in-tree calculators; it has none
   — `CalculationKey.build` has no caller left in its `src/`, and `cached_compute` has exactly one,
   `remote.py::cached_remote`. `remote_key` folds *its* epoch over **ours**
   (`stable_hash({"epoch": <theirs>, "remote_params": <ours>})`), so the two **compose**: a bump on
   either side alone changes the composed digest and misses every stored row, which is what an
   epoch is for. Move them together as a convention — it keeps the two epoch logs describing the
   same events — not because a divergence would be silent.

`tests/test_key_contract.py` pins the first as literal strings taken from Chemclaw3, which is the
only mechanism available: neither repository may import the other.

**What is deliberately absent**: `ResultStore`, `StoredResult`, `cached_compute`, `run_cached` and
every backend. This server has no store. A key is emitted as provenance, never looked up.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from chemclaw_mcp_calc.engine.ids import stable_hash

__all__ = ["CALCULATION_EPOCH", "CalculationKey", "Keyed"]

# The version of **ChemClaw's own** contribution to a stored result — the half no `calc_version`
# covers, folded into every key by `CalculationKey.build`.
#
# Every calculator's `calc_version` answers one question: *would the program we shell out to
# produce a different number now?* It is built from a tblite build, an xtb binary version, an RDKit
# version. Two things change what a stored row *means* that no such version can see: our own
# arithmetic being wrong and then fixed (a linear rotor's rotational partition function was divided
# by `2 * symmetry` instead of `symmetry`, so every N2/CO/CO2/HCN/alkyne entropy already on disk was
# wrong), and a persisted payload's *shape* changing under a stable version.
#
# **The rows it partitions live over there, so a bump here is still Chemclaw3's decision to take.**
# If the arithmetic in `xtb_thermo` here is fixed in a way that makes an already-written Chemclaw3
# row wrong, the epoch is bumped in both repositories in the same change — by convention, so the two
# logs below describe the same events. It is a convention rather than an invariant: `remote_key`
# folds Chemclaw3's epoch over this one's `params_hash`, so bumping either alone already invalidates
# every stored row. What a unilateral bump costs is CPU, not correctness.
#
#   1 — introduced.
#   2 — the per-atom reactivity panel. `SiteReactivityResult` gained the conceptual-DFT global
#       descriptors and four local ones per site, and `AtomCharge` gained its Wiberg and free
#       valence. No number that was already stored moved — the same three SCFs run, on the same
#       geometry — but every row written under epoch 1 is now *incomplete*, and an incomplete row
#       is exactly what this constant exists to invalidate: the new fields are required, so a
#       pre-change row cannot come back claiming a panel it never carried.
CALCULATION_EPOCH = "2"


class CalculationKey(BaseModel):
    """Content-addressed identity of a calculation, versioned by the calculator.

    Two calculations share a key iff they are the same calculator *version* run on the same input
    with the same parameters, under the same `CALCULATION_EPOCH`. `calc_version` is what prevents a
    method update from returning a pre-update cached result; the epoch is what prevents a
    ChemClaw-side fix or payload change from doing the same, and it is why `build` is the only
    honest way to make a key.

    **`calc_version` names every program whose output survives into the payload, and no program
    that does not run** — a calculation that composes two programs names both, because either one
    moving changes the number.
    """

    calc_type: str
    calc_version: str
    input_hash: str
    params_hash: str

    @classmethod
    def build(
        cls,
        calc_type: str,
        calc_version: str,
        inputs: Any,
        params: Any = None,
    ) -> CalculationKey:
        """Construct a key by hashing the inputs and parameters.

        The single place a key is assembled, which is why `CALCULATION_EPOCH` is folded in here: no
        calculator can be keyed without it, and none has to remember to ask.
        """
        return cls(
            calc_type=calc_type,
            calc_version=calc_version,
            input_hash=stable_hash(inputs),
            params_hash=stable_hash({"epoch": CALCULATION_EPOCH, "params": params}),
        )

    def as_str(self) -> str:
        """Flat string form for use as a storage/index key — what a result carries to Chemclaw3."""
        return f"{self.calc_type}@{self.calc_version}:{self.input_hash}:{self.params_hash}"


class Keyed(BaseModel):
    """The two provenance fields every compute result on this server carries.

    A base class rather than two fields repeated nine times, so a new result model cannot ship
    without them and `tests/test_calc_version.py` has one property to assert over every tool.

    `calc_version` is **required and non-empty** by declaration. That is the invariant: a result
    that reached a caller without it would invite the caller to derive one, and the client-side
    derivation is precisely the silent failure this port exists to prevent — `xtb_cli
    .binary_version()` answers `"absent"` instead of raising, so a locally-built string is
    well-formed, matches zero rows in Chemclaw3's `predictions` table, and turns
    `calculator_trust("pka")` into a confident `UNCALIBRATED`.

    `calc_key` is the flat `calc_type@calc_version:input_hash:params_hash` form, and it is `None`
    only where the ported source derives no key. Exactly one calculator is in that position —
    `logd`, which composes a cached pKa with an uncached Crippen descriptor and was never keyed as a
    calculation of its own. Everything else carries one.
    """

    calc_version: str = Field(min_length=1)
    calc_key: str | None = None
