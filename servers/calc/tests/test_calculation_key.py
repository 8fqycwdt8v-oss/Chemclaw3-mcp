"""`calculation_key` returns the identity the compute tool will produce — the design rests on this.

The whole point of the split is that Chemclaw3 keeps the calculation cache and this server does the
computing. That only works if Chemclaw3 can answer "have I already computed this?" *before* paying
for the calculation, and `cached_compute` needs a `CalculationKey` to do it:

```python
hit = await store.get(key)          # <- the key is an argument, not a result
if hit is not None:
    return hit.result, True
result = await compute()
```

If the key this tool returns and the key the compute tool stamps on its result ever disagreed, the
consequence is not an error. It is a lookup that misses forever — every calculation recomputed,
every minute of CPU paid twice — and, worse, a `predictions` row written under a version string
nothing reconciles, which surfaces as `calculator_trust` reporting `UNCALIBRATED` rather than as a
failure. So the parity is asserted directly, for every tool, against the real compute path.
"""

from __future__ import annotations

from typing import Any

import pytest
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine import xtb_engine
from chemclaw_mcp_calc.engine.identity import COMPUTE_TOOLS, calculation_identity
from chemclaw_mcp_calc.engine.key import CalculationKey

# The two tools whose key is not derivable from their arguments, and which therefore return `None`
# on **both** sides of the parity check. Named as a set so a third cannot join them quietly: adding
# a tool here is a deliberate statement that its answer cannot be looked up before it is computed,
# which is a real loss and should be argued rather than absorbed.
#
# - `predict_logd` never had a key: Chemclaw3 did not cache logD, because its expensive half is
#   already a cached pKa and Crippen LogP is sub-millisecond. Its result carries `calc_key: null`
#   too, so the two sides agree exactly.
# - `compute_thermochemistry`'s key names the geometry `relax_to_minimum` finally settled on, which
#   is an *output* — the loop optimises, takes a Hessian, and displaces along the imaginary mode and
#   repeats when the optimiser lands on a saddle. Its result does carry a key (computed after the
#   fact); it simply cannot be known beforehand.
WITHOUT_A_DERIVABLE_KEY = frozenset({"predict_logd", "compute_thermochemistry"})

# One argument set per tool, and the compute coroutine that must agree with it. Deliberately the
# *same* arguments on both sides — that is the property, and passing different ones would make the
# test pass for the wrong reason. Small molecules, because this file runs all nine calculations.
CASES: list[tuple[str, dict[str, Any], Any]] = [
    ("compute_xtb_energy", {"smiles": "CCO"}, tools.compute_xtb_energy),
    ("compute_xtb_energy", {"smiles": "CC(=O)[O-]", "charge": -1}, tools.compute_xtb_energy),
    (
        "compute_electronic_properties",
        {"smiles": "CCO", "solvent": "water"},
        tools.compute_electronic_properties,
    ),
    (
        "predict_site_reactivity",
        {"smiles": "CCO", "mode": "nucleophilic", "top_n": 3},
        tools.predict_site_reactivity,
    ),
    ("optimize_geometry", {"smiles": "O"}, tools.optimize_geometry),
    ("optimize_geometry", {"smiles": "O", "solvent": "thf"}, tools.optimize_geometry),
    (
        "compute_thermochemistry",
        {"smiles": "O", "symmetry_number": 2},
        tools.compute_thermochemistry,
    ),
    ("predict_pka", {"smiles": "CC(=O)O"}, tools.predict_pka),
    ("predict_solubility", {"smiles": "CCO"}, tools.predict_solubility),
    ("predict_logd", {"smiles": "CC(=O)O", "ph": 1.0}, tools.predict_logd),
    (
        "predict_developability_profile",
        {"smiles": "CCO"},
        tools.predict_developability_profile,
    ),
]


