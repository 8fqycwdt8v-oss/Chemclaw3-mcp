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

So the contract is written as **data**: an input and the exact string it must produce, taken by
running Chemclaw3's own code rather than by reading this repository's copy and agreeing with it.

**The reproduction command has to name modules Chemclaw3 still has, and for a while it did not.**
It imported `chemclaw.science.calc.xtb` and `chemclaw.science.calc.xtb_spec`, both of which left
with the physics — so the one instruction telling a future session how to re-derive these values
raised `ModuleNotFoundError` on its last two lines. What Chemclaw3 can still be asked, and what
every pinned digest below comes from:

    cd /path/to/Chemclaw3 && uv run python -c "
    from chemclaw.core.ids import stable_hash
    from chemclaw.science.calc.store import CALCULATION_EPOCH, CalculationKey
    print(CALCULATION_EPOCH, stable_hash('CCO'), stable_hash({'smiles': 'CCO'}))
    print(CalculationKey.build(
        calc_type='solubility',
        calc_version='esol-delaney@2004/rdkit-2026.3.5/u-0.75',
        inputs={'smiles': 'CCO'}).as_str())
    print(list(CalculationKey.model_fields))"

## The epochs compose; they are not compared

`CALCULATION_EPOCH` exists on both sides and this file used to assert the two were **equal**,
against a hand-copied literal, with a comment saying a unilateral bump here was "the failure this
line exists to catch". Neither half held up:

- A literal copied from the other repository and never re-read agrees only with whoever last
  edited it. Measured: setting `CALCULATION_EPOCH` **and** the copy to `"9"` left that assertion
  green while Chemclaw3 said `"2"`. The guard that actually bites is the pinned digest in
  `test_build_folds_the_epoch_into_params_and_nothing_else`, because that number came from
  Chemclaw3 — the same `"9"` turns it red.
- A unilateral bump here is not a failure at all. `CalculationKey.build` has **no caller left** in
  Chemclaw3's `src/`; every `calc` key comes back from this server as four fields and is rebuilt by
  `connectors/calc/remote.py::remote_key`, which folds *its* epoch over **this server's**
  `params_hash`: `stable_hash({"epoch": <theirs>, "remote_params": <ours>})`. The two therefore
  **compose**. A bump on either side changes the composed digest and misses every stored row, which
  is exactly what an epoch is for. Moving them together stays the convention — it keeps the two
  epoch logs describing the same events — but it is a convention, not a correctness invariant, and
  it is not something a literal in this file can enforce.

What this file does enforce is what a divergence would actually break: the pure `stable_hash`, the
`{"epoch": ..., "params": ...}` envelope this server's epoch rides in, the flat string format, and
the four field *names* `remote_key` reads by name.

**Two of these rows depend on the installed tblite and RDKit versions and two do not**, and that
split is deliberate. `stable_hash`, the envelope and the flat-string format are pure — they must
never move. `structure_id` moves with RDKit (a new ETKDG embedding is a new geometry) and the whole
key moves with either distribution, so those are asserted *structurally* plus pinned against the
versions this test observes, rather than frozen against a string that a legitimate upgrade would
break.
"""

from __future__ import annotations

from functools import partial
from importlib.metadata import version

import pytest
from chemclaw_mcp_calc.engine import key as key_module
from chemclaw_mcp_calc.engine.ids import stable_hash
from chemclaw_mcp_calc.engine.key import CALCULATION_EPOCH, CalculationKey
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb import _sp_structure
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

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


def test_the_epoch_is_what_rides_in_params_and_nothing_else_moves_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bump here must change `params_hash` — and must change nothing else about the key.

    This is what the deleted `CALCULATION_EPOCH == CHEMCLAW3_EPOCH` assertion was reaching for and
    could not check. The epoch's whole job is to invalidate stored rows from *our* side, and it can
    only do that through `params_hash`: `calc_type`, `calc_version` and `input_hash` are facts about
    the calculation and the programs that ran it, and folding the epoch into any of them would make
    a ChemClaw-side fix look like a different calculation.

    It rides in `params_hash` rather than in `calc_version` for a reason worth restating, because it
    is what makes a wrong value hard to notice: the version string is also the calibration ledger's
    key, and a measured residual stays valid across a ChemClaw-side fix that a *cached prediction*
    does not. So a bump invalidates the cache and leaves the ledger intact — which is correct, and
    which also means a spurious bump costs CPU rather than raising.
    """
    built = partial(
        CalculationKey.build,
        calc_type="solubility",
        calc_version="esol-delaney@2004/rdkit-2026.3.5/u-0.75",
        inputs={"smiles": "CCO"},
    )
    key = built()
    assert key.params_hash == stable_hash({"epoch": CALCULATION_EPOCH, "params": None})
    assert key.input_hash == stable_hash({"smiles": "CCO"})

    monkeypatch.setattr(key_module, "CALCULATION_EPOCH", "next")
    bumped = built()
    assert bumped.params_hash != key.params_hash, "a bump that changes no key invalidates nothing"
    assert (bumped.calc_type, bumped.calc_version, bumped.input_hash) == (
        key.calc_type,
        key.calc_version,
        key.input_hash,
    ), "the epoch moved a field that names the calculation rather than our contribution to it"


