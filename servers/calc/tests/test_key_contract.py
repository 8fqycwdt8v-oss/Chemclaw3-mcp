"""The key contract with Chemclaw3, written as literal strings on both sides.

`engine/ids.py` and `engine/key.py` copy definitions Chemclaw3 owns (`chemclaw/core/ids.py`,
`chemclaw/science/calc/store.py`); neither repository may import the other. If they drift, an
`input_hash` misses every cache row (visible) or a `calc_version` misses every calibration row
(silent). So the contract is data, taken from Chemclaw3's own output:

    cd /path/to/Chemclaw3 && uv run python -c "
    from chemclaw.core.ids import stable_hash
    from chemclaw.science.calc.store import CALCULATION_EPOCH, CalculationKey
    print(CALCULATION_EPOCH, stable_hash('CCO'), stable_hash({'smiles': 'CCO'}))
    print(CalculationKey.build(
        calc_type='solubility', calc_version='esol-delaney@2004/rdkit-2026.3.5/u-0.75',
        inputs={'smiles': 'CCO'}).as_str())
    print(list(CalculationKey.model_fields))"

**The epochs compose; they are not compared.** Chemclaw3's `remote_key` folds its epoch over this
server's `params_hash`, so a bump on either side alone misses every stored row. Moving them
together is convention. Enforced here: the pure `stable_hash`, the `{"epoch", "params"}` envelope,
the flat string format, and the four field names `remote_key` reads.

`stable_hash`, the envelope and the format are pure and never move. `structure_id` and the full
key move with RDKit and tblite, so they are asserted structurally and pinned only against the
versions this test observes.
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
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec
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
    """A bump of the epoch must change `params_hash` and nothing else about the key.

    `calc_type`, `calc_version` and `input_hash` describe the calculation. The epoch stays out of
    `calc_version` because that is also the ledger's key: a bump invalidates the cache and leaves
    measured residuals intact.
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

    `remote_key` reads the four fields by name, because a real `calc_version` contains both `@` and
    `:`. Renaming one here would break every calculation on the Chemclaw3 side with no local
    symptom. Names taken from Chemclaw3's `list(CalculationKey.model_fields)`.
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
        calc_version="GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/scipy-1.17.1/h3",
        input_hash="389b625b3220108a",
        params_hash="74c818075e77fec2",
    )
    assert key.as_str() == (
        "xtb.sp@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/scipy-1.17.1/h3"
        ":389b625b3220108a:74c818075e77fec2"
    )


def test_build_folds_the_epoch_into_params_and_nothing_else() -> None:
    """The two `stable_hash` calls `build` makes, pinned against Chemclaw3's own output.

    `inputs` is hashed bare and `params` inside an `{"epoch", "params"}` envelope; getting the
    envelope wrong gives a valid key that addresses nothing.
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
    """`structure_id` is serialized on the way out and recomputed, not trusted, on the way in.

    Callers need it on the payload rather than re-deriving it (which depends on RDKit and
    `xtb_geometry_decimals`), but a settable field would let an edited payload key as whatever it
    claimed. `computed_field` gives exactly that pair.
    """
    structure = Structure(elements=[8, 1], positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 0.96]], charge=-1)
    payload = structure.model_dump()
    assert payload["structure_id"] == structure.structure_id

    lying = {**payload, "structure_id": "st_0000000000000000"}
    assert Structure.model_validate(lying).structure_id == structure.structure_id


def test_structure_id_is_derived_from_the_rounded_geometry_and_nothing_else() -> None:
    """The four fields that make a structure id, and the two that deliberately do not.

    `smiles` and `origin` are excluded so identical geometries are one structure whatever the route.
    Including them would fork every key without failing anything else.
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
    """End to end: ethanol's single-point and optimization keys, byte for byte.

    These strings are this repository's own derivation (Chemclaw3 cannot produce them and
    deliberately re-derives nothing). They pin it against the distributions it was measured on —
    tblite, RDKit, scipy (whose CODATA constants feed the unit conversions) and geomeTRIC — and skip
    on any other set, since an upgrade should change them. The `sp` key proves geomeTRIC stays out
    of the shared engine version; the `opt` key proves it is in the one calculation it decides.
    """
    observed = (version("tblite"), version("rdkit"), version("scipy"), version("geometric"))
    if observed != ("0.7.0", "2026.3.5", "1.17.1", "1.1.1"):
        pytest.skip(
            "pinned against tblite 0.7.0 / rdkit 2026.3.5 / scipy 1.17.1 / geometric 1.1.1; "
            f"env has {observed}"
        )
    structure = _sp_structure("CCO", 0)
    assert structure.structure_id == "st_739a222f45be0c3a"
    assert XtbSpec(task="sp").cache_key(structure).as_str() == (
        "xtb.sp@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/scipy-1.17.1/h3"
        ":389b625b3220108a:74c818075e77fec2"
    )
    assert OptSpec().cache_key(structure).as_str() == (
        "xtb.opt@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/scipy-1.17.1/h3+geometric-1.1.1"
        ":389b625b3220108a:5e9dada5819590e9"
    )