@pytest.mark.parametrize(
    ("tool", "arguments", "compute"), CASES, ids=[f"{c[0]}-{c[1]['smiles']}" for c in CASES]
)
async def test_the_key_derived_up_front_is_the_key_the_result_carries(
    tool: str, arguments: dict[str, Any], compute: Any
) -> None:
    """Derive, then compute, then compare. The one property the remote-cache design rests on.

    Both halves matter. `calc_version` is what Chemclaw3's calibration ledger matches on — exactly,
    with no version pooling — and `calc_key` is what its calculation cache is addressed by, so a
    divergence in either is a silent cost rather than a failure.
    """
    identity = await tools.calculation_key(tool, arguments)
    result = await compute(**arguments)

    assert identity.tool == tool
    assert identity.calc_version == result.calc_version, (
        f"{tool}: the version derived up front is not the one the result carries; a ledger row "
        "written under one and read under the other is unreachable, and reads as UNCALIBRATED"
    )
    if tool in WITHOUT_A_DERIVABLE_KEY:
        assert identity.calc_key is None
        assert identity.caveat, (
            f"{tool} returns no key, so it must say why — an absent key with no reason reads as "
            "'not computed yet', which is the one thing it does not mean"
        )
        return
    assert identity.calc_key == result.calc_key, (
        f"{tool}: the key derived up front is not the one the result carries; every lookup would "
        "miss forever and every calculation would be paid for twice"
    )


@pytest.mark.parametrize(
    ("tool", "arguments", "compute"), CASES, ids=[f"{c[0]}-{c[1]['smiles']}" for c in CASES]
)
async def test_the_four_parts_reconstruct_the_flat_key(
    tool: str, arguments: dict[str, Any], compute: Any
) -> None:
    """`key` is what `store.get` takes; `calc_key` is the same identity flattened.

    Returned as an object rather than left to be parsed out of the string on purpose:
    `calc_version` legitimately contains both `@` and `:` — `esol-delaney@2004/…` and
    `cal-0.28733:-29.3116` — so a caller splitting the flat form is one delimiter away from a key
    that misses forever. The compute tools return only the flat form because a caller reaching them
    already called this tool to do the lookup.
    """
    identity = await tools.calculation_key(tool, arguments)
    if identity.key is None:
        assert identity.calc_key is None
        return
    assert isinstance(identity.key, CalculationKey)
    assert identity.key.as_str() == identity.calc_key
    assert identity.key.calc_version == identity.calc_version


async def test_deriving_a_key_runs_no_scf() -> None:
    """ "Cheap" is asserted rather than claimed: every SCF path is made to raise.

    `make_calculator` resolves `Calculator` in `xtb_engine`'s own globals and `run_singlepoint` goes
    through it, so replacing that one name blocks every route to a single point — the in-process
    optimizer, the finite-difference Hessian and the three Fukui points included. All nine
    identities still come back, which is the property: this tool is a canonicalisation, an embedding
    and two hashes.

    Worth pinning because the obvious way to "fix" `compute_thermochemistry`'s missing key is to
    relax the geometry first, and that would turn a cheap probe into a minutes-long call on the
    exact tool a cache is most needed for.
    """

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deriving a key must not run an SCF")

    original = xtb_engine.Calculator
    xtb_engine.Calculator = _explode  # type: ignore[misc]
    try:
        for tool in sorted(COMPUTE_TOOLS):
            identity = calculation_identity(tool, {"smiles": "CCO"})
            assert identity.calc_version
    finally:
        xtb_engine.Calculator = original  # type: ignore[misc]


async def test_only_the_two_named_tools_lack_a_derivable_key() -> None:
    """The set is closed. A third tool losing its key would be a real regression, quietly.

    Checked over the whole surface rather than over the cases above, so it holds for arguments no
    case happens to use.
    """
    for tool in sorted(COMPUTE_TOOLS):
        identity = calculation_identity(tool, {"smiles": "CC(=O)O"})
        assert (identity.calc_key is None) == (tool in WITHOUT_A_DERIVABLE_KEY), tool


async def test_the_two_tools_without_a_key_say_why() -> None:
    """An absent key must never read as "not computed yet". Both cases carry their reason.

    They are different absences and the messages say which. `predict_logd` never had a key —
    Chemclaw3 did not cache logD, because the expensive half is already a cached pKa.
    `compute_thermochemistry` cannot have one derived from its arguments, because its key names the
    geometry the refinement loop settled on, which is an output of the calculation.
    """
    logd = await tools.calculation_key("predict_logd", {"smiles": "CC(=O)O"})
    assert logd.key is None and logd.calc_key is None
    assert logd.caveat is not None and "predict_pka" in logd.caveat

    thermo = await tools.calculation_key("compute_thermochemistry", {"smiles": "O"})
    assert thermo.key is None and thermo.calc_key is None
    assert thermo.caveat is not None and "refinement loop" in thermo.caveat
    # The version is still exact, and still worth returning: it is the string the calibration ledger
    # matches on, and the ledger is keyed per prediction rather than per cache entry.
    assert "GFN2-xTB" in thermo.calc_version


