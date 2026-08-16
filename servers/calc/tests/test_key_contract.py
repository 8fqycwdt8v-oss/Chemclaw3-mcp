"""The key contract with Chemclaw3, written as literal strings on both sides.

`engine/ids.py` and `engine/key.py` are **copies** of definitions Chemclaw3 owns
(`chemclaw/core/ids.py`, `chemclaw/science/calc/store.py`). Copying is normally how two answers to
one question appear; here it is unavoidable, because neither repository may import the other and the
key can only be derived where `tblite`, `rdkit` and any `xtb` binary are installed.

The copy is not free, and the cost is specific. If either drifts:

- an `input_hash` addresses a `calculation_results` row that does not exist, so every result is a
  cache miss forever — expensive, and *visible*;
- a `calc_version` addresses a `predictions` row that does not exist, so every recorded residual
  becomes unreachable and `calculator_trust` reports `UNCALIBRATED` with n=0 — cheap, and
  **silent**.

So the contract is written as **data**: an input and the exact string it must produce. Every
expected value below was produced by running Chemclaw3's own code on the same package versions, not
by reading this repository's copy and agreeing with it:

    cd /path/to/Chemclaw3 && uv run python -c "
    from chemclaw.core.ids import stable_hash
    from chemclaw.science.calc.store import CalculationKey, CALCULATION_EPOCH
    from chemclaw.science.calc.xtb import _sp_structure
    from chemclaw.science.calc.xtb_spec import XtbSpec
    print(CALCULATION_EPOCH, stable_hash('CCO'), _sp_structure('CCO', 0).structure_id)
    print(XtbSpec(task='sp').cache_key(_sp_structure('CCO', 0)).as_str())"

**Two of these rows depend on the installed tblite and RDKit versions and two do not**, and that
split is deliberate. `stable_hash`, `CALCULATION_EPOCH` and the flat-string format are pure — they
must never move. `structure_id` moves with RDKit (a new ETKDG embedding is a new geometry) and the
whole key moves with either distribution, so those are asserted *structurally* plus pinned against
the versions this test observes, rather than frozen against a string that a legitimate upgrade would
break.
"""

from __future__ import annotations

from importlib.metadata import version

import pytest
from chemclaw_mcp_calc.engine.ids import stable_hash
from chemclaw_mcp_calc.engine.key import CALCULATION_EPOCH, CalculationKey
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb import _sp_structure
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

# The version this repository's copy of `CALCULATION_EPOCH` must equal. It is a **source constant on
# both sides**, moved only when a ChemClaw-side change makes an already-written row wrong — and it
# has to move in both repositories in the same change, or the two silently stop addressing the same
# rows. Bumping it here alone is the failure this line exists to catch.
CHEMCLAW3_EPOCH = "1"

# (payload, the digest Chemclaw3's `stable_hash` returns for it). Pure: sorted keys, tight
# separators, SHA-256, first 16 hex characters. Nothing about these may ever change.
HASH_CONTRACT: list[tuple[object, str]] = [
    ("CCO", "f29e20f49d416e54"),
    ({"b": 2, "a": [1, "x"]}, "8cbd548a32262b76"),
    ({"smiles": "CCO"}, "a7d334ebee616d78"),
    ({"epoch": "1", "params": None}, "a075a6029c28d314"),
]


@pytest.mark.parametrize(("payload", "digest"), HASH_CONTRACT, ids=lambda case: str(case)[:32])
def test_the_hash_matches_chemclaw3(payload: object, digest: str) -> None:
    """One row of the hash contract. A failure means the two identity schemes have diverged."""
    assert stable_hash(payload) == digest


def test_the_epoch_matches_chemclaw3() -> None:
    """`CALCULATION_EPOCH` is one constant with two homes, and they must agree.

    It rides in `params_hash` rather than in `calc_version` for a reason worth restating here,
    because it is what makes a wrong value hard to notice: the version string is also the
    calibration ledger's key, and a measured residual stays valid across a ChemClaw-side fix that a
    *cached prediction* does not. So bumping the epoch invalidates the cache and leaves the ledger
    intact — which is correct, and which also means a spurious bump here costs CPU rather than
    raising.
    """
    assert CALCULATION_EPOCH == CHEMCLAW3_EPOCH


def test_the_flat_key_format_is_the_one_chemclaw3_parses() -> None:
    """`calc_type@calc_version:input_hash:params_hash` — separators included.

    Built from a hand-made key rather than a computed one, so this row stays fixed forever while the
    rows below legitimately move with the installed packages.
    """
    key = CalculationKey(
        calc_type="xtb.sp",
        calc_version="GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2",
        input_hash="389b625b3220108a",
        params_hash="b41312b0cdc59ab7",
    )
    assert key.as_str() == (
        "xtb.sp@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:b41312b0cdc59ab7"
    )


