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

# Tools that compute nothing, so there is nothing to derive an identity *for*: two build a geometry
# and one is this probe itself.
HELPERS = {"embed_structure", "combine_structures", "calculation_key"}


def sweep_arguments(tool: str, accepts: frozenset[str], geometry: dict[str, Any]) -> dict[str, Any]:
    """Minimal valid arguments for `tool`, for the tests that sweep the whole table.

    One builder rather than a literal per tool, because these tests are about the *set* being
    closed: a new tool must be exercised by them without anyone remembering to add a case, and a
    builder keyed on the declared `accepts` set is what makes that automatic.
    """
    arguments: dict[str, Any] = (
        {"structure": geometry} if "structure" in accepts else {"smiles": "CC(=O)O"}
    )
    if tool == "scan_point":
        arguments |= {"atoms": [0, 1, 2], "value": 1.5}
    return arguments


# The tools whose key is not derivable from their arguments, and which therefore return `None` on
# **both** sides of the parity check. A frozenset with one member rather than a bare constant, so a
# second joining it is a deliberate statement that its answer cannot be looked up before it is
# computed — a real loss, to be argued rather than absorbed.
#
# `predict_logd` never had a key: Chemclaw3 did not cache logD, because its expensive half is
# already a cached pKa and Crippen LogP is sub-millisecond. Its result carries `calc_key: null` too,
# so the two sides agree exactly.
#
# **It briefly had company, and how that resolved is the point of this file.**
# `compute_thermochemistry`'s key named the geometry its refinement loop settled on — an output, not
# a function of its arguments — so it could not be derived. Rather than ship a tool Chemclaw3 could
# not cache, the *composite* was removed and its parts exposed instead: `relax_structure` +
# `compute_hessian`, each keyed, with the RRHO arithmetic on the caller's side. The measurement that
# forced it: repeating thermochemistry in Chemclaw3 costs 0.007 s against 0.816 s cold for ethanol
# and 0.012 s against 3.273 s for ethyl acetate. Decomposed, every one of those hits still hits.
WITHOUT_A_DERIVABLE_KEY = frozenset({"predict_logd"})

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


async def test_the_primitives_key_up_front_too() -> None:
    """The same property for the structure-in primitives, which is where it actually pays.

    These are what Chemclaw3's durable-job activities compose, so the identity has to be answerable
    *before* the call for the composition to hit a cache at all — a scan point that could only be
    keyed after it ran would make a 24-point profile 24 unavoidable optimisations.

    Built as one chain rather than independent cases because that is how a caller uses them: embed,
    relax, then differentiate at the relaxed geometry.
    """
    water = await tools.embed_structure("O")
    ethanol = await tools.embed_structure("CCO")

    relax_id = await tools.calculation_key("relax_structure", {"structure": water.model_dump()})
    relaxed = await tools.relax_structure(water)
    assert relax_id.calc_key == relaxed.calc_key
    assert relax_id.structure_id == water.structure_id

    for tool, arguments, computed in (
        (
            "compute_properties_at",
            {"structure": relaxed.structure.model_dump()},
            await tools.compute_properties_at(relaxed.structure),
        ),
        (
            "compute_hessian",
            {"structure": relaxed.structure.model_dump()},
            await tools.compute_hessian(relaxed.structure),
        ),
        (
            "scan_point",
            {"structure": ethanol.model_dump(), "atoms": [0, 1, 2, 3], "value": 60.0},
            await tools.scan_point(ethanol, [0, 1, 2, 3], 60.0),
        ),
    ):
        identity = await tools.calculation_key(tool, arguments)
        assert identity.calc_key == computed.calc_key, tool
        assert identity.calc_version == computed.calc_version, tool