async def test_an_argument_the_tool_does_not_take_is_refused_not_ignored() -> None:
    """The quiet failure this refusal exists for: a misspelled `solvent` keying the gas phase.

    Ignoring it would return a *valid* key — for the unsolvated calculation — the lookup would hit a
    real row, and the caller would be handed a gas-phase answer to a solvated question with nothing
    anywhere saying so.
    """
    with pytest.raises(ValueError, match="does not take 'solvant'"):
        await tools.calculation_key(
            "compute_electronic_properties", {"smiles": "CCO", "solvant": "water"}
        )

    # And the argument that *is* accepted really does move the key, which is what makes the above
    # more than a spelling check.
    gas = await tools.calculation_key("compute_electronic_properties", {"smiles": "CCO"})
    solvated = await tools.calculation_key(
        "compute_electronic_properties", {"smiles": "CCO", "solvent": "water"}
    )
    assert gas.calc_key != solvated.calc_key


async def test_an_unknown_tool_name_is_refused_and_lists_the_real_ones() -> None:
    """A typo'd tool name must not fall through to a default, and the message is the fix."""
    with pytest.raises(ValueError, match="is not a compute tool"):
        await tools.calculation_key("compute_energy", {"smiles": "CCO"})
    with pytest.raises(ValueError, match="requires a 'smiles'"):
        await tools.calculation_key("predict_pka", {})


async def test_the_derivation_table_matches_the_served_surface() -> None:
    """Every compute tool has a derivation, and every derivation names a served tool.

    Both directions. A tenth compute tool with no derivation would be one a caller cannot cache; a
    derivation for a tool that no longer exists would be a key nothing ever writes.
    """
    served = {tool.name for tool in await tools.server.list_tools()}
    assert set(COMPUTE_TOOLS) | {"calculation_key"} == served


async def test_each_derivation_accepts_exactly_its_tool_s_arguments() -> None:
    """The `accepts` sets are checked against the served tools' own input schemas, not by eye.

    This is what stops the refusal above becoming wrong the day an argument is added: a new
    parameter on a compute tool that nobody added to `COMPUTE_TOOLS` would make `calculation_key`
    reject a perfectly valid call, which is a loud failure — but the reverse, a parameter removed
    from the tool and left in `accepts`, is silent, and this catches both.
    """
    schemas = {tool.name: tool.inputSchema for tool in await tools.server.list_tools()}
    for name, (accepts, _) in COMPUTE_TOOLS.items():
        declared = set(schemas[name].get("properties", {}))
        assert accepts == declared, f"{name}: derivation accepts {accepts}, tool takes {declared}"


async def test_two_spellings_of_one_molecule_derive_one_key() -> None:
    """The canonicalisation happens on this path too, or a caller's spelling would fork the cache.

    It is the same `require_canonical_smiles` the compute path uses — asserted here because this
    tool is the one that decides which row is looked up, so a lenient probe would miss a row the
    compute path would then happily overwrite under the canonical key.
    """
    for tool in ("compute_xtb_energy", "predict_pka", "predict_solubility"):
        assert (await tools.calculation_key(tool, {"smiles": "CCO"})).calc_key == (
            await tools.calculation_key(tool, {"smiles": "OCC"})
        ).calc_key


async def test_a_bad_input_is_refused_here_exactly_as_the_compute_tool_refuses_it() -> None:
    """A probe accepting what the calculation rejects would defer the error to the expensive call.

    Both refusals come from the same code — `require_canonical_smiles` and `XtbSpec`'s solvent
    validator — because the derivation reads the engine's own `*_inputs` pairing rather than
    restating it.
    """
    with pytest.raises(ValueError, match="invalid SMILES"):
        await tools.calculation_key("compute_xtb_energy", {"smiles": "CCO junk"})
    with pytest.raises(ValueError, match="tetrahydrofuran"):
        await tools.calculation_key(
            "optimize_geometry", {"smiles": "CCO", "solvent": "2-methyltetrahydrofuran"}
        )