def test_build_folds_the_epoch_into_params_and_nothing_else() -> None:
    """The two `stable_hash` calls `build` makes, pinned against Chemclaw3's own output.

    `inputs` is hashed bare; `params` is hashed inside an `{"epoch": ..., "params": ...}` envelope.
    Getting the envelope wrong — hashing `params` bare, or naming the keys differently — produces a
    perfectly valid key that addresses nothing, which is the whole class of defect this file covers.
    """
    key = CalculationKey.build(
        calc_type="solubility",
        calc_version="esol-delaney@2004/rdkit-2026.3.5/u-0.75",
        inputs={"smiles": "CCO"},
    )
    assert key.input_hash == "a7d334ebee616d78"
    assert key.params_hash == "a075a6029c28d314"
    assert key.as_str() == (
        "solubility@esol-delaney@2004/rdkit-2026.3.5/u-0.75:a7d334ebee616d78:a075a6029c28d314"
    )


def test_the_structure_id_is_serialized_and_ignored_on_the_way_back_in() -> None:
    """It has to *be on the payload*, and it has to be recomputed rather than trusted.

    Both halves matter and they pull in opposite directions. A plain property would not serialize at
    all, so a caller receiving a geometry would have to re-derive its content address — the silent
    divergence this seam exists to remove, since the derivation depends on the installed RDKit and
    on `xtb_geometry_decimals`. But a field a caller could *set* would be worse: an edited payload
    would then key as whatever it claimed rather than as what it is.

    `computed_field` is exactly that pair — written on the way out, ignored on the way in.
    """
    structure = Structure(elements=[8, 1], positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 0.96]], charge=-1)
    payload = structure.model_dump()
    assert payload["structure_id"] == structure.structure_id

    lying = {**payload, "structure_id": "st_0000000000000000"}
    assert Structure.model_validate(lying).structure_id == structure.structure_id


def test_structure_id_is_derived_from_the_rounded_geometry_and_nothing_else() -> None:
    """The four fields that make a structure id, and the two that deliberately do not.

    `smiles` and `origin` are excluded so that two identical geometries are one structure whether
    one was embedded and the other optimized — which is what lets a downstream calculation address
    the same entry regardless of route. Asserted directly, because a well-meaning "include the
    SMILES, it is free" would fork every key in the system without failing anything else.
    """
    positions = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.96]]
    base = Structure(elements=[8, 1], positions=positions, charge=-1)
    labelled = Structure(
        elements=[8, 1],
        positions=positions,
        charge=-1,
        smiles="[OH-]",
        origin="xtb.opt@whatever:0:0",
    )
    assert base.structure_id == labelled.structure_id
    assert base.structure_id.startswith("st_")

    # Rounding happens on construction, so float noise below `xtb_geometry_decimals` (4 → 0.1 pm)
    # cannot fork the id, and the *stored* coordinates are the ones that were hashed.
    noisy = Structure(elements=[8, 1], positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 0.960001]], charge=-1)
    assert noisy.structure_id == base.structure_id
    assert noisy.positions == base.positions

    # A different charge is a different calculation even at the same coordinates.
    assert (
        Structure(elements=[8, 1], positions=positions, charge=-1, multiplicity=1).structure_id
        != Structure(elements=[8, 1, 1], positions=[*positions, [0.9, 0.0, 0.0]]).structure_id
    )


def test_the_whole_key_matches_chemclaw3_on_the_versions_this_test_observes() -> None:
    """End to end: ethanol's single-point key, byte for byte.

    Pinned only for the exact `tblite`/`rdkit` pair Chemclaw3 was run on to produce it, and skipped
    otherwise — because a version bump *should* change this string, and a test that failed on the
    upgrade would be asserting the wrong thing. What it catches is the case that matters: the two
    repositories running the same distributions and producing different keys, which is drift in the
    derivation rather than in the dependencies.
    """
    observed = (version("tblite"), version("rdkit"))
    if observed != ("0.7.0", "2026.3.5"):
        pytest.skip(f"pinned against tblite 0.7.0 / rdkit 2026.3.5; this env has {observed}")
    structure = _sp_structure("CCO", 0)
    assert structure.structure_id == "st_739a222f45be0c3a"
    assert XtbSpec(task="sp").cache_key(structure).as_str() == (
        "xtb.sp@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:b41312b0cdc59ab7"
    )