def test_the_key_crosses_the_wire_as_the_four_fields_remote_key_reads_by_name() -> None:
    """Chemclaw3 rebuilds a key field by field, so the *names* are the contract, not the string.

    `connectors/calc/remote.py::remote_key` reads `key["calc_type"]`, `key["calc_version"]`,
    `key["input_hash"]` and `key["params_hash"]` out of this server's `calculation_key` answer, and
    raises `CalcToolError("calculation_key returned an unusable key")` on a `KeyError`. It reads
    them by name deliberately: a real `calc_version` contains both flat-form delimiters —
    `esol-delaney@2004` carries the `@`, `cal-0.28733:-29.3116` carries the `:` — so a client
    splitting `as_str()` would build a key that misses forever.

    Nothing pinned those four names, and renaming one here is a rename with no local consequence:
    this server would keep serving, and every calculation on the Chemclaw3 side would fail at the
    key round trip. Taken from Chemclaw3's own `list(CalculationKey.model_fields)`.
    """
    assert list(CalculationKey.model_fields) == [
        "calc_type",
        "calc_version",
        "input_hash",
        "params_hash",
    ]
    served = CalculationKey.build(
        calc_type="solubility",
        calc_version="esol-delaney@2004/rdkit-2026.3.5/u-0.75",
        inputs={"smiles": "CCO"},
    ).model_dump()
    # Exactly what `remote_key` does with the answer, including the fold it applies on its side.
    rebuilt = CalculationKey(
        calc_type=served["calc_type"],
        calc_version=served["calc_version"],
        input_hash=served["input_hash"],
        params_hash=stable_hash({"epoch": "<theirs>", "remote_params": served["params_hash"]}),
    )
    assert rebuilt.params_hash != served["params_hash"], (
        "Chemclaw3 folds its own epoch over ours rather than passing it through; if these were "
        "equal, a bump on their side would invalidate nothing"
    )
    assert rebuilt.as_str().startswith("solubility@esol-delaney@2004/rdkit-2026.3.5/u-0.75:")


def test_the_flat_key_format_is_the_one_chemclaw3_parses() -> None:
    """`calc_type@calc_version:input_hash:params_hash` — separators included.

    Built from a hand-made key rather than a computed one, so this row stays fixed forever while the
    rows below legitimately move with the installed packages.
    """
    key = CalculationKey(
        calc_type="xtb.sp",
        calc_version="GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2",
        input_hash="389b625b3220108a",
        params_hash="74c818075e77fec2",
    )
    assert key.as_str() == (
        "xtb.sp@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:74c818075e77fec2"
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
    assert key.params_hash == "3ba6ef80c850abd1"
    assert key.as_str() == (
        "solubility@esol-delaney@2004/rdkit-2026.3.5/u-0.75:a7d334ebee616d78:3ba6ef80c850abd1"
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


def test_the_whole_key_is_stable_on_the_versions_this_test_observes() -> None:
    """End to end: ethanol's single-point key, byte for byte.

    **These two strings are this repository's, and saying otherwise was the point to correct.**
    This test used to claim they came from "running Chemclaw3's own code"; Chemclaw3 has neither
    `_sp_structure` nor `XtbSpec` — both left with the physics — so it cannot produce either one,
    and no cross-repo agreement is being asserted here. Nor should one be: `remote_key` refuses to
    re-derive a `structure_id` or a `calc_version` on that side precisely because a locally built
    value would be well-formed and would match nothing.

    So what this pins is *this* server's derivation against the distributions it was measured on,
    which is the thing that can silently move a key while every unit test passes. Skipped on any
    other `tblite`/`rdkit` pair, because a version bump *should* change this string and a test that
    failed on the upgrade would be asserting the wrong thing.
    """
    observed = (version("tblite"), version("rdkit"))
    if observed != ("0.7.0", "2026.3.5"):
        pytest.skip(f"pinned against tblite 0.7.0 / rdkit 2026.3.5; this env has {observed}")
    structure = _sp_structure("CCO", 0)
    assert structure.structure_id == "st_739a222f45be0c3a"
    assert XtbSpec(task="sp").cache_key(structure).as_str() == (
        "xtb.sp@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:74c818075e77fec2"
    )