async def test_a_scan_point_keys_as_the_constrained_optimisation_it_is() -> None:
    """A scan point is an `xtb.opt` row, not a namespace of its own.

    Not a coincidence to preserve but the reason `scan_point` has no task of its own: driving the
    coordinate is pure geometry, so what is actually computed is an optimisation with those atoms
    frozen. Sharing the row is what stops a profile and a hand-written constrained relaxation of the
    same geometry paying twice — and it is why `XtbTask` deliberately has no `scan` member.
    """
    ethanol = await tools.embed_structure("CCO")
    point = await tools.calculation_key(
        "scan_point", {"structure": ethanol.model_dump(), "atoms": [0, 1, 2, 3], "value": 60.0}
    )
    assert point.key is not None and point.key.calc_type == "xtb.opt"

    # The frozen atoms have to be *in* the key, or every point of a profile would collide with the
    # free optimisation of the same driven geometry.
    free = await tools.calculation_key("relax_structure", {"structure": ethanol.model_dump()})
    assert free.key is not None and free.key.calc_type == "xtb.opt"
    assert free.key.params_hash != point.key.params_hash


async def test_a_crest_search_refuses_to_be_keyed_without_its_binary() -> None:
    """The probe refuses exactly where the search would, and for the reason that matters.

    `CrestSpec.calc_version()` answers `crest-absent` rather than raising, so a key *is* derivable
    with no binary — and it would be a well-formed identity naming a program that cannot run,
    addressing a row nothing will ever write. That is the same shape as the `binary_version()` trap
    this whole port exists to contain, so both paths refuse together.
    """
    water = await tools.embed_structure("O")
    for tool in ("search_conformer_ensemble", "search_binding_modes"):
        with pytest.raises(ValueError, match="crest"):
            await tools.calculation_key(tool, {"structure": water.model_dump()})


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

    from chemclaw_mcp_calc.engine.structure import structure_from_smiles

    # Embedded *before* the SCF is broken: building a geometry is RDKit's job, not tblite's, and
    # this test is about what the derivation does with one rather than about how it was made.
    geometry = structure_from_smiles("CC(=O)O", optimize=True).model_dump()

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deriving a key must not run an SCF")

    original = xtb_engine.Calculator
    xtb_engine.Calculator = _explode  # type: ignore[misc]
    try:
        for tool, (accepts, _) in sorted(COMPUTE_TOOLS.items()):
            if tool.startswith("search_"):
                continue  # refuses on the missing binary before any of this is reached
            identity = calculation_identity(tool, sweep_arguments(tool, accepts, geometry))
            assert identity.calc_version
    finally:
        xtb_engine.Calculator = original  # type: ignore[misc]


async def test_only_the_named_tool_lacks_a_derivable_key() -> None:
    """The set is closed. A second tool losing its key would be a real regression, quietly.

    Checked over the whole surface rather than over the cases above, so it holds for arguments no
    case happens to use.
    """
    from chemclaw_mcp_calc.engine.structure import structure_from_smiles

    geometry = structure_from_smiles("CC(=O)O", optimize=True).model_dump()
    for tool, (accepts, _) in sorted(COMPUTE_TOOLS.items()):
        if tool.startswith("search_"):
            continue  # keyed like the rest; refuses without the binary, checked separately
        identity = calculation_identity(tool, sweep_arguments(tool, accepts, geometry))
        assert (identity.calc_key is None) == (tool in WITHOUT_A_DERIVABLE_KEY), tool


async def test_the_one_tool_without_a_key_says_why() -> None:
    """An absent key must never read as "not computed yet". The one case carries its reason.

    And the reason names the alternative: logD's cost is a pKa, whose key *is* available, so a
    caller that wants to avoid paying twice knows exactly what to look up.
    """
    logd = await tools.calculation_key("predict_logd", {"smiles": "CC(=O)O"})
    assert logd.key is None and logd.calc_key is None
    assert logd.caveat is not None and "predict_pka" in logd.caveat
    # The version is still exact, and still worth returning: it is what a calibration ledger matches
    # on, and a ledger is keyed per prediction rather than per cache entry.
    assert logd.calc_version.startswith("logd/")


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
    assert set(COMPUTE_TOOLS) | HELPERS == served


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
